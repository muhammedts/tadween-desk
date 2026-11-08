# Tadween Desk | تدوين: components and licences

Tadween Desk's own code is released under the MIT License (LICENSE in the source
repository). This page lists the third-party components it runs with.

Tadween Desk runs speech recognition on this PC. No audio is saved or uploaded and
nothing is downloaded. The speech model runs in a separate local process
(llama-server) that listens only on this computer (127.0.0.1) and accepts requests
only from this run of the app.

The one exception is optional: if you add your own OpenAI API key in Settings and
press Generate MoM on a saved meeting, the app sends that meeting's saved transcript
and related details over HTTPS to api.openai.com: the title, date, time zone, line
timestamps, source labels and gap or recognition markers (never audio). Nothing is sent without that key and that button.

The licence texts of the speech model, its server, the VAD and Qt follow this summary.
The other packages' licence texts are in their installed folders (app\.venv) and in
the Python installation.

| Component | Version | Licence | Text below |
|---|---|---|---|
| CPython runtime | 3.11 (64-bit) | PSF-2.0 (also covers bundled OpenSSL, libffi, SQLite, expat, zlib, bzip2, xz) | `LICENSE.txt` of the Python installation |
| PySide6-Essentials, shiboken6 (Qt 6.11) | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only; used here under LGPL-3.0 | `Qt-PySide6-LGPL-3.0.txt` and `Qt-PySide6-GPL-3.0.txt` (the LGPL-3.0 builds on the GPL-3.0 text; official texts from gnu.org) |
| sherpa-onnx, sherpa-onnx-core (includes ONNX Runtime) | 1.13.8 | Apache-2.0 | in the package |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | in the package |
| soxr (python-soxr, libsoxr, PFFFT) | 1.1.0 | LGPL-2.1-or-later | in the package |
| PyAudioWPatch (includes PortAudio) | 0.2.12.9 | Apache-2.0 (from its metadata); the bundled PortAudio is under its own MIT-style licence | in the package |
| pydantic, pydantic-core | 2.14.1, 2.50.1 | MIT | in the package |
| annotated-types, typing-inspection | 0.8.0, 0.4.4 | MIT | |
| typing-extensions | 4.16.0 | PSF-2.0 | |
| Qwen3-ASR-0.6B, Q8_0 GGUF and audio projector (`ggml-org/Qwen3-ASR-0.6B-GGUF@928ab95`) | upstream `Qwen/Qwen3-ASR-0.6B` | Apache-2.0 (declared on both model cards; neither repository has a licence file, so the standard Apache-2.0 text is included) | `Qwen3-ASR-Apache-2.0-LICENSE.txt` |
| llama.cpp `llama-server` and libraries, Windows x64 CPU build | b11960 (`8642541b`) | MIT | `llama.cpp-MIT-LICENSE.txt` |
| LLVM OpenMP runtime (`libomp.dll`, shipped with llama.cpp) | from release b11960 | Apache-2.0 WITH LLVM-exception | `LICENSE-LLVM-OpenMP` next to llama-server |
| Silero VAD v4 (k2-fsa ONNX export) | v4 | MIT | `silero-vad-MIT-LICENSE.txt` |
| Microsoft Visual C++ runtime (`msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll`; copied next to llama-server by the model installer only when Windows has none) | 14.44 (from the PySide6-Essentials wheel) | Microsoft Visual C++ redistributable files, distributed with PySide6 | |
