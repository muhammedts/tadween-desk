"""Minimal capture diagnostic window (tkinter from the standard library).

A deliberately plain control surface for capture and mic privacy, separate from the
PySide6 desktop app: device pickers, Start, Pause all/Resume, Stop, an always-visible
Mute mic/Unmute mic control (Ctrl+M) with a persistent state banner, per-source states
and meters, gaps and a metadata-only event list.

Nothing starts implicitly: capture begins only from Start. Closing the window stops
capture and releases the devices. Starting with a source that fails asks once whether
to continue with the sources that opened, so a degraded meeting is always an explicit
choice.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import Any

from meeting_minutes.capture.controller import CaptureController, CaptureError, CaptureWorker
from meeting_minutes.domain.contracts import DeviceDescriptor, Direction, Source
from meeting_minutes.domain.states import SourceState
from meeting_minutes.ui import presenter

NONE_CHOICE = "(none)"
EVENT_LINES = 14


class CapturePanel:
    def __init__(
        self,
        root: tk.Tk | tk.Toplevel,
        backend: Any,
        *,
        controller_factory: Callable[..., CaptureController] = CaptureController,
        poll_ms: int = 100,
        ask_degraded: Callable[[str], bool] | None = None,
    ) -> None:
        self.root = root
        self.backend = backend
        self._factory = controller_factory
        self._poll_ms = poll_ms
        self._ask_degraded = ask_degraded or (lambda text: messagebox.askyesno("Start capture", text, parent=root))
        self._mic_included = True  # shown before Start; the user can change it first
        self._devices: dict[str, DeviceDescriptor] = {}
        self.controller = self._new_controller()
        self.worker: CaptureWorker | None = None
        self._after_id: str | None = None
        self.closed = False
        self._build()
        self.refresh_devices()
        self.poll()

    # ---------------------------------------------------------------- building

    def _new_controller(self) -> CaptureController:
        return self._factory(self.backend, mic_included=self._mic_included)

    def _build(self) -> None:
        root = self.root
        root.title("Tadween Desk — capture")
        root.minsize(720, 520)
        pad = {"padx": 8, "pady": 4}

        self.mic_banner = tk.Label(root, font=("Segoe UI", 13, "bold"), anchor="w", padx=10, pady=8)
        self.mic_banner.pack(fill="x")

        devices = ttk.LabelFrame(root, text="Devices")
        devices.pack(fill="x", **pad)
        ttk.Label(devices, text="Microphone").grid(row=0, column=0, sticky="w", **pad)
        self.mic_choice = ttk.Combobox(devices, state="readonly", width=70)
        self.mic_choice.grid(row=0, column=1, sticky="we", **pad)
        ttk.Label(devices, text="System audio (playback endpoint)").grid(row=1, column=0, sticky="w", **pad)
        self.system_choice = ttk.Combobox(devices, state="readonly", width=70)
        self.system_choice.grid(row=1, column=1, sticky="we", **pad)
        self.refresh_button = ttk.Button(devices, text="Refresh devices", command=self.refresh_devices)
        self.refresh_button.grid(row=0, column=2, **pad)
        self.apply_button = ttk.Button(devices, text="Use selected devices", command=self.on_apply_devices)
        self.apply_button.grid(row=1, column=2, **pad)
        devices.columnconfigure(1, weight=1)
        for combo in (self.mic_choice, self.system_choice):
            combo.bind("<<ComboboxSelected>>", lambda _e: self._update_controls())

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", **pad)
        self.start_button = ttk.Button(buttons, text="Start", command=self.on_start)
        self.pause_button = ttk.Button(buttons, text="Pause all", command=self.on_pause_resume)
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.on_stop)
        self.mic_button = ttk.Button(buttons, text="Mute mic", command=self.on_toggle_mic)
        for button in (self.start_button, self.pause_button, self.stop_button, self.mic_button):
            button.pack(side="left", padx=6)
        ttk.Label(buttons, text="Ctrl+M mutes/unmutes the mic").pack(side="left", padx=12)
        root.bind_all("<Control-m>", self._on_shortcut)

        sources = ttk.LabelFrame(root, text="Sources")
        sources.pack(fill="x", **pad)
        self.source_text: dict[str, tk.StringVar] = {}
        self.meters: dict[str, ttk.Progressbar] = {}
        for row, name in enumerate(("mic", "system")):
            self.source_text[name] = tk.StringVar()
            ttk.Label(sources, textvariable=self.source_text[name], anchor="w").grid(
                row=row, column=0, sticky="we", **pad
            )
            meter = ttk.Progressbar(sources, length=160, maximum=1.0)
            meter.grid(row=row, column=1, **pad)
            self.meters[name] = meter
        sources.columnconfigure(0, weight=1)

        self.summary = tk.StringVar()
        ttk.Label(root, textvariable=self.summary, anchor="w").pack(fill="x", **pad)
        # Explains a refused or failed command (e.g. a device change during capture).
        self.notice = tk.StringVar()
        ttk.Label(root, textvariable=self.notice, anchor="w", foreground="#7a3b00").pack(fill="x", **pad)

        events = ttk.LabelFrame(root, text="Timeline (metadata only)")
        events.pack(fill="both", expand=True, **pad)
        self.event_list = tk.Listbox(events, height=EVENT_LINES)
        self.event_list.pack(fill="both", expand=True, padx=4, pady=4)

        note = ttk.Label(root, text=presenter.ROUTING_NOTE, wraplength=700, justify="left", foreground="#444")
        note.pack(fill="x", **pad)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----------------------------------------------------------------- devices

    def refresh_devices(self) -> None:
        devices = self.backend.enumerate()
        self._devices = {d.device_id: d for d in devices}
        mics = [d for d in devices if d.direction is Direction.CAPTURE and d.available]
        outs = [d for d in devices if d.direction is Direction.RENDER and d.supports_loopback]
        self._mic_ids = [None] + [d.device_id for d in mics]
        self._system_ids = [None] + [d.device_id for d in outs]
        self.mic_choice["values"] = [NONE_CHOICE] + [self._label(d) for d in mics]
        self.system_choice["values"] = [NONE_CHOICE] + [self._label(d) for d in outs]
        # Preselect the Windows defaults; the user confirms by pressing Start.
        self.mic_choice.current(self._default_index(mics))
        self.system_choice.current(self._default_index(outs))
        self._update_controls()

    @staticmethod
    def _label(d: DeviceDescriptor) -> str:
        fmt = ", ".join(f"{f.sample_rate} Hz x{f.channels}" for f in d.probed_formats)
        default = " [Windows default]" if "default" in d.default_roles else ""
        return f"{d.display_name} ({fmt}){default}"

    @staticmethod
    def _default_index(devices: list[DeviceDescriptor]) -> int:
        for i, d in enumerate(devices, start=1):
            if "default" in d.default_roles:
                return i
        return 1 if devices else 0

    def _combo(self, source: Source) -> tuple[ttk.Combobox, list[str | None]]:
        if source is Source.MIC:
            return self.mic_choice, self._mic_ids
        return self.system_choice, self._system_ids

    def show_selection(self, source: Source, device_id: str | None) -> None:
        """Point a picker back at the device the controller is actually using."""
        combo, ids = self._combo(source)
        if device_id in ids:
            combo.current(ids.index(device_id))

    def selected(self, source: Source) -> str | None:
        combo, ids = (
            (self.mic_choice, self._mic_ids) if source is Source.MIC else (self.system_choice, self._system_ids)
        )
        index = combo.current()
        return ids[index] if 0 <= index < len(ids) else None

    # ---------------------------------------------------------------- commands

    def on_start(self) -> None:
        if self.controller.state.value == "stopped":
            self.controller = self._new_controller()
            self.event_list.delete(0, "end")
        self.notice.set("")
        mic, system = self.selected(Source.MIC), self.selected(Source.SYSTEM)
        result = self.controller.start(mic=mic, system=system)
        if not result.started and result.failed:
            failed = ", ".join(f"{presenter.SOURCE_LABELS[s.value]} ({code})" for s, code in result.failed.items())
            others = (mic is not None and Source.MIC not in result.failed) or (
                system is not None and Source.SYSTEM not in result.failed
            )
            if others and self._ask_degraded(f"Could not open: {failed}.\n\nStart with the other source only?"):
                result = self.controller.start(mic=mic, system=system, allow_degraded=True)
        if result.started and self.worker is None:
            self.worker = CaptureWorker(self.controller)
            self.worker.start()
        self._update_controls()

    def on_pause_resume(self) -> None:
        if self.controller.state.value == "paused":
            self.controller.resume()
            # A device picked while paused is opened only now; report if it failed.
            self.notice.set(presenter.reopen_notice(self.controller.status(), "Resume"))
        else:
            self.controller.pause()
        self._update_controls()

    def on_stop(self) -> None:
        self.controller.stop()
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self._update_controls()

    def on_toggle_mic(self) -> None:
        if self.controller.state.value == "stopped":
            self._mic_included = not self._mic_included
            self.controller = self._new_controller()
        else:
            self.controller.set_mic_included(
                not self.controller.mic_included, expected_generation=self.controller.privacy_generation
            )
            self._mic_included = self.controller.mic_included
            if self._mic_included:
                self.notice.set(presenter.reopen_notice(self.controller.status(), "unmute"))
        self._update_controls()

    def _on_shortcut(self, _event: Any) -> None:
        self.on_toggle_mic()

    def on_apply_devices(self) -> None:
        """Switch a running/paused meeting to the picked endpoints.

        Removing a source during capture is not supported: picking "(none)" is refused,
        the picker is restored to the device in use and the reason is shown, so the
        panel never claims a selection that is not applied. To stop transcribing the mic
        use Mute mic; to stop both use Pause all or Stop.

        After every switch the picker is set from the controller's effective selection,
        not from the request, and any outcome other than a plain success is explained:
        a failed device rolled back to the previous one, a source left not capturing,
        or a selection stored until Resume/unmute. Applying
        the current device again retries a source that failed or was lost.
        """
        notices: list[str] = []
        for source in (Source.MIC, Source.SYSTEM):
            choice = self.selected(source)
            current = self.controller.selected_device(source)
            label = presenter.SOURCE_LABELS[source.value]
            if choice is None:
                if current is not None:
                    self.show_selection(source, current)
                    hint = "use Mute mic" if source is Source.MIC else "use Pause all or Stop"
                    notices.append(f"{label} cannot be removed during capture ({hint}); kept the current device.")
                continue
            if choice == current and not self._can_retry(source):
                continue
            try:
                result = self.controller.switch_source(
                    source, choice, expected_epoch=self.controller.source_epoch(source)
                )
            except CaptureError as exc:
                self.show_selection(source, current)
                notices.append(f"{label} was not changed: {exc}.")
                continue
            self.show_selection(source, result.effective)
            if text := presenter.switch_notice(result):
                notices.append(text)
        self.notice.set(" ".join(notices))
        self._update_controls()

    def _can_retry(self, source: Source) -> bool:
        """A failed or lost source that would be capturing now (not paused/excluded)."""
        status = self.controller.status()
        live = status["meeting_state"] in ("capturing", "degraded")
        wanted = source is Source.SYSTEM or status["mic_included"]
        return live and wanted and self.controller.source_state(source) in (SourceState.FAILED, SourceState.LOST)

    def on_close(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.on_stop()
        self.controller.close()
        self.closed = True
        self.root.destroy()

    # ----------------------------------------------------------------- display

    def _update_controls(self) -> None:
        status = self.controller.status()
        enabled = presenter.controls(
            status,
            mic_selected=self.selected(Source.MIC) is not None,
            system_selected=self.selected(Source.SYSTEM) is not None,
        )
        if status["meeting_state"] == "stopped":
            enabled["start"] = True  # a new capture with the same devices
        self.start_button.state(["!disabled"] if enabled["start"] else ["disabled"])
        self.pause_button.configure(text="Resume" if enabled["resume"] else "Pause all")
        self.pause_button.state(["!disabled"] if enabled["pause"] or enabled["resume"] else ["disabled"])
        self.stop_button.state(["!disabled"] if enabled["stop"] else ["disabled"])
        self.mic_button.configure(text=presenter.mic_button_text(status))
        self.refresh_button.state(["!disabled"] if enabled["devices"] or status["meeting_state"] == "stopped" else ["disabled"])
        self.apply_button.state(["!disabled"] if enabled["apply_devices"] else ["disabled"])
        included = status["mic_included"]
        self.mic_banner.configure(
            text=presenter.mic_banner(status),
            background="#e7f4e4" if included else "#fde2c8",
            foreground="#1d4d1a" if included else "#7a3b00",
        )

    def poll(self) -> None:
        status = self.controller.status()
        self._update_controls()
        for name in ("mic", "system"):
            picked = self.selected(Source.MIC if name == "mic" else Source.SYSTEM) is not None
            self.source_text[name].set(presenter.source_line(name, status, picked=picked))
            running = status["sources"][name]["state"] == "running"
            level = status["sources"][name]["level_dbfs"] if running else -120.0
            self.meters[name]["value"] = presenter.meter_fraction(level)
        self.summary.set(presenter.summary_line(status))
        lines = [presenter.event_line(e) for e in self.controller.events()[-EVENT_LINES:]]
        if list(self.event_list.get(0, "end")) != lines:
            self.event_list.delete(0, "end")
            for line in lines:
                self.event_list.insert("end", line)
        self._after_id = self.root.after(self._poll_ms, self.poll)


def run_panel(backend: Any) -> int:
    root = tk.Tk()
    CapturePanel(root, backend)
    root.mainloop()
    return 0
