# app/audio_engine.py
"""
Audio engine for bit-perfect playback via ALSA + soundfile.
Features:
 - list ALSA devices (hw: and plughw:)
 - persist last used alsa device and music dir
 - non-blocking play() that starts a playback thread (no join() in GUI)
 - robust ALSA open with backoff retries inside playback thread
 - integer dtype reads and raw pcm writes (keeps bit-path intact)

This version adds a Qt signal `pcm_chunk` to broadcast copies of PCM blocks
for a visualization UI. Emission is non-blocking and does not affect the
actual ALSA write path (we emit a copy).

This version also tracks a conservative "bit-perfect" flag based on:
 - using an explicit hw: ALSA device
 - integer PCM subtype (PCM_16 / PCM_24 / PCM_32)
The flag is exposed via status() as `bitperfect` and `bitperfect_reason`.
"""
import os
import glob
import threading
import time
import json
from typing import List, Optional

import numpy as np
import soundfile as sf
import alsaaudio
import mutagen
from mutagen.flac import FLAC

# Qt import for signaling
from PyQt6.QtCore import pyqtSignal, QObject

# Defaults (if you have a project config, it will be used)
try:
    from .config import MUSIC_DIR, ALSA_CARD, BUFFER_FRAMES
except Exception:
    MUSIC_DIR = os.path.expanduser("~/Music")
    ALSA_CARD = "hw:0,0"
    BUFFER_FRAMES = 4096


class TrackInfo:
    def __init__(self, track_id: int, path: str):
        self.id = track_id
        self.path = path
        self.name = os.path.basename(path)

        # metadata
        self.title: Optional[str] = None
        self.artist: Optional[str] = None
        self.album: Optional[str] = None
        self.sample_rate: Optional[int] = None
        self.bit_depth: Optional[int] = None
        self.channels: Optional[int] = None
        self.duration: Optional[float] = None
        self.album_art: Optional[bytes] = None

        self._read_metadata()

    def _read_metadata(self):
        try:
            a = mutagen.File(self.path)
            if a is None:
                self.title = self.name
                self.artist = "Unknown Artist"
                self.album = "Unknown Album"
            else:
                tags = getattr(a, "tags", None)

                def find_tag(keys):
                    if not tags:
                        return None
                    for k in keys:
                        if k in tags:
                            v = tags[k]
                            if isinstance(v, list):
                                return v[0]
                            return v
                        if k.lower() in tags:
                            v = tags[k.lower()]
                            if isinstance(v, list):
                                return v[0]
                            return v
                    return None

                self.title = find_tag(["TITLE", "Title"]) or self.name
                self.artist = find_tag(["ARTIST", "Artist", "PERFORMER"]) or "Unknown Artist"
                self.album = find_tag(["ALBUM", "Album"]) or "Unknown Album"

                info = getattr(a, "info", None)
                if info is not None:
                    self.duration = getattr(info, "length", None)
                    self.sample_rate = getattr(info, "sample_rate", None)
                    self.channels = getattr(info, "channels", None)
                    self.bit_depth = getattr(info, "bits_per_sample", None)

                if isinstance(a, FLAC):
                    if a.pictures:
                        try:
                            self.album_art = a.pictures[0].data
                        except Exception:
                            pass

                if not self.album_art and tags:
                    apic = tags.get("APIC:")
                    if apic:
                        try:
                            self.album_art = apic.data
                        except Exception:
                            pass

                if not self.album_art:
                    folder = os.path.dirname(self.path)
                    for candidate in ("cover.jpg", "Cover.jpg", "folder.jpg", "Folder.jpg", "cover.png"):
                        candidate_path = os.path.join(folder, candidate)
                        if os.path.exists(candidate_path):
                            try:
                                with open(candidate_path, "rb") as fh:
                                    self.album_art = fh.read()
                                break
                            except Exception:
                                continue
        except Exception as e:
            print(f"[audio_engine] Warning: metadata read error {self.path}: {e}", flush=True)
            if not self.title:
                self.title = self.name
            if not self.artist:
                self.artist = "Unknown Artist"
            if not self.album:
                self.album = "Unknown Album"

        # fallback: probe with soundfile for duration/sample info
        if self.duration is None:
            try:
                with sf.SoundFile(self.path) as sfh:
                    frames = sfh.frames
                    sr = sfh.samplerate
                    if frames and sr:
                        self.duration = frames / float(sr)
                        if not self.sample_rate:
                            self.sample_rate = sr
                        if not self.channels:
                            self.channels = sfh.channels
            except Exception:
                pass


class PlaybackState:
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class AudioEngine(QObject):
    """
    NOTE: AudioEngine now inherits QObject to support emission of `pcm_chunk` Qt signal.
    Everything else remains the same as your prior engine.

    This version also keeps track of a conservative "bit-perfect" flag for the
    current playback session.
    """
    pcm_chunk = pyqtSignal(object)  # emits numpy array (always a copy) for visualizer

    def __init__(self, music_dir: str = MUSIC_DIR, alsa_card: str = ALSA_CARD):
        super().__init__()  # QObject init
        self.music_dir = music_dir
        self.alsa_card = alsa_card

        self.tracks: List[TrackInfo] = []
        self.current_track: Optional[TrackInfo] = None

        # thread & control events (per-playback events are created inside play())
        self._playback_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        self._seek_to_seconds: Optional[float] = None
        self._seek_lock = threading.Lock()

        self._position: float = 0.0
        self._duration: float = 0.0

        self._state = PlaybackState.IDLE
        self._lock = threading.Lock()

        # Bit-perfect status (per-playback)
        self._bitperfect: bool = False
        self._bitperfect_reason: str = "Idle"

        # load persisted config (may override defaults)
        try:
            self._load_config()
        except Exception:
            pass

        # build library
        self._scan_library()

    # ---------- config persistence ----------
    def _config_path(self) -> str:
        cfg_dir = os.path.join(os.path.expanduser("~"), ".config", "bitperfect-player")
        os.makedirs(cfg_dir, exist_ok=True)
        return os.path.join(cfg_dir, "config.json")

    def _load_config(self):
        try:
            p = self._config_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
                alsa_card = cfg.get("alsa_card")
                music_dir = cfg.get("music_dir")
                if alsa_card:
                    self.alsa_card = alsa_card
                if music_dir:
                    self.music_dir = music_dir
        except Exception as e:
            print(f"[audio_engine] Warning: failed to load config: {e}", flush=True)

    def _save_config(self):
        try:
            p = self._config_path()
            cfg = {
                "alsa_card": self.alsa_card,
                "music_dir": self.music_dir
            }
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
        except Exception as e:
            print(f"[audio_engine] Warning: failed to save config: {e}", flush=True)

    # ---------- library ----------
    def _scan_library(self):
        patterns = ("*.flac", "*.wav", "*.mp3", "*.m4a", "*.aac")
        files = []
        for p in patterns:
            files.extend(glob.glob(os.path.join(self.music_dir, p)))
            files.extend(glob.glob(os.path.join(self.music_dir, "**", p), recursive=True))
        files = sorted(set(files))
        self.tracks = [TrackInfo(i, f) for i, f in enumerate(files)]

    def list_tracks(self) -> List[TrackInfo]:
        return self.tracks

    def get_track_by_id(self, track_id: int) -> Optional[TrackInfo]:
        for t in self.tracks:
            if t.id == track_id:
                return t
        return None

    def set_music_dir(self, music_dir: str):
        with self._lock:
            self.music_dir = music_dir
        self._scan_library()
        try:
            self._save_config()
        except Exception:
            pass

    # ---------- devices ----------
    def list_alsa_devices(self):
        """
        Return list of dicts: {id: <device id>, name: <desc>, hw: True/False}
        Offers both hw: and plughw: entries where available.
        """
        devices = []
        try:
            cards = alsaaudio.cards()
        except Exception:
            cards = []

        for idx, card_name in enumerate(cards):
            label_base = f"{card_name}"
            hw_id = f"hw:{idx},0"
            plug_id = f"plughw:{idx},0"

            try:
                pcm = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK, device=hw_id)
                pcm.close()
                devices.append({"id": hw_id, "name": f"{label_base} — {hw_id} (Recommended, no conversion)", "hw": True})
            except Exception:
                pass

            try:
                pcm = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK, device=plug_id)
                pcm.close()
                devices.append({"id": plug_id, "name": f"{label_base} — {plug_id} (Fallback, may convert)", "hw": False})
            except Exception:
                pass

        if not devices:
            devices.append({"id": "default", "name": "default (system default)", "hw": False})
        return devices

    def set_output_device(self, device_id: str):
        with self._lock:
            self.alsa_card = device_id
        try:
            self._save_config()
        except Exception:
            pass

    # ---------- playback control ----------
    def play(self, track_id: int):
        track = self.get_track_by_id(track_id)
        if not track:
            raise ValueError("Track not found")

        # signal any existing playback threads to stop (do NOT join here)
        try:
            old_stop = self._stop_event
            old_stop.set()
        except Exception:
            pass

        # create fresh events for new playback
        stop_event = threading.Event()
        pause_event = threading.Event()

        with self._lock:
            self._stop_event = stop_event
            self._pause_event = pause_event

            with self._seek_lock:
                self._seek_to_seconds = None

            self.current_track = track
            self._state = PlaybackState.PLAYING

            self._duration = float(track.duration) if (track.duration is not None) else 0.0
            self._position = 0.0

            # reset bit-perfect flags for new playback; will be set in _playback_loop
            self._bitperfect = False
            self._bitperfect_reason = "Opening device"

            print(f"[audio_engine] Starting playback id={track.id} path={track.path} device={self.alsa_card}", flush=True)

            # start playback thread (daemon)
            self._playback_thread = threading.Thread(
                target=self._playback_loop,
                args=(track.path, stop_event, pause_event),
                daemon=True,
            )
            self._playback_thread.start()

    def pause(self):
        try:
            with self._lock:
                self._pause_event.set()
                self._state = PlaybackState.PAUSED
        except Exception:
            pass

    def resume(self):
        try:
            with self._lock:
                self._pause_event.clear()
                self._state = PlaybackState.PLAYING
        except Exception:
            pass

    def stop(self):
        try:
            with self._lock:
                self._stop_event.set()
                self._pause_event.clear()
                self._state = PlaybackState.STOPPED
                # when explicitly stopped, clear bit-perfect state
                self._bitperfect = False
                self._bitperfect_reason = "Stopped"
        except Exception:
            pass
        print("[audio_engine] stop() requested: stop_event set", flush=True)

    def seek(self, seconds: float):
        with self._seek_lock:
            self._seek_to_seconds = float(max(0.0, seconds))
        with self._lock:
            self._position = float(max(0.0, seconds))

    def get_position(self) -> float:
        with self._lock:
            return float(self._position)

    def get_duration(self) -> float:
        with self._lock:
            if self._duration and self._duration > 0:
                return float(self._duration)
            if self.current_track and self.current_track.duration:
                return float(self.current_track.duration)
            return 0.0

    # ---------- internals: bit-perfect tracking ----------
    def _set_bitperfect_state(
        self,
        file_samplerate: int,
        file_channels: int,
        file_subtype: str,
        alsa_device: Optional[str],
        alsa_format,
    ) -> None:
        """
        Conservative bit-perfect check.

        We mark playback as bit-perfect if:
          - ALSA device is an explicit 'hw:' device,
          - File subtype is integer PCM we pass directly (PCM_16 / PCM_24 / PCM_32).

        Any resampling or format conversion beyond that would be outside
        this engine (e.g., driver/OS), but this matches the typical
        "bit-perfect pipeline" for ALSA.
        """
        with self._lock:
            self._bitperfect = False
            self._bitperfect_reason = ""

            if not alsa_device or not alsa_device.startswith("hw:"):
                self._bitperfect_reason = "Using non-hw ALSA device (likely mixed/resampled)"
                return

            if file_subtype not in ("PCM_16", "PCM_24", "PCM_32"):
                self._bitperfect_reason = f"Unsupported or non-integer subtype: {file_subtype}"
                return

            # All conditions met: pipeline is configured for bit-perfect playback.
            self._bitperfect = True
            self._bitperfect_reason = ""

    # ---------- internals: mapping / open / loop ----------
    def _map_subtype_to_alsa_format(self, subtype: str):
        if subtype == "PCM_16":
            return alsaaudio.PCM_FORMAT_S16_LE, np.int16
        elif subtype == "PCM_24":
            # present as 32-bit container commonly
            return alsaaudio.PCM_FORMAT_S32_LE, np.int32
        elif subtype == "PCM_32":
            return alsaaudio.PCM_FORMAT_S32_LE, np.int32
        else:
            raise ValueError(f"Unsupported PCM subtype: {subtype}")

    def _open_alsa_device(self, channels: int, samplerate: int, alsa_format):
        print(f"[audio_engine] Opening ALSA device {self.alsa_card} channels={channels} rate={samplerate}", flush=True)
        pcm = alsaaudio.PCM(type=alsaaudio.PCM_PLAYBACK, mode=alsaaudio.PCM_NORMAL, device=self.alsa_card)
        pcm.setchannels(channels)
        pcm.setrate(samplerate)
        pcm.setformat(alsa_format)
        pcm.setperiodsize(BUFFER_FRAMES)
        return pcm

    def _playback_loop(self, path: str, stop_event: threading.Event, pause_event: threading.Event):
        pcm = None
        try:
            print(f"[audio_engine] Playback loop started for {path} (thread={threading.get_ident()})", flush=True)

            with sf.SoundFile(path, mode="r") as f:
                channels = f.channels
                samplerate = f.samplerate
                subtype = f.subtype
                frames_total = getattr(f, "frames", None)
                if frames_total and samplerate:
                    with self._lock:
                        self._duration = float(frames_total) / float(samplerate)

                print(f"[audio_engine] File info: channels={channels}, samplerate={samplerate}, subtype={subtype}", flush=True)
                alsa_format, np_dtype = self._map_subtype_to_alsa_format(subtype)

                # attempt to open ALSA device with backoff (inside playback thread)
                max_retries = 80
                base_delay = 0.03
                for attempt in range(1, max_retries + 1):
                    if stop_event.is_set():
                        raise RuntimeError("Stop requested before device open")
                    try:
                        pcm = self._open_alsa_device(channels, samplerate, alsa_format)
                        break
                    except Exception as e:
                        wait_time = base_delay * (1 + (attempt // 20))
                        print(f"[audio_engine] ALSA open failed (attempt {attempt}/{max_retries}): {e}; retry in {wait_time:.3f}s", flush=True)
                        time.sleep(wait_time)
                if pcm is None:
                    with self._lock:
                        self._bitperfect = False
                        self._bitperfect_reason = "Unable to open ALSA device"
                    raise RuntimeError("Unable to open ALSA device for playback")

                # We now know file format + ALSA device; compute bit-perfect state.
                try:
                    self._set_bitperfect_state(
                        file_samplerate=samplerate,
                        file_channels=channels,
                        file_subtype=subtype,
                        alsa_device=self.alsa_card,
                        alsa_format=alsa_format,
                    )
                except Exception as e:
                    with self._lock:
                        self._bitperfect = False
                        self._bitperfect_reason = f"Bit-perfect check failed: {e!r}"

                # initial seek if requested
                with self._seek_lock:
                    seek_secs = self._seek_to_seconds
                    self._seek_to_seconds = None
                if seek_secs is not None:
                    try:
                        target_frame = int(seek_secs * samplerate)
                        target_frame = max(0, min(target_frame, frames_total or target_frame))
                        f.seek(target_frame)
                        with self._lock:
                            self._position = float(target_frame) / float(samplerate)
                    except Exception as e:
                        print("[audio_engine] seek failed at start:", e, flush=True)

                # main loop
                while not stop_event.is_set():
                    if pause_event.is_set():
                        time.sleep(0.05)
                        continue

                    with self._seek_lock:
                        seek_secs = self._seek_to_seconds
                        self._seek_to_seconds = None
                    if seek_secs is not None:
                        try:
                            target_frame = int(seek_secs * samplerate)
                            if frames_total:
                                target_frame = max(0, min(target_frame, frames_total - 1))
                            f.seek(target_frame)
                            with self._lock:
                                self._position = float(target_frame) / float(samplerate)
                        except Exception as e:
                            print("[audio_engine] seek error:", e, flush=True)

                    data = f.read(frames=BUFFER_FRAMES, dtype=np_dtype, always_2d=True)
                    if len(data) == 0:
                        print("[audio_engine] EOF reached", flush=True)
                        break

                    # --- VISUALIZER: emit a copy of the PCM chunk (non-blocking) ---
                    try:
                        # emit a copy so visualizer can't accidentally modify our buffer
                        self.pcm_chunk.emit(np.copy(data))
                    except Exception:
                        # swallow any signal errors — playback must continue
                        pass

                    interleaved = data.reshape(-1)
                    try:
                        pcm.write(interleaved.tobytes())
                    except Exception as e:
                        print(f"[audio_engine] PCM write error: {e}", flush=True)
                        break

                    try:
                        with self._lock:
                            self._position = float(f.tell()) / float(samplerate)
                    except Exception:
                        pass

        except Exception as e:
            import traceback
            print("[audio_engine] ERROR in playback loop:", e, flush=True)
            traceback.print_exc()
            with self._lock:
                self._bitperfect = False
                if not self._bitperfect_reason:
                    self._bitperfect_reason = "Playback error"
        finally:
            try:
                if pcm is not None:
                    try:
                        pcm.close()
                        print("[audio_engine] ALSA device closed", flush=True)
                    except Exception as e:
                        print(f"[audio_engine] Error while closing ALSA device: {e}", flush=True)
                    finally:
                        pcm = None
            except Exception:
                pass

            with self._lock:
                if self.current_track and self.current_track.path == path:
                    if not stop_event.is_set():
                        self._state = PlaybackState.IDLE
                    self.current_track = None
                    self._position = 0.0
                    # when idle with no current track, clear bit-perfect state
                    self._bitperfect = False
                    self._bitperfect_reason = "Idle"

            print("[audio_engine] Playback loop finished", flush=True)

    # ---------- status ----------
    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "current_track": self.current_track.name if self.current_track else None,
                "current_track_id": self.current_track.id if self.current_track else None,
                "alsa_card": self.alsa_card,
                "music_dir": self.music_dir,
                "position": float(self._position),
                "duration": float(self._duration),
                "bitperfect": bool(self._bitperfect),
                "bitperfect_reason": self._bitperfect_reason,
            }