#!/usr/bin/env python3

import os
import sys
import glob
import numpy as np
import soundfile as sf
import alsaaudio

MUSIC_DIR = "/home/prince/projects/test"  
ALSA_CARD = "hw:0,0"
BUFFER_FRAMES = 2048                      # playback chunk size in frames
# ===================================


def list_audio_files(folder):
    patterns = ("*.flac", "*.wav")
    files = []
    for p in patterns:
        files.extend(glob.glob(os.path.join(folder, p)))
        files.extend(glob.glob(os.path.join(folder, "**", p), recursive=True))
    files = sorted(set(files))
    return files


def pick_file(files):
    if not files:
        print("No FLAC/WAV files found in:", MUSIC_DIR)
        sys.exit(1)

    print("\nAvailable tracks:\n")
    for i, f in enumerate(files):
        print(f"[{i}] {os.path.basename(f)}")

    choice = input("\nEnter track number: ").strip()
    try:
        idx = int(choice)
        return files[idx]
    except (ValueError, IndexError):
        print("Invalid choice.")
        sys.exit(1)


def map_subtype_to_alsa_format(subtype):
    """
    Map libsndfile subtype to ALSA PCM format.
    We handle the most common PCM cases used in FLAC/WAV rips.
    """
    # subtype examples: 'PCM_16', 'PCM_24', 'PCM_32'
    if subtype == "PCM_16":
        return alsaaudio.PCM_FORMAT_S16_LE, np.int16
    elif subtype == "PCM_24":
        # We'll use 32-bit container; ALSA will send full 24 bits to the DAC.
        return alsaaudio.PCM_FORMAT_S32_LE, np.int32
    elif subtype == "PCM_32":
        return alsaaudio.PCM_FORMAT_S32_LE, np.int32
    else:
        raise ValueError(f"Unsupported PCM subtype for bit-perfect: {subtype}")


def open_alsa_device(channels, samplerate, alsa_format):
    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_PLAYBACK,
        mode=alsaaudio.PCM_NORMAL,
        device=ALSA_CARD,
        channels=channels,
        rate=samplerate,
        format=alsa_format,
        periodsize=BUFFER_FRAMES,
    )
    return pcm


def play_file_bitperfect(path):
    print(f"\nPlaying: {path}\n")

    with sf.SoundFile(path, mode="r") as f:
        channels = f.channels
        samplerate = f.samplerate
        subtype = f.subtype  # e.g. 'PCM_16'

        print(f"Channels   : {channels}")
        print(f"Sample rate: {samplerate} Hz")
        print(f"Subtype    : {subtype}")

        alsa_format, np_dtype = map_subtype_to_alsa_format(subtype)
        pcm = open_alsa_device(channels, samplerate, alsa_format)

        while True:
            data = f.read(frames=BUFFER_FRAMES, dtype=np_dtype, always_2d=True)
            if len(data) == 0:
                break  # EOF

            interleaved = data.reshape(-1)
            pcm.write(interleaved.tobytes())

    print("\nDone.\n")


def main():
    if not os.path.isdir(MUSIC_DIR):
        print("MUSIC_DIR does not exist:", MUSIC_DIR)
        sys.exit(1)

    files = list_audio_files(MUSIC_DIR)
    track = pick_file(files)
    play_file_bitperfect(track)


if __name__ == "__main__":
    main()
