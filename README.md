# Tadween Desk | تدوين

**Your meeting notes, on your PC.**

![Tadween Desk | تدوين: "Your meeting notes, on your PC." Open-source Windows app, English and Arabic, live. Beside it, a dashed box labelled This PC shows Mic and System audio feeding a live transcript with an English line, an Arabic line and a note that the mic is muted while system audio continues, then Edit, .txt and .md.](docs/images/tadween-desk-cover.png)

Tadween Desk is an open-source Windows desktop app that writes down what was discussed while you
focus on the meeting. It transcribes English and Arabic live from two sources: your microphone
and your PC's system audio.

*Tadween* (تدوين) is Arabic for "writing things down".

![Three steps inside a dashed box labelled This PC: Mic + system audio, then Transcribe on this PC, then Edit and export. Below it, separated by a dashed amber line: Optional, Generate MoM, to OpenAI, only when you choose.](docs/images/tadween-desk-workflow.png)

- **Transcription runs on your PC.** After a one-time setup, speech recognition runs locally.
  The app does not save or upload meeting audio.
- **Edit and export.** Every meeting is saved in History on your PC. Correct or redact lines,
  then use **Save As** to write a TXT or Markdown file.
- **Minutes with OpenAI are optional.** Nothing is sent unless you add your own OpenAI API key
  and press **Generate MoM** for a meeting. Only then does the app send that meeting's saved
  transcript and related details (title, date, time zone, line timestamps, source labels and gap
  or recognition markers) to OpenAI. Audio is never sent. See [Privacy](#privacy).

> **Status: early source release.** It targets Windows 10/11 (64-bit) and has been tested on
> Windows 11. You run it from source with Python; there is no installer or program file.

## What it does

- **Live English and Arabic transcription.** Tadween Desk listens to two sources at once:
  your **microphone** and your PC's **system audio** (what Teams, Zoom, Meet or any other app
  plays through the output you pick). It transcribes both, keeps Arabic in Arabic script and
  handles meetings that switch between the two languages.
- **Mic mute while system audio continues.** **Mute mic** (or `Ctrl+M`) excludes the
  selected microphone source from transcription. Pending mic audio is discarded before it
  reaches speech recognition. The other participants (system audio) keep being transcribed,
  and the transcript shows exactly when the mic was excluded. This only affects Tadween Desk;
  it does not mute you in your meeting app. Your voice can still reach system audio through
  sidetone, mic monitoring ("Listen to this device") or echo from speakers, so use **Pause
  all** when both sources need to stop, and prefer headphones.
- **Editable History.** Every meeting is saved automatically on your PC as it happens. Open
  it later from **History** to search it, rename it, correct a line (the original recognition
  stays in the line's edit history), redact text or delete the meeting. You can choose how
  long History keeps meetings.
- **Save As TXT or Markdown.** **Save As…** writes the current saved version of the
  transcript to a `.txt` or `.md` file wherever you choose.
- **Optional AI minutes.** If you add your own OpenAI API key, **Generate MoM** drafts
  minutes and action items from a saved transcript, with links back to the lines they came
  from. This is the only feature that sends anything off your PC; see [Privacy](#privacy).

![The real Tadween Desk live view of a fictional meeting. Callout 1 marks the English and Arabic transcript lines, each labelled Mic or System, with Arabic written right to left. Callout 2 marks the mic excluded (muted in this app) while the System audio source keeps transcribing and a new System line arrives.](docs/images/live-transcript-annotated.png)

![The same fictional meeting saved in History. Callout 3 marks the Save As button and the notice confirming that a Markdown copy was saved while History is unchanged.](docs/images/saved-transcript-annotated.png)

*Screenshots of the real app with a fictional demo meeting (rendered by
`app/scripts/demo_screenshots.py` with synthetic audio devices and a scripted recognizer).*

## Privacy

- **Live transcription runs on your PC, and the app does not save or upload meeting
  audio.** Speech recognition (Qwen3-ASR 0.6B or 1.7B, run by a local
  [llama.cpp](https://github.com/ggml-org/llama.cpp) server that only listens on
  `127.0.0.1`) and voice detection (Silero VAD) run on your CPU.
- **No audio at rest.** The app does not write recordings or temporary audio files. Audio
  waits in small, bounded memory buffers until it is transcribed.
- **Your transcripts stay local.** History is a SQLite database in
  `%LOCALAPPDATA%\MeetingMinutes` (the app's internal name). It is not encrypted by the app,
  so protect your Windows account and disk (for example with BitLocker).
- **OpenAI minutes are opt-in and on demand.** Nothing is sent unless you save an OpenAI API
  key *and* press **Generate MoM** for a meeting. If you choose Generate MoM, the app sends
  the selected meeting's saved transcript and related details to OpenAI, including the
  title, date, time zone, line timestamps, source labels and gap or recognition markers.
  Audio is never sent. The request asks OpenAI not to store the response
  (`store: false`); OpenAI's own data policies still apply. Your key is kept in Windows
  Credential Manager, not in the app's files. Before sending, the app shows its own cost
  estimate (not an OpenAI bill), and an app-side limit on that estimate (default US$1.00 per
  generation) blocks larger requests.
- **No telemetry and no automatic updates.**

## Quick start (Windows, from a fresh clone)

You need:

- Windows 10 or 11, 64-bit, with about 3 GB of free disk space (the Python environment
  and 1.1 GB of models), or about 4.5 GB with the larger speech model;
- [Python 3.11](https://www.python.org/downloads/windows/) (64-bit) with the `py` launcher.
  The dependency locks are made for 3.11 only;
- [Git](https://git-scm.com/download/win), or download the repository as a ZIP;
- internet access for the setup steps only. After setup, the app runs offline.

Run these in PowerShell:

```powershell
git clone https://github.com/muhammedts/tadween-desk.git
cd tadween-desk

# 1. Python environment in app\.venv, from the hash-locked dependency lists (downloads from PyPI)
powershell -ExecutionPolicy Bypass -File app\scripts\bootstrap.ps1

# 2. Speech models into app\models: shows the size (about 1.04 GB) and destination, asks,
#    then checks every file's size and SHA-256. Files that already match are skipped.
#    For the larger speech model, add -Model 1.7B (see "Choosing the speech model" below).
.\"Install Models.cmd"

# 3. Start the app
.\"Start Tadween Desk.cmd"
```

On a slow connection, a download can time out; run the same command again. The bootstrap
reuses what pip already downloaded, and the model installer resumes partial downloads
(with Windows' built-in `curl.exe`).
You can also double-click `Install Models.cmd` and `Start Tadween Desk.cmd` in Explorer.
If the app does not open, use `Start Tadween Desk (troubleshooting).cmd`, which keeps a
console window open with any error.

In the app, choose your microphone and the output you hear the meeting on, then press
**Start**. Headphones give the best results: with speakers, your microphone also picks up
the other participants.

### Choosing the speech model

Tadween Desk runs one of two sizes of the same speech model. Install the one you want:

| Model | Install with | Download | Good for |
|---|---|---|---|
| Qwen3-ASR 0.6B (default) | `.\"Install Models.cmd"` | 1.02 GB | Most laptops. Faster, and uses less memory. |
| Qwen3-ASR 1.7B | `.\"Install Models.cmd" -Model 1.7B` | 2.52 GB | Better accuracy, most of all for Arabic. About twice as slow on a CPU, and needs about 3 GB of free memory. |

The app has no model setting: it uses the model that is installed, and shows its name in
**Settings** and at the top of every saved transcript.

- **Only one installed:** that one is used.
- **Both installed** (`-Model Both`, or one after the other): the app uses 0.6B. To use
  1.7B, delete the folder `app\models\stt\qwen3-asr-0.6b-q8` and start the app again.
  `Install Models.cmd` brings the 0.6B model back whenever you want it.

On a slower laptop the 1.7B model can fall behind in a busy meeting: the text then arrives
later, and the transcript notes any stretch that had to be skipped. If that happens, go
back to 0.6B.

### What `Install Models.cmd` installs

All sources and versions are pinned in [`app/models/manifest.json`](app/models/manifest.json)
(each file's URL, revision, byte size and SHA-256). The release archive is listed in
[`app/models/acquisition.json`](app/models/acquisition.json).

| Asset | Source | Download | Licence |
|---|---|---|---|
| Qwen3-ASR 0.6B, Q8_0 GGUF + audio projector | [ggml-org/Qwen3-ASR-0.6B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-0.6B-GGUF) @ `928ab95` | 1.02 GB | Apache-2.0 |
| Qwen3-ASR 1.7B, Q8_0 GGUF + audio projector (only with `-Model 1.7B` or `-Model Both`) | [ggml-org/Qwen3-ASR-1.7B-GGUF](https://huggingface.co/ggml-org/Qwen3-ASR-1.7B-GGUF) @ `36a6786` | 2.52 GB | Apache-2.0 |
| llama.cpp b11960 `llama-server`, Windows x64 CPU | [ggml-org/llama.cpp release b11960](https://github.com/ggml-org/llama.cpp/releases/tag/b11960) | 24 MB zip | MIT |
| Silero VAD (ONNX) | [k2-fsa/sherpa-onnx `asr-models`](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models) | 0.6 MB | MIT |

Options:

```powershell
.\"Install Models.cmd" -Model 1.7B             # the larger speech model instead of 0.6B (or -Model Both)
.\"Install Models.cmd" -DryRun                 # only show what is installed and what would be downloaded
.\"Install Models.cmd" -From D:\my-downloads   # use files you downloaded yourself (checked the same way)
```

**Placing the files by hand.** Download the files listed in the manifest (the `source_url`
of each entry with role `stt` (or `stt-alt` for the 1.7B model), `vad`, and the llama.cpp
zip) into one folder, then run `Install Models.cmd -From <that folder>` (with `-Model 1.7B`
for the larger model). The installer verifies them and puts each in place.
You can also copy them yourself. The app expects:

```text
app\models\stt\qwen3-asr-0.6b-q8\Qwen3-ASR-0.6B-Q8_0.gguf
app\models\stt\qwen3-asr-0.6b-q8\mmproj-Qwen3-ASR-0.6B-Q8_0.gguf
   (or, for the 1.7B model: app\models\stt\qwen3-asr-1.7b-q8\Qwen3-ASR-1.7B-Q8_0.gguf and mmproj-Qwen3-ASR-1.7B-Q8_0.gguf)
app\models\runtime\llama.cpp-b11960-win-cpu-x64\  (every file the manifest lists for this entry, from the zip)
app\models\vad\silero_vad.onnx
```

Run `Install Models.cmd` again afterwards: it only checks files that are already in place.
Before the speech engine first starts in a session, the app itself checks every file's size
and SHA-256. A missing or changed file stops it with a setup message; the app never
downloads anything itself. `llama-server` needs the Microsoft Visual C++ runtime. If
Windows does not have it, the installer copies it next to `llama-server` from the PySide6
package in `app\.venv`. Otherwise, install the "Microsoft Visual C++ Redistributable
2015–2022 (x64)".

### Check the setup

From the `app` folder:

```powershell
.\.venv\Scripts\python.exe -m meeting_minutes app --check     # starts the speech engine offscreen with synthetic devices, then stops it
.\.venv\Scripts\python.exe -m meeting_minutes doctor --no-capture
.\.venv\Scripts\python.exe -m pytest                           # developer tests; no network, no audio devices
```

## Optional: AI minutes with OpenAI

1. Open **Settings and help**, find **OpenAI minutes (optional)** and paste your OpenAI API
   key. Saving the key makes no request.
2. Optionally set the minutes language and the spending limit. If you change the model,
   also enter its prices: the app knows only the default model's prices, and without
   prices it cannot estimate the cost and refuses to send while a limit is set.
3. Open a saved meeting in History and press **Generate MoM**. Check the estimate, then
   confirm.

Minutes are **drafts** until you approve them. Each point links to the transcript lines it
is based on, unknown owners and dates stay empty instead of being guessed, and you can edit
them; an export says whether it is a draft. Correcting the transcript marks the minutes as
out of date; redacting text or deleting the meeting removes them.
