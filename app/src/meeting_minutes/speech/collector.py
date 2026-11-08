"""In-memory text consumer for tests.

Committed finals are kept in arrival order; a partial is shown per hypothesis until
its final (or a clear) replaces it. Nothing is persisted.
"""

from __future__ import annotations

import threading

from meeting_minutes.domain.transcript import PartialsCleared, SpeechGap, TextEvent, TextKind, TranscriptText


class TranscriptCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.finals: list[TranscriptText] = []
        self.partials: dict[str, TranscriptText] = {}
        self.gaps: list[SpeechGap] = []
        self.partials_shown = 0

    def __call__(self, event: TextEvent) -> None:
        with self._lock:
            if isinstance(event, TranscriptText):
                if event.kind is TextKind.FINAL:
                    self.finals.append(event)
                    self._drop_partials(event.source.value, event.chunk_index)
                else:
                    self.partials[event.hypothesis_id] = event
                    self.partials_shown += 1
            elif isinstance(event, PartialsCleared):
                self._drop_partials(event.source.value, event.through_chunk_index)
            elif isinstance(event, SpeechGap):
                self.gaps.append(event)

    def _drop_partials(self, source: str, through: int | None) -> None:
        for key, partial in list(self.partials.items()):
            if partial.source.value == source and (through is None or partial.chunk_index <= through):
                del self.partials[key]

    def snapshot(self) -> tuple[list[TranscriptText], list[TranscriptText], list[SpeechGap]]:
        with self._lock:
            return list(self.finals), list(self.partials.values()), list(self.gaps)
