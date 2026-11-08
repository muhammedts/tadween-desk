# Contributing to Tadween Desk

Thanks for helping. Tadween Desk is a small project, so please open an issue before starting
a large change. That way we can agree on the approach first.

## Reporting a problem

Open a GitHub issue with:

- what you did, what you expected and what happened instead;
- your Windows version and Python version;
- the output of this command (it contains no meeting content):

  ```powershell
  cd app
  .\.venv\Scripts\python.exe -m meeting_minutes doctor --no-capture
  ```

- for setup problems, the output of `Install Models.cmd -DryRun` or of
  `Start Tadween Desk (troubleshooting).cmd`.

**Never attach** recordings, transcripts, exported meeting files, `meetings.db` or other
files from `%LOCALAPPDATA%\MeetingMinutes`, screenshots of real meetings, or API keys. If a
transcript line matters, retype a made-up example that shows the same problem.

**Security or privacy issues** (for example, audio or text leaving the PC unexpectedly, or a
key showing up somewhere) should not go in a public issue. Use GitHub's private
vulnerability reporting for this repository (**Security › Report a vulnerability**), or
contact the maintainer privately.

## Making a change

1. Set up the environment and models as described in the [README](README.md#quick-start-windows-from-a-fresh-clone).
2. Make one focused change. Keep to the existing style: type hints, short functions and
   comments that explain *why*, not *what*.
3. Add or update tests in `app/tests/`. Use synthetic or clearly licensed public data only,
   never recordings of real people (see `app/tests/fixtures/README.md`).
4. Run the tests from `app\`:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest
   ```

   The suite blocks network access and needs no audio device. For UI changes, also run
   the app (`Start Tadween Desk.cmd`) and check it with your own eyes.
5. Open a pull request that explains what changed and why, and how you tested it.

Please keep these product rules intact. A change that weakens one will not be merged
without a very good reason:

- the app never writes audio to disk, and audio buffers stay bounded;
- **Mute mic** stops microphone audio *before* voice detection and recognition, and late
  results from before the mute are discarded; system audio keeps transcribing;
- nothing goes over the network except the OpenAI minutes request the user explicitly
  starts; no telemetry, no automatic updates, and the app never downloads models;
- model files are checked against the pinned sizes and SHA-256 hashes in
  `app/models/manifest.json`;
- the internal names (`meeting_minutes`, `%LOCALAPPDATA%\MeetingMinutes`,
  `MEETING_MINUTES_DATA_DIR`, the `MeetingMinutes/OpenAI` credential) stay, because
  renaming them needs a data migration.

Dependency changes: update `app/pyproject.toml`, then regenerate both hash-locked files on
Python 3.11 from `app\` with the commands recorded in their headers:

```powershell
pip-compile --allow-unsafe --generate-hashes --no-emit-index-url --output-file=requirements.lock.txt --strip-extras pyproject.toml
pip-compile --allow-unsafe --extra=dev --generate-hashes --no-emit-index-url --output-file=requirements-dev.lock.txt --strip-extras pyproject.toml
```

Mention the licences of new packages in `THIRD_PARTY_NOTICES.md`.

More background on the architecture is in [docs/development.md](docs/development.md).

By contributing, you agree that your contribution is licensed under the
[MIT License](LICENSE).
