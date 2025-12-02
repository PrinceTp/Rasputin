# app/audio_engine.py

import os
import glob
import threading
import time
from typing import List, Optional

import numpy as np
import soundfile as sf
import alsaaudio

from .config import MUSIC_DIR, ALSA_CARD, BUFFER_FRAMES


class TrackInfo:
    def __init__(self, track_id: int, path: str):
        self.id = track_id
        self.path = path
        self.name = os.path.basename(path)


class PlaybackState:
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class AudioEngine:
    """
    Bit-perfect playback engine using ALSA + soundfile.
    - Decodes FLAC/WAV with soundfile (libsndfile)
    - Streams raw PCM to ALSA hw device (exclusive)
    - Runs playback in a background thread
    """

    def __init__(self, music_dir: str = MUSIC_DIR, alsa_card: str = ALSA_CARD):
        self.music_dir = music_dir
        self.alsa_card = alsa_card  # current output device (e.g. "hw:1,0")

        self.tracks: List[TrackInfo] = []
        self.current_track: Optional[TrackInfo] = None

        self._playback_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        self._state = PlaybackState.IDLE
        self._lock = threading.Lock()

        self._scan_library()

    # ---------- Library management ----------

    def _scan_library(self):
        patterns = ("*.flac", "*.wav")
        files: list[str] = []
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

    # ---------- ALSA output helpers ----------

    def _map_subtype_to_alsa_format(self, subtype: str):
        """
        Map libsndfile subtype to ALSA PCM format.
        We keep it simple and handle common PCM types.
        """
        if subtype == "PCM_16":
            return alsaaudio.PCM_FORMAT_S16_LE, np.int16
        elif subtype == "PCM_24":
            # Use 32-bit container; top 24 bits carry audio
            return alsaaudio.PCM_FORMAT_S32_LE, np.int32
        elif subtype == "PCM_32":
            return alsaaudio.PCM_FORMAT_S32_LE, np.int32
        else:
            raise ValueError(f"Unsupported PCM subtype for bit-perfect: {subtype}")

    def _open_alsa_device(self, channels: int, samplerate: int, alsa_format):
        print(
            f"[ENGINE] Opening ALSA device {self.alsa_card} "
            f"channels={channels} rate={samplerate}",
            flush=True,
        )
        pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_PLAYBACK,
            mode=alsaaudio.PCM_NORMAL,
            device=self.alsa_card,  # <- current selected device
        )
        pcm.setchannels(channels)
        pcm.setrate(samplerate)
        pcm.setformat(alsa_format)
        pcm.setperiodsize(BUFFER_FRAMES)
        return pcm

    # ---------- ALSA device selection ----------

    def list_alsa_devices(self):
        devices = []
        try:
            cards = alsaaudio.cards()
        except Exception as e:
            print("[ENGINE] ERROR listing ALSA cards:", e, flush=True)
            return devices

        for card_index, card_name in enumerate(cards):
            # Probe several device indices for each card (0–7 is plenty)
            for dev_index in range(8):
                dev_id = f"hw:{card_index},{dev_index}"
                try:
                    pcm = alsaaudio.PCM(
                        type=alsaaudio.PCM_PLAYBACK,
                        mode=alsaaudio.PCM_NORMAL,
                        device=dev_id,
                    )
                    # If we reached here, it's a valid playback device
                    devices.append({
                        "id": dev_id,
                        "name": f"{card_name} (dev {dev_index})",
                        "card_index": card_index,
                    })
                    pcm.close()
                except Exception:
                    # Not a valid playback device, ignore
                    continue

        print("[ENGINE] ALSA devices detected:", devices, flush=True)
        return devices


    def set_output_device(self, device_id: str):
        """
        Change the ALSA hw device (e.g. "hw:1,0").
        This will affect the next playback.
        """
        with self._lock:
            print(f"[ENGINE] Changing ALSA device to {device_id}", flush=True)
            self.alsa_card = device_id

    # ---------- Public playback controls ----------

    def play(self, track_id: int):
        track = self.get_track_by_id(track_id)
        if not track:
            raise ValueError("Track not found")

        with self._lock:
            # stop current playback if any
            self._stop_event.set()
            if self._playback_thread and self._playback_thread.is_alive():
                self._playback_thread.join()

            self._stop_event.clear()
            self._pause_event.clear()

            self.current_track = track
            self._state = PlaybackState.PLAYING

            print(
                f"[ENGINE] Starting playback: id={track.id}, "
                f"path={track.path}, alsa_card={self.alsa_card}",
                flush=True,
            )

            self._playback_thread = threading.Thread(
                target=self._playback_loop,
                args=(track.path,),
                daemon=True,
            )
            self._playback_thread.start()

    def pause(self):
        with self._lock:
            if self._state == PlaybackState.PLAYING:
                self._pause_event.set()
                self._state = PlaybackState.PAUSED
                print("[ENGINE] Pause requested", flush=True)

    def resume(self):
        with self._lock:
            if self._state == PlaybackState.PAUSED:
                self._pause_event.clear()
                self._state = PlaybackState.PLAYING
                print("[ENGINE] Resume requested", flush=True)

    def stop(self):
        with self._lock:
            self._stop_event.set()
            self._pause_event.clear()
            self._state = PlaybackState.STOPPED
            print("[ENGINE] Stop requested", flush=True)
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join()

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "current_track": self.current_track.name if self.current_track else None,
                "current_track_id": self.current_track.id if self.current_track else None,
                "alsa_card": self.alsa_card,
            }

    # ---------- Internal playback loop ----------

    def _playback_loop(self, path: str):
        try:
            print(f"[ENGINE] Playback loop started for: {path}", flush=True)
            with sf.SoundFile(path, mode="r") as f:
                channels = f.channels
                samplerate = f.samplerate
                subtype = f.subtype

                print(
                    f"[ENGINE] File info: channels={channels}, "
                    f"samplerate={samplerate}, subtype={subtype}",
                    flush=True,
                )

                alsa_format, np_dtype = self._map_subtype_to_alsa_format(subtype)
                pcm = self._open_alsa_device(channels, samplerate, alsa_format)

                while not self._stop_event.is_set():
                    if self._pause_event.is_set():
                        time.sleep(0.05)
                        continue

                    data = f.read(frames=BUFFER_FRAMES, dtype=np_dtype, always_2d=True)
                    if len(data) == 0:
                        print("[ENGINE] Reached EOF", flush=True)
                        break  # EOF

                    interleaved = data.reshape(-1)
                    pcm.write(interleaved.tobytes())
        except Exception as e:
            import traceback
            print("[ENGINE] ERROR in playback loop:", e, flush=True)
            traceback.print_exc()
        finally:
            with self._lock:
                if not self._stop_event.is_set():
                    self._state = PlaybackState.IDLE
                self.current_track = None
            print("[ENGINE] Playback loop finished", flush=True)
