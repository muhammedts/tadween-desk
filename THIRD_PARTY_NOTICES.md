# Third-party notices

The MIT License in [`LICENSE`](LICENSE) covers only Tadween Desk's own code. The app uses
the third-party software and models below, each under its own licence. **Model weights and
the llama.cpp runtime are not included in this repository:** `Install Models.cmd` downloads
them from their publishers (pinned in [`app/models/manifest.json`](app/models/manifest.json)).
Python packages are installed from PyPI by `app\scripts\bootstrap.ps1` (pinned with hashes
in `app/requirements-dev.lock.txt`; the app's runtime subset is `app/requirements.lock.txt`).

The full licence texts the app shows in **Settings and help › About and licences** are in
[`app/src/meeting_minutes/licences/`](app/src/meeting_minutes/licences/), with a summary in
[`NOTICES.md`](app/src/meeting_minutes/licences/NOTICES.md).

## Speech models and runtime (downloaded by `Install Models.cmd`)

| Component | Version | Licence | Source |
|---|---|---|---|
| Qwen3-ASR-0.6B, Q8_0 GGUF and audio projector | `ggml-org/Qwen3-ASR-0.6B-GGUF` @ `928ab958` (upstream `Qwen/Qwen3-ASR-0.6B`) | Apache-2.0 (declared on the model cards; the standard text is in `licences/Qwen3-ASR-Apache-2.0-LICENSE.txt`) | https://huggingface.co/ggml-org/Qwen3-ASR-0.6B-GGUF |
| llama.cpp `llama-server` and libraries, Windows x64 CPU | release b11960 (`8642541b`) | MIT (`licences/llama.cpp-MIT-LICENSE.txt`) | https://github.com/ggml-org/llama.cpp |
| LLVM OpenMP runtime (`libomp.dll`, inside the llama.cpp release) | release b11960 | Apache-2.0 WITH LLVM-exception (`LICENSE-LLVM-OpenMP`, installed next to it) | https://github.com/llvm/llvm-project |
| Silero VAD v4, ONNX export by k2-fsa | `silero_vad.onnx`, SHA-256 `9e2449e1…` | MIT (`licences/silero-vad-MIT-LICENSE.txt`) | https://github.com/snakers4/silero-vad |

## Python packages (installed by `bootstrap.ps1`)

| Package | Version | Licence |
|---|---|---|
| PySide6-Essentials, shiboken6 (Qt for Python, Qt 6.11) | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; used under LGPL-3.0 (texts in `licences/`) |
| sherpa-onnx, sherpa-onnx-core (includes ONNX Runtime) | 1.13.8 | Apache-2.0 |
| numpy | 2.4.6 | BSD-3-Clause and bundled-component licences |
| soxr (python-soxr, libsoxr, PFFFT) | 1.1.0 | LGPL-2.1-or-later |
| PyAudioWPatch (includes PortAudio) | 0.2.12.9 | Apache-2.0 (package metadata); PortAudio under its own MIT-style licence |
| pydantic, pydantic-core | 2.14.1, 2.50.1 | MIT |
| annotated-types, typing-inspection | 0.8.0, 0.4.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| CPython | 3.11 (64-bit) | PSF-2.0 |

Development tools (not part of the app): pytest, hypothesis and pip-tools
(`app/requirements-dev.lock.txt`).

## Optional online service

**Generate MoM** uses the OpenAI API with your own API key, under OpenAI's terms. Nothing is
sent unless you add a key and ask for minutes.

## Assets

The UI icons in `app/src/meeting_minutes/ui/assets/` and the images in `docs/images/` are
part of this project.

- `live-transcript.png` and `saved-transcript.png` are screenshots of the real app showing a
  fictional demo meeting, rendered by `app/scripts/demo_screenshots.py`.
- `tadween-desk-cover.png` and `tadween-desk-workflow.png` were designed for this project
  with Anthropic's Claude as HTML layouts and exported to PNG.
- `live-transcript-annotated.png` and `saved-transcript-annotated.png` are the two
  screenshots above with numbered callouts added in the same way. The app interface in them
  is unchanged.
- These four designed images keep their C2PA content credentials (Anthropic).
- The text added in them is set in Inter, Space Grotesk, IBM Plex Mono and Noto Kufi Arabic, all under the
  SIL Open Font License 1.1. The fonts themselves are not included in this repository.
