# Test fixtures

The suite currently needs no fixture files. Test audio is generated in memory
(sine tones from `meeting_minutes.capture.fake` and `tests/speech_fakes.py`, or
Windows SAPI speech from `tests/speech_tts.py`) and is never written to disk.

Rules for any new fixture:

- Only synthetic input, or public material whose licence permits this use. Never a
  recording of people who did not agree to it, and never captured live app output.
- Document each file next to it (for example in this README) with its source URL,
  licence, SHA-256, byte size and the reason it is needed.
- Text fixtures carry distinctive canary phrases so their absence can be tested.
- Audio fixtures are inputs only. Tests compare text and metrics; they never
  save processed audio.
