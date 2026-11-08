"""The mic privacy boundary behind mic mute/unmute.

One lock serialises the four places where microphone audio or its results can move
forward:

1. `admit` - a native callback's frame entering the bounded queue,
2. `forward` - a queued frame leaving the queue for downstream processing (level
   meter, resampling, VAD and recognition input),
3. `dispatch` - a cut chunk being claimed for recognition, and
4. `commit` - a result derived from mic audio being written.

`change` flips inclusion under the same lock and runs the caller's clearing work
(queues, downstream per-source state) before releasing it. So once `change` returns,
no frame or result stamped with an older generation can pass any of these points:
it was either already through, or it is rejected. System audio is always admitted.

`forward` delivers while holding the lock, so a downstream `accept` must stay bounded
and non-blocking (copy into its own bounded queue); slow work such as decoding happens
outside and is re-checked at `commit`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

from meeting_minutes.domain.contracts import AudioFrame, Source
from meeting_minutes.domain.privacy import (
    CommandOrigin,
    MicPrivacyState,
    PrivacyTransition,
    set_mic_included,
)

T = TypeVar("T")


class PrivacyBoundary:
    def __init__(self, *, mic_included: bool) -> None:
        self._state = MicPrivacyState.initial(included=mic_included)
        self._lock = threading.Lock()
        self.rejected_frames = {Source.MIC: 0, Source.SYSTEM: 0}
        self.rejected_at_forward = 0
        self.rejected_at_dispatch = 0
        self.rejected_commits = 0

    @property
    def state(self) -> MicPrivacyState:
        return self._state

    def generation(self) -> int:
        return self._state.generation

    def admit(self, frame: AudioFrame, enqueue: Callable[[AudioFrame], None]) -> bool:
        with self._lock:
            if not self._state.admits(frame.source, frame.privacy_generation):
                self.rejected_frames[frame.source] += 1
                return False
            enqueue(frame)
            return True

    def forward(
        self, take: Callable[[], AudioFrame | None], deliver: Callable[[AudioFrame], None]
    ) -> AudioFrame | None:
        """Dequeue one frame and deliver it if still admitted; returns the frame taken."""
        with self._lock:
            frame = take()
            if frame is None:
                return None
            if self._state.admits(frame.source, frame.privacy_generation):
                deliver(frame)
            else:
                self.rejected_at_forward += 1
            return frame

    def dispatch(self, source: Source, generation: int, start: Callable[[], bool]) -> bool:
        """Claim work stamped with `generation` for recognition: `start` runs (and must
        stay short) only if the generation is still admitted. Returns the claim."""
        with self._lock:
            if not self._state.admits(source, generation):
                self.rejected_at_dispatch += 1
                return False
            return start()

    def serialized(self, run: Callable[[], T]) -> T:
        """Run `run` with no admit, forward, dispatch, commit or change interleaving."""
        with self._lock:
            return run()

    def commit(self, source: Source, generation: int, write: Callable[[], T]) -> tuple[bool, T | None]:
        """Run `write` only if a result stamped with `generation` is still admitted."""
        with self._lock:
            if not self._state.admits(source, generation):
                self.rejected_commits += 1
                return False, None
            return True, write()

    def change(
        self,
        included: bool,
        *,
        expected_generation: int,
        origin: CommandOrigin = CommandOrigin.LOCAL,
        on_change: Callable[[PrivacyTransition], None] | None = None,
    ) -> PrivacyTransition:
        with self._lock:
            transition = set_mic_included(
                self._state, included, expected_generation=expected_generation, origin=origin
            )
            if transition.changed:
                self._state = transition.current
                if on_change is not None:
                    on_change(transition)
            return transition
