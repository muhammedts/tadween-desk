# Developing Tadween Desk | تدوين

This guide is for people who change, test or package the app. For installing and
running it, start with the README in the repository root.

All application commands run from the `app\` folder with the project virtual
environment (`app\.venv`, CPython 3.11, 64-bit Windows), created by
`app\scripts\bootstrap.ps1` from the hash-locked requirements.

## Repository layout

```
app/
  src/meeting_minutes/   the application package (internal name, see below)
    capture/             WASAPI microphone and playback-loopback capture, the capture
                         controller, the mic privacy boundary (gate.py), a fake backend
    audio/               bounded per-source PCM queue
    domain/              contracts, events, states, privacy state, transcript types
    speech/              resampling, VAD segmenter, speech pipeline, Qwen3-ASR client
                         (qwen.py), Silero VAD adapter (sherpa.py), boundary de-duplication
    models/              model manifest loading and local asset checks
    storage/             SQLite transcript store, schema, full-text search
    session/             one meeting: capture -> speech -> saved transcript
    minutes/             optional OpenAI minutes: provider, schema, credentials, service
    ui/                  Qt (PySide6) desktop window, widgets, theme, wording
    diagnostics/         `doctor` environment report
    cli.py, export.py, retention.py, notices.py, selftest.py
  tests/
    unit/  ui/  smoke/  privacy/  integration/   (fixtures/README.md: fixture rules)
  scripts/
    bootstrap.ps1        Python environment setup (app\.venv) from the hash locks
    install_models.ps1   model installer behind "Install Models.cmd"
    demo_screenshots.py  renders the README screenshots with a fictional meeting
  models/manifest.json   pinned model and runtime files (URLs, revisions, sizes, SHA-256)
  models/acquisition.json  the release archive the runtime files come from
docs/images/             README images
```

Model files themselves are not in the repository. They are installed under
`app\models\` in the `install_subdir` named by each manifest entry.

## Data flow

```
selected microphone ─┐                               ┌─ resample to 16 kHz ─ Silero VAD segmenter ─┐
                     ├─ bounded per-source queues ───┤                                             ├─ Qwen3-ASR ─ commit ─ transcript store
selected playback ───┘   (privacy gate on the mic)   └─ resample to 16 kHz ─ Silero VAD segmenter ─┘  (llama-server
endpoint (loopback)                                                                                    on 127.0.0.1)
```

1. **Capture** (`capture/wasapi.py`, `capture/controller.py`). The microphone and the
   playback endpoint are captured independently through WASAPI (PyAudioWPatch); a render
   endpoint is captured through its exactly-matching loopback device. The native callback
   only copies the buffer into an owned frame and hands it to the controller's gate; it
   does no I/O or inference. Choosing a playback endpoint does not tell the app which
   device a meeting app is actually using.
2. **Bounded queues** (`audio/bounded_queue.py`). `put` never blocks and never spools to
   disk. When a byte or duration cap would be exceeded, the oldest whole frames are dropped
   and recorded as an explicit gap. A global owned-PCM budget caps all sources together.
3. **Speech pipeline** (`speech/pipeline.py`). Each source has its own resampler (soxr),
   VAD and segmenter state, and a new state is started for every new epoch, privacy
   generation, format or sample discontinuity. The segmenter (`speech/segmenter.py`) cuts
   speech into bounded chunks (soft cut at 8 s, forced cut at 12 s with a short overlap
   that `speech/dedup.py` removes at the boundary). Waiting chunks are bounded per source;
   overflow becomes an `stt_backlog` gap.
4. **Recognition** (`speech/qwen.py`). The app starts its own `llama-server` child process
   from verified local files, bound to `127.0.0.1` on an ephemeral port with a random
   per-run key, inside a Windows job object so it cannot outlive the app. Each chunk is
   sent as an in-memory WAV over loopback with proxies disabled and prompt caching off.
   A reply cut off at `max_tokens` is recorded as a recognition failure, not as text.
5. **Commit** (`session/meeting.py`, `storage/store.py`). Finals pass the privacy commit
   check, are queued to a single writer thread and saved in SQLite transactions.

Source labels ("Mic" / "System") say which capture source the text came from. They are
not speaker identification.

## Invariants and why they exist

**Mic mute gates audio, not text.** `capture/gate.py` (`PrivacyBoundary`) serialises the
points where mic audio or its results move forward: `admit` (callback into the queue),
`forward` (queue to the pipeline), `dispatch` (a chunk claimed for recognition) and
`commit` (a result written). Muting (`change`) increments the privacy generation and,
under the same lock, clears queued and downstream mic audio, the open utterance,
pre-roll and waiting chunks, and closes the mic stream. Any frame, chunk or result
stamped with an older generation is rejected, including a decode already in flight that
finishes after the mute. Unmuting opens a new mic epoch with fresh state, so nothing
from before or during the mute is replayed. The state persists through pause/resume,
device switches and reconnects. System audio is always admitted and keeps transcribing.
This is a transcription control; it does not mute the user in Teams, Zoom or Meet.

Keep the rule: `forward` delivers while holding the lock, so downstream `accept` must
only copy into its own bounded queue. Slow work happens outside the lock and is
re-checked at `commit`.

**No audio at rest.** Capture, the pipeline and the recognizer client keep audio only in
bounded memory. Chunks are encoded to WAV in memory for the loopback request and not kept.
Test audio is generated in memory as well (`tests/fixtures/README.md`).

**Everything is bounded, and loss is explicit.** Queue overflow, budget overflow, device
loss, recognition failure and audio still undrained at stop are recorded as gaps with
exact capture-time bounds. Pause and mic mute are intentional states, not loss.

**Saving and settling** (`session/meeting.py`). If the store refuses a final, capture
pauses, the text is kept in memory marked NOT SAVED (up to a cap) and retried on every
writer cycle; capture resumes only when the user presses Resume and saving works. Stop
runs within one overall time budget: close capture, finish speech, save the remaining
text and coverage. The meeting is `stopped` only if everything drained and saved,
otherwise `interrupted`. `save_settled` tells the window when no final save can still be
writing.

**Revisions and corrections** (`storage/store.py`). Every change is one `BEGIN IMMEDIATE`
transaction. The original recognized text is revision 1; a user correction appends a new
revision and increments the meeting's `transcript_revision`. Only current text is indexed
for search. Redaction and deletion remove the text, all revisions and index entries, then
merge the FTS index and truncate the WAL so the text does not linger in the files.

**Crash recovery.** At startup `recover_unfinished` marks meetings left running or
stopping as `interrupted` with a notice at the last known transcript time. Capture is
never reopened and lost audio is not reconstructed.

**Exports are of one saved revision** (`export.py`). Save As builds TXT or Markdown from a
single consistent store snapshot, never from what the window shows, and states the saved
revision. Text still held in memory as NOT SAVED goes into a separate, labelled section.
The file is written to a temporary file in the target folder and replaces the target
only when complete, so a failed write leaves an existing file untouched.

**Uncertain recognition is marked, never deleted.** `speech/qwen.py` flags a line when the
model's mean token probability falls below a per-model floor (`CONFIDENCE_PROFILES`: the
0.6B default uses 0.70; the pinned 1.7B alternative uses 0.75 and also marks lines recognized
in a language other than English or Arabic). Flags are saved with the segment and never
change whether the transcript is complete.

**Model files are verified, never downloaded by the app.** `models/assets.py` checks each
required role (`stt`, `stt-runtime`, `vad`) against `app/models/manifest.json`: file
present, exact byte count and SHA-256 (hashed once per run before the server starts). A
missing or mismatched file gives a setup-required state with content-free codes; there is
no fallback model. Model installation is a separate, explicit step (`Install Models.cmd`).
`models/manifest.py` keeps shippability (licences, provenance) as a separate question
from local readiness.

**The only network use is optional and user-triggered.** Transcription is entirely
on-device. The optional minutes feature (`minutes/`) sends saved transcript text of the
selected revision to the OpenAI Responses API only when the user asks to generate. The
API key is stored in Windows Credential Manager (`minutes/credentials.py`), never logged
or put in errors. Before sending, the app shows its own conservative cost estimate and
refuses the request locally if it exceeds the user's budget limit
(`minutes/provider.py: estimate_cost`, `check_budget`). Requests use `store: false`, and
redirects are refused so the key cannot be forwarded. Generated minutes are saved as a
new draft version; a redaction or deletion of the meeting invalidates them.

## Running tests

```powershell
cd app
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest tests\unit          # one folder
.\.venv\Scripts\python.exe -m pytest tests\privacy -q
```

- `tests\conftest.py` blocks `socket.socket`, `create_connection` and `getaddrinfo` for
  every test; a test that tries to reach the network fails.
- UI tests set `QT_QPA_PLATFORM=offscreen`, so no window appears.
- Warnings are errors and `xfail` is strict (`pyproject.toml`).
- `tests\integration\test_real_speech_models.py` runs the legacy Whisper small recognizer
  (manifest role `stt-legacy`, which `Install Models.cmd` does not install; place its files
  by hand from the manifest) and needs a Windows SAPI English voice; without them it is
  skipped with a `NOT RUN` reason. Test
  speech is synthesized in memory and never written to disk.

## Developer CLI

Run from `app\` as `.\.venv\Scripts\python.exe -m meeting_minutes <command>`:

| Command | What it does |
|---|---|
| `doctor --no-capture [--json] [--out FILE] [--manifest FILE]` | Environment and model-asset report. Never opens audio devices; `--no-capture` is required. |
| `devices [--json] [--backend wasapi\|fake]` | Lists microphone and playback endpoints without opening a stream. |
| `capture-test [--mic ID] [--system ID] [--seconds N] ...` | Explicit timed capture check; prints counts and levels only. Supports `--start-excluded`, `--exclude-mic-at`, `--include-mic-at`. |
| `transcribe-test ...` | Timed live transcription printed to the console, using the manifest's legacy Whisper small entry (`stt-legacy`), not the app's Qwen3-ASR path. Needs those files installed separately. |
| `capture-panel` | Minimal capture window (devices, Start/Pause/Stop, mic mute). |
| `app [--data-dir DIR] [--backend wasapi\|fake]` | The desktop app. |
| `app --check` | Starts the speech engine offscreen with synthetic devices and a throwaway folder, then stops; prints JSON. Opens no device and leaves History untouched. |

Use `app --data-dir <empty folder>` (or `MEETING_MINUTES_DATA_DIR`) for demo or test data
so your own History is not touched. `--backend fake` replaces audio devices with
synthetic tones.

## Internal names kept on purpose

The product is Tadween Desk, but these internal names stay because renaming them would
need a data or credential migration for existing users:

- the Python package `meeting_minutes` (and the `meeting-minutes` project name);
- the History folder `%LOCALAPPDATA%\MeetingMinutes` (`ui/desktop.py: default_data_dir`);
- the environment variable `MEETING_MINUTES_DATA_DIR`, which overrides that folder;
- the Windows Credential Manager target `MeetingMinutes/OpenAI` (`minutes/credentials.py`).

Treat a change to any of these as a migration, with tests, rather than a rename.

## Licences

The repository's own code is MIT (`LICENSE`); third-party components keep their own
licences (`THIRD_PARTY_NOTICES.md`, and `app/src/meeting_minutes/licences/NOTICES.md`,
which the app shows in Settings). Qt for Python (PySide6) is LGPL-3.0 and libsoxr is
LGPL-2.1-or-later; anyone who redistributes the app together with its installed
packages must meet those licences' conditions.
