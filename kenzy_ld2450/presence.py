"""Frames → a room-presence state worth sending.

The radar reports ~12 frames a second; the house needs a *state*: someone is
here, or the room has been clear long enough to believe it. Two asymmetric
rules, both deliberate:

- **Present asserts immediately.** A single frame with a target is a person
  (subject to the range gate) — waiting to be sure costs the exact latency
  this sensor exists to remove.
- **Clear waits.** Empty gaps happen with the room still occupied — the
  occupant wanders out of the ±60° beam, or holds still enough to drop — and
  the first real desk capture contained a continuous 7.6 s one. So absence
  only becomes CLEAR after ``clear_after_s`` (default 30 s: rides out such
  gaps with margin, and still beats a PIR's 60–120 s timeout by 2–4×).

The range gate (``max_range_mm``) exists because the sensor sees through
drywall better than you'd expect — a target past the far wall is the hallway,
not this room. Distance is radial (``hypot``), matching how the range actually
degrades off-boresight.

``ignore_zones`` are rectangles in sensor coordinates (mm) whose targets never
count toward presence: a ceiling fan is a *stationary* mover, so a box drawn
around its spot (from the panel's live view) removes it deterministically.
Zoned-out targets still appear in the live target stream — you should SEE the
fan dot you're ignoring, or the zone can't be checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kenzy_ld2450.protocol import Frame, Target


def _norm_zones(zones: Any) -> list[tuple[int, int, int, int]]:
    """Config → normalized (x1, y1, x2, y2) rects, corner order forgiven.
    Malformed entries are dropped (a bad zone must cost that zone, not the
    sensor)."""
    out: list[tuple[int, int, int, int]] = []
    for z in zones or ():
        try:
            x1, y1, x2, y2 = (int(v) for v in z)
        except (TypeError, ValueError):
            continue
        out.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    return out


@dataclass(frozen=True)
class PresenceState:
    present: bool
    #: Targets inside the range gate in the frame that produced this state.
    targets: int
    #: Nearest in-range target's radial distance, or None when clear.
    nearest_mm: int | None


class PresenceTracker:
    """Debounced presence over a frame stream. ``update()`` returns the new
    :class:`PresenceState` when the STATE changed, else None — callers send
    state changes, not frame rates, over the wire."""

    def __init__(
        self,
        *,
        clear_after_s: float = 30.0,
        max_range_mm: int = 6000,
        ignore_zones: Any = (),
    ) -> None:
        self._clear_after_s = float(clear_after_s)
        self._max_range_mm = int(max_range_mm)
        self._zones = _norm_zones(ignore_zones)
        self._present = False
        self._last_seen: float | None = None
        self.state = PresenceState(present=False, targets=0, nearest_mm=None)

    def _ignored(self, t: Target) -> bool:
        return any(x1 <= t.x_mm <= x2 and y1 <= t.y_mm <= y2 for x1, y1, x2, y2 in self._zones)

    def update(self, frame: Frame, now: float) -> PresenceState | None:
        in_range = [
            t
            for t in frame.targets
            if t.distance_mm <= self._max_range_mm and not self._ignored(t)
        ]
        if in_range:
            self._last_seen = now
            nearest = min(t.distance_mm for t in in_range)
            new = PresenceState(present=True, targets=len(in_range), nearest_mm=nearest)
            # A change is a TRANSITION (clear→present, or the target count
            # moving) — never nearest_mm jitter, which moves every frame on a
            # live target and would put the 12 Hz frame rate back on the wire.
            changed = not self._present or new.targets != self.state.targets
            self._present = True
            self.state = new
            return new if changed else None
        if self._present:
            if self._last_seen is not None and (now - self._last_seen) < self._clear_after_s:
                return None  # a flicker, not a departure
            self._present = False
            self.state = PresenceState(present=False, targets=0, nearest_mm=None)
            return self.state
        return None
