"""Synthetic English speech from the Windows SAPI voices, returned in memory only.

The synthesizer writes 16 kHz mono 16-bit PCM into a .NET MemoryStream and the bytes
come back over the PowerShell process's stdout; no audio file is created. Only the
English voices in `VOICES` are used (no Arabic SAPI voice is assumed), so this is a
controlled English fixture, not a dialect or quality corpus.
"""

from __future__ import annotations

import base64
import functools
import subprocess
import sys

import numpy as np

VOICES = ("Microsoft Zira Desktop", "Microsoft David Desktop")


@functools.cache
def available_voices() -> tuple[str, ...]:
    if sys.platform != "win32":
        return ()
    script = ("Add-Type -AssemblyName System.Speech;"
              "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | "
              "% { $_.VoiceInfo.Name }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(v for v in VOICES if v in out.splitlines())


@functools.cache
def speak(text: str, voice: str = VOICES[0], rate: int = 0) -> np.ndarray:
    """16 kHz mono float32 speech for `text` (letters, digits and basic punctuation only)."""
    if any(c in text for c in "'\"`$;"):
        raise ValueError("keep fixture text free of quoting characters")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SelectVoice('{voice}');$s.Rate={int(rate)};"
        "$m=New-Object System.IO.MemoryStream;"
        "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono);"
        "$s.SetOutputToAudioStream($m,$f);"
        f"$s.Speak('{text}');"
        "[Convert]::ToBase64String($m.ToArray())"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=120, check=True).stdout
    pcm = np.frombuffer(base64.b64decode(out.strip()), dtype=np.int16)
    return (pcm.astype(np.float32) / 32768.0)
