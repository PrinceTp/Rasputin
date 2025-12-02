# app/audio_engine.py
import os
import glob
import threading
import time
from typing import List, Optional

import numpy as np
import soundfile as sf
import alsaaudio
import mutagen
from mutagen.flac import FLAC

from .config import MUSIC_DIR, ALSA_CARD, BUFFER_FRAMES


class TrackInfo:
    """
    Holds path + metadata + album art bytes + reliable duration extraction.
    """
    def __init__(self, track_id: int, path: str):
        self.id = track_id
        self.path = path
        self.name = os.path.basename(path)

        # Metadata fields (populated by _read_metadata)
        self.title: Optional[str] = None
        self.artist: Optional[str] = None
        self.album: Optional[str] = None
        self.sample_rate: Optional[int] = None
        self.bit_depth: Optional[int] = None
        self.channels: Optional[int] = None
        self.duration: Optional[float] = None  # seconds

        # Raw album art bytes (None if missing)
        self.album_art: Optional[bytes] = None

        self._read_metadata()

    def _read_metadata(self):
        try:
            a = mutagen.File(self.path)
            if a is None:
                # fallback: set defaults and try soundfile for duration
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

                # FLAC embedded art
                if isinstance(a, FLAC):
                    if a.pictures:
                        try:
                            self.album_art = a.pictures[0].data
                        except Exception:
                            pass

                # ID3/APIC generic
                if not self.album_art and tags:
                    apic = tags.get("APIC:")
                    if apic:
                        try:
                            self.album_art = apic.data
                        except Exception:
                            pass

                # folder fallback
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
            # non-fatal
            print(f"[audio_engine] Warning: metadata read error {self.path}: {e}", flush=True)
            if not self.title:
                self.title = self.name
            if not self.artist:
                self.artist = "Unknown Artist"
            if not self.album:
                self.album = "Unknown Album"

        # Fallback: if duration missing, use soundfile to probe frames/samplerate
        if self.duration is None:
            try:
                with sf.SoundFile(self.path) as sfh:
                    frames = sfh.frames
                    sr = sfh.samplerate
                    if frames and sr:
                        self.duration = frames / float(sr)
                        # set sample_rate / channels if missing
                        if not self.sample_rate:
                            self.sample_rate = sr
                        if not self.channels:
                            self.channels = sfh.channels
            except Exception:
                # if even this fails, leave duration None
                pass


class PlaybackState:
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class AudioEngine:
    """
    Bit-perfect playback via ALSA + soundfile; dynamic music_dir and ALSA selection.
    """
    def __init__(self, music_dir: str = MUSIC_DIR, alsa_card: str = ALSA_CARD):
        self.music_dir = music_dir
        self.alsa_card = alsa_card

        self.tracks: List[TrackInfo] = []
        self.current_track: Optional[TrackInfo] = None

        self._playback_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        self._state = PlaybackState.IDLE
        self._lock = threading.Lock()

        self._scan_library()

    # ---------- Library ----------
    def _scan_library(self):
        patterns = ("*.flac", "*.wav")
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
        self.music_dir = music_dir
        self._scan_library()

    # ---------- ALSA ----------
    def list_alsa_devices(self):
        devices = []
        for idx, name in enumerate(alsaaudio.cards()):
            dev_id = f"hw:{idx},0"
            try:
                alsaaudio.PCM(device=dev_id)
                devices.append({"id": dev_id, "name": name})
            except Exception:
                continue
        return devices

    def set_output_device(self, device_id: str):
        with self._lock:
            self.alsa_card = device_id

    # ---------- Playback ----------
    def play(self, track_id: int):
        track = self.get_track_by_id(track_id)
        if not track:
            raise ValueError("Track not found")

        with self._lock:
            self._stop_event.set()
            if self._playback_thread and self._playback_thread.is_alive():
                self._playback_thread.join()

            self._stop_event.clear()
            self._pause_event.clear()

            self.current_track = track
            self._state = PlaybackState.PLAYING

            print(f"[audio_engine] Starting playback id={track.id} path={track.path} device={self.alsa_card}", flush=True)
            self._playback_thread = threading.Thread(target=self._playback_loop, args=(track.path,), daemon=True)
            self._playback_thread.start()

    def pause(self):
        with self._lock:
            if self._state == PlaybackState.PLAYING:
                self._pause_event.set()
                self._state = PlaybackState.PAUSED

    def resume(self):
        with self._lock:
            if self._state == PlaybackState.PAUSED:
                self._pause_event.clear()
                self._state = PlaybackState.PLAYING

    def stop(self):
        with self._lock:
            self._stop_event.set()
            self._pause_event.clear()
            self._state = PlaybackState.STOPPED
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join()

    # ---------- Playback loop ----------
    def _map_subtype_to_alsa_format(self, subtype: str):
        if subtype == "PCM_16":
            return alsaaudio.PCM_FORMAT_S16_LE, np.int16
        elif subtype == "PCM_24":
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

    def _playback_loop(self, path: str):
        try:
            print(f"[audio_engine] Playback loop started for {path}", flush=True)
            with sf.SoundFile(path, mode="r") as f:
                channels = f.channels
                samplerate = f.samplerate
                subtype = f.subtype

                print(f"[audio_engine] File info: channels={channels}, samplerate={samplerate}, subtype={subtype}", flush=True)
                alsa_format, np_dtype = self._map_subtype_to_alsa_format(subtype)
                pcm = self._open_alsa_device(channels, samplerate, alsa_format)

                while not self._stop_event.is_set():
                    if self._pause_event.is_set():
                        time.sleep(0.05)
                        continue

                    data = f.read(frames=BUFFER_FRAMES, dtype=np_dtype, always_2d=True)
                    if len(data) == 0:
                        print("[audio_engine] EOF reached", flush=True)
                        break

                    interleaved = data.reshape(-1)
                    pcm.write(interleaved.tobytes())

        except Exception as e:
            import traceback
            print("[audio_engine] ERROR in playback loop:", e, flush=True)
            traceback.print_exc()
        finally:
            with self._lock:
                if not self._stop_event.is_set():
                    self._state = PlaybackState.IDLE
                self.current_track = None
            print("[audio_engine] Playback loop finished", flush=True)

    # ---------- Status ----------
    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "current_track": self.current_track.name if self.current_track else None,
                "current_track_id": self.current_track.id if self.current_track else None,
                "alsa_card": self.alsa_card,
                "music_dir": self.music_dir,
            }
