"""Command-line entry point: `python -m meeting_minutes <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

EXIT_DIAGNOSTIC_ERROR = 3
EXIT_CAPTURE_FAILED = 4
EXIT_SETUP_REQUIRED = 5
STOP_BUDGET_NS = 30 * 1_000_000_000


def _doctor(args: argparse.Namespace) -> int:
    from meeting_minutes.diagnostics.doctor import collect, format_text

    if not args.no_capture:
        # The doctor never opens devices; endpoint checks are separate, explicit commands.
        print("doctor supports only --no-capture; use `devices` to list endpoints and "
              "`capture-test` for an explicit timed capture check")
        return 2
    report = collect(args.manifest)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered if args.json else format_text(report))
    # The report is still printed so the failure is structured, but it is not a pass.
    return EXIT_DIAGNOSTIC_ERROR if report["models"]["error"] else 0


def make_backend(name: str) -> Any:
    if name == "fake":
        from meeting_minutes.capture.fake import RealtimeFakeBackend

        return RealtimeFakeBackend()
    from meeting_minutes.capture.wasapi import WasapiBackend

    return WasapiBackend()


def _resolve_choice(devices: list, choice: str | None, direction: str) -> str | None:
    """`default` picks the endpoint Windows reports as default for that direction."""
    if choice in (None, "none"):
        return None
    if choice != "default":
        return choice
    for device in devices:
        if device.direction.value == direction and "default" in device.default_roles and device.available:
            return device.device_id
    raise SystemExit(f"no default {direction} endpoint is available; pass an id from `devices`")


def _devices(args: argparse.Namespace) -> int:
    backend = make_backend(args.backend)
    try:
        devices = backend.enumerate()
    finally:
        backend.close()
    if args.json:
        print(json.dumps([d.model_dump(mode="json") for d in devices], indent=2, ensure_ascii=False))
        return 0
    if not devices:
        print("No WASAPI endpoints found.")
    for d in devices:
        role = "microphone" if d.direction.value == "capture" else "playback (system audio via loopback)"
        fmt = ", ".join(f"{f.sample_rate} Hz x{f.channels}" for f in d.probed_formats) or "not capturable"
        default = " [default]" if "default" in d.default_roles else ""
        print(f"{d.device_id}\n    {role}{default}: {d.display_name} ({fmt})")
    return 0


def _capture_test(args: argparse.Namespace) -> int:
    """Explicit, timed, visible capture check. Prints counts and levels, never audio."""
    from meeting_minutes.capture.controller import CaptureController, CaptureWorker

    backend = make_backend(args.backend)
    devices = backend.enumerate()
    mic = _resolve_choice(devices, args.mic, "capture")
    system = _resolve_choice(devices, args.system, "render")
    controller = CaptureController(backend, mic_included=not args.start_excluded)
    worker = CaptureWorker(controller)
    worker.start()
    timeline: list[str] = []
    try:
        result = controller.start(mic=mic, system=system, allow_degraded=args.allow_degraded)
        if not result.started:
            print(json.dumps({"started": False, "failed": {k.value: v for k, v in result.failed.items()}}))
            return EXIT_CAPTURE_FAILED
        print(f"Capturing for {args.seconds:g} s (mic {'included' if controller.mic_included else 'EXCLUDED'}); "
              "nothing is saved.", file=sys.stderr)
        start = time.monotonic()
        actions = sorted(
            [(t, False) for t in args.exclude_mic_at] + [(t, True) for t in args.include_mic_at]
        )
        while (elapsed := time.monotonic() - start) < args.seconds:
            while actions and actions[0][0] <= elapsed:
                _, include = actions.pop(0)
                controller.set_mic_included(include, expected_generation=controller.privacy_generation)
                timeline.append(f"{elapsed:6.2f}s mic {'included' if include else 'excluded'}")
            time.sleep(0.02)
    finally:
        stop = controller.stop()
        worker.stop()
        controller.close()
    status = controller.status()
    report = {
        "started": True,
        "backend": args.backend,
        "stop": {"drained_frames": stop.drained_frames, "undrained_frames": stop.undrained_frames},
        "status": status,
        "epochs": [
            {"source": e.source.value, "epoch": e.epoch, "native_rate": e.native_rate,
             "native_channels": e.native_channels, "start_ms": e.start_ms, "end_ms": e.end_ms}
            for e in controller.epochs
        ],
        "gaps": [g.model_dump(mode="json") for g in controller.gaps],
        "commands": timeline,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for name, s in status["sources"].items():
            print(f"{name}: {s['state']} epoch {s['epoch']}, admitted {s['samples_admitted']} samples "
                  f"in {s['frames_admitted']} buffers, level {s['level_dbfs']} dBFS (peak {s['peak_dbfs']}), "
                  f"lost {s['lost_ms']} ms")
        print(f"gaps {status['gaps']}, peak owned PCM {status['peak_pcm_bytes']} bytes, "
              f"privacy generation {status['privacy_generation']}")
    return 0


def _transcribe_test(args: argparse.Namespace) -> int:
    """Explicit, timed live transcription for checking the speech pipeline from a console.

    Final text is printed to this console as it is committed; nothing is saved, and
    the mic follows the same exclusion gate as `capture-test`."""
    from meeting_minutes.capture.controller import CaptureController, CaptureWorker
    from meeting_minutes.domain.transcript import SpeechGap, TextKind, TranscriptText
    from meeting_minutes.models.manifest import default_manifest_path, load_manifest
    from meeting_minutes.speech.config import SpeechConfig
    from meeting_minutes.speech.pipeline import SpeechPipeline, SpeechWorker
    from meeting_minutes.speech.sherpa import SpeechAssetsMissing, load_speech_models

    manifest_path = default_manifest_path()
    try:
        models = load_speech_models(load_manifest(manifest_path), manifest_path.parent,
                                    language_mode=args.language, num_threads=args.threads)
    except SpeechAssetsMissing as exc:
        print(json.dumps({"setup_required": exc.readiness}))
        return EXIT_SETUP_REQUIRED
    finals: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    def on_text(event: Any) -> None:
        if isinstance(event, TranscriptText) and event.kind is TextKind.FINAL:
            line = {"source": event.source.value, "start_ms": event.start_ms, "end_ms": event.end_ms,
                    "epoch": event.epoch, "language": event.language, "text": event.text}
            finals.append(line)
            if not args.json:
                print(f"[{event.start_ms / 1000:7.2f}s {event.source.value:6} {event.language or '?'}] {event.text}",
                      flush=True)
        elif isinstance(event, SpeechGap):
            gaps.append(event.model_dump(mode="json"))

    backend = make_backend(args.backend)
    devices = backend.enumerate()
    mic = _resolve_choice(devices, args.mic, "capture")
    system = _resolve_choice(devices, args.system, "render")
    pipeline = SpeechPipeline(models.recognizer, models.vad_factory, on_text=on_text,
                              config=SpeechConfig(partials=False))
    controller = CaptureController(backend, mic_included=not args.start_excluded, sink=pipeline)
    pipeline.attach(controller)
    capture_worker, speech_worker = CaptureWorker(controller), SpeechWorker(pipeline)
    capture_worker.start()
    speech_worker.start()
    timeline: list[str] = []
    try:
        result = controller.start(mic=mic, system=system, allow_degraded=args.allow_degraded)
        if not result.started:
            print(json.dumps({"started": False, "failed": {k.value: v for k, v in result.failed.items()}}))
            return EXIT_CAPTURE_FAILED
        print(f"Transcribing for {args.seconds:g} s (mic {'included' if controller.mic_included else 'EXCLUDED'}, "
              f"language {args.language}); nothing is saved.", file=sys.stderr)
        start = time.monotonic()
        actions = sorted([(t, False) for t in args.exclude_mic_at] + [(t, True) for t in args.include_mic_at])
        while (elapsed := time.monotonic() - start) < args.seconds:
            while actions and actions[0][0] <= elapsed:
                _, include = actions.pop(0)
                controller.set_mic_included(include, expected_generation=controller.privacy_generation)
                timeline.append(f"{elapsed:6.2f}s mic {'included' if include else 'excluded'}")
            time.sleep(0.02)
    finally:
        # One stop budget for the worker's running decode and the drain.
        deadline = time.monotonic_ns() + STOP_BUDGET_NS
        controller.stop()
        capture_worker.stop()
        worker_stopped = speech_worker.stop(timeout_s=(deadline - time.monotonic_ns()) / 1e9)
        pipeline.finish(deadline)
        controller.close()
    speech = pipeline.finish_report or {}
    capture_losses = [g for g in controller.gaps if _capture_loss(g)]
    report = {"started": True,
              # complete: transcript coverage, i.e. drained with no speech gap or fault
              # and no capture loss. drain_complete alone says nothing was left waiting.
              "complete": bool(speech.get("complete")) and not capture_losses,
              "drain_complete": bool(speech.get("drain_complete")), "speech_worker_stopped": worker_stopped,
              "speech_finish": speech, "capture_loss_gaps": len(capture_losses),
              "finals": finals, "speech_gaps": gaps,
              "capture_gaps": [g.model_dump(mode="json") for g in controller.gaps], "commands": timeline,
              "speech_status": pipeline.status(), "capture_status": controller.status()}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"{len(finals)} final segments; complete={report['complete']} "
              f"(drained={report['drain_complete']}); speech gaps {len(gaps)}; "
              f"capture losses {len(capture_losses)}; faults {speech.get('faults')}; "
              f"duty {report['speech_status']['duty']}", file=sys.stderr)
    return 0


def _capture_loss(gap: Any) -> bool:
    """Whether a capture gap means audio from an admitted period is missing. Discarding
    pending mic audio on exclusion is intentional, and a playback endpoint sends no
    buffers while nothing plays, so those two are not losses; a silent mic is."""
    from meeting_minutes.domain.contracts import Source
    from meeting_minutes.domain.events import GapReason

    if gap.reason is GapReason.MIC_EXCLUDED_PENDING_DISCARDED:
        return False
    return not (gap.reason is GapReason.NO_DATA_DELIVERED and gap.source is Source.SYSTEM)


def _desktop(args: argparse.Namespace) -> int:
    if args.check:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from meeting_minutes.ui.desktop import self_check

        return self_check()
    from meeting_minutes.ui.desktop import run

    return run(backend=args.backend, data_dir=args.data_dir)


def _capture_panel(args: argparse.Namespace) -> int:
    from meeting_minutes.ui.capture_panel import run_panel

    return run_panel(make_backend(args.backend))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m meeting_minutes")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="report environment metadata without capture or network")
    doctor.add_argument("--no-capture", action="store_true", help="required: do not touch audio devices")
    doctor.add_argument("--json", action="store_true", help="print the JSON report")
    doctor.add_argument("--out", type=Path, help="also write the JSON report to this path")
    doctor.add_argument("--manifest", type=Path, help="model manifest path (default: app/models/manifest.json)")
    doctor.set_defaults(handler=_doctor)

    backend_help = "wasapi (Windows endpoints) or fake (synthetic tones, no hardware)"
    devices = commands.add_parser("devices", help="list microphone and playback endpoints (opens no stream)")
    devices.add_argument("--json", action="store_true")
    devices.add_argument("--backend", choices=("wasapi", "fake"), default="wasapi", help=backend_help)
    devices.set_defaults(handler=_devices)

    test = commands.add_parser("capture-test", help="explicit timed capture check; prints counts and levels only")
    test.add_argument("--mic", default="default", help="device id, 'default' or 'none'")
    test.add_argument("--system", default="default", help="playback device id, 'default' or 'none'")
    test.add_argument("--seconds", type=float, default=5.0)
    test.add_argument("--start-excluded", action="store_true", help="start with the mic excluded")
    test.add_argument("--exclude-mic-at", type=float, action="append", default=[], metavar="SECONDS")
    test.add_argument("--include-mic-at", type=float, action="append", default=[], metavar="SECONDS")
    test.add_argument("--allow-degraded", action="store_true", help="continue if one selected source fails")
    test.add_argument("--json", action="store_true")
    test.add_argument("--backend", choices=("wasapi", "fake"), default="wasapi", help=backend_help)
    test.set_defaults(handler=_capture_test)

    live = commands.add_parser("transcribe-test",
                               help="explicit timed live transcription with the legacy Whisper small model (the app "
                                    "uses Qwen3-ASR); prints final text to this console only")
    live.add_argument("--mic", default="default", help="device id, 'default' or 'none'")
    live.add_argument("--system", default="default", help="playback device id, 'default' or 'none'")
    live.add_argument("--seconds", type=float, default=20.0)
    live.add_argument("--start-excluded", action="store_true", help="start with the mic excluded")
    live.add_argument("--exclude-mic-at", type=float, action="append", default=[], metavar="SECONDS")
    live.add_argument("--include-mic-at", type=float, action="append", default=[], metavar="SECONDS")
    live.add_argument("--allow-degraded", action="store_true", help="continue if one selected source fails")
    live.add_argument("--language", choices=("mixed", "en", "ar"), default="mixed",
                      help="mixed: Whisper detects the language per segment")
    live.add_argument("--threads", type=int, default=4, help="recognizer CPU threads")
    live.add_argument("--json", action="store_true")
    live.add_argument("--backend", choices=("wasapi", "fake"), default="wasapi", help=backend_help)
    live.set_defaults(handler=_transcribe_test)

    panel = commands.add_parser("capture-panel", help="minimal capture window: devices, Start/Pause/Stop, mic mute")
    panel.add_argument("--backend", choices=("wasapi", "fake"), default="wasapi", help=backend_help)
    panel.set_defaults(handler=_capture_panel)

    desktop = commands.add_parser("app", help="desktop app: live transcript, mic mute, saved history")
    desktop.add_argument("--backend", choices=("wasapi", "fake"), default="wasapi", help=backend_help)
    desktop.add_argument("--data-dir", type=Path,
                         help=r"transcript database folder (default: %%LOCALAPPDATA%%\MeetingMinutes)")
    desktop.add_argument("--check", action="store_true",
                         help="start the default speech engine offscreen with synthetic devices and a throwaway "
                              "folder, then stop it; prints JSON (no device opened, History untouched)")
    desktop.set_defaults(handler=_desktop)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
