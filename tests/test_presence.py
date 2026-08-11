"""Presence semantics: assert immediately, clear reluctantly, gate by range,
and never put frame-rate jitter on the wire."""

from __future__ import annotations

from pathlib import Path

from kenzy_ld2450.presence import PresenceTracker
from kenzy_ld2450.protocol import Frame, FrameParser, Target


def _t(x: int = 0, y: int = 2000, speed: int = 0) -> Target:
    return Target(x_mm=x, y_mm=y, speed_cms=speed, resolution_mm=300)


EMPTY = Frame(targets=())


def test_present_asserts_on_the_first_frame() -> None:
    tr = PresenceTracker()
    state = tr.update(Frame(targets=(_t(),)), now=0.0)
    assert state is not None and state.present and state.targets == 1
    assert state.nearest_mm == 2000


def test_a_flicker_is_not_a_departure_but_absence_is() -> None:
    tr = PresenceTracker(clear_after_s=5.0)
    tr.update(Frame(targets=(_t(),)), now=0.0)
    # The radar drops the still person for a couple of frames.
    assert tr.update(EMPTY, now=1.0) is None
    assert tr.update(EMPTY, now=4.9) is None
    # …and past the clear window, the room is genuinely empty.
    state = tr.update(EMPTY, now=5.1)
    assert state is not None and not state.present and state.nearest_mm is None
    # Empty stays quiet — one CLEAR, not one per frame.
    assert tr.update(EMPTY, now=6.0) is None


def test_reappearing_reasserts_immediately() -> None:
    tr = PresenceTracker(clear_after_s=1.0)
    tr.update(Frame(targets=(_t(),)), now=0.0)
    tr.update(EMPTY, now=2.0)  # cleared
    state = tr.update(Frame(targets=(_t(),)), now=3.0)
    assert state is not None and state.present


def test_range_gate_drops_the_hallway() -> None:
    """The sensor sees through drywall; a target past max_range is not this
    room's business — including for the debounce clock."""
    tr = PresenceTracker(clear_after_s=5.0, max_range_mm=3000)
    beyond = Frame(targets=(_t(y=4500),))
    assert tr.update(beyond, now=0.0) is None  # never present
    tr.update(Frame(targets=(_t(y=2000),)), now=1.0)
    # Only the beyond-range target remains: that's absence, and the clear
    # clock runs from the last IN-RANGE sighting.
    assert tr.update(beyond, now=2.0) is None
    state = tr.update(beyond, now=6.5)
    assert state is not None and not state.present


def test_the_default_clear_rides_out_a_real_coverage_gap() -> None:
    """The office-desk capture (pi-a, 2026-08-10, occupant moving around the
    room) contains a continuous 7.6 s empty run — the occupant out of the
    ±60° beam, room still occupied. The DEFAULT clear must ride that out;
    the old 5 s guess demonstrably false-cleared, which is why the default
    is measured now, not guessed."""
    frames = FrameParser().feed(
        (Path(__file__).parent / "fixtures" / "office-desk-30s.bin").read_bytes()
    )
    assert len(frames) == 337

    def transitions(clear_after_s: float) -> list[bool]:
        tr = PresenceTracker(clear_after_s=clear_after_s)
        out = []
        for i, f in enumerate(frames):
            change = tr.update(f, now=i / 11.2)  # the capture's measured rate
            if change is not None:
                out.append(change.present)
        return out

    assert transitions(30.0) == [True]  # one arrival; present throughout
    wrong = transitions(5.0)
    assert False in wrong, "if 5s no longer false-clears here, re-measure the default"


def test_an_ignore_zone_silences_a_ceiling_fan_but_not_the_person() -> None:
    """The fan: a mover that never changes position. A zone drawn around its
    spot must remove it from presence entirely — while a person elsewhere in
    the room still counts, and a person WALKING THROUGH the zone is lost only
    while inside it (the debounce rides that out like any other gap)."""
    fan = _t(x=1200, y=3000, speed=30)  # spinning blades read as speed at a fixed spot
    tr = PresenceTracker(clear_after_s=5.0, ignore_zones=[[900, 2700, 1500, 3300]])
    for i in range(50):  # the fan alone, for ages: never presence
        assert tr.update(Frame(targets=(fan,)), now=i * 0.1) is None
    assert not tr.state.present
    state = tr.update(Frame(targets=(fan, _t(x=-500, y=1500))), now=6.0)
    assert state is not None and state.present and state.targets == 1  # person counted, fan not
    # Person walks INTO the zone: only the fan's spot is blind, and the
    # debounce covers the crossing.
    assert tr.update(Frame(targets=(fan, _t(x=1000, y=2900))), now=6.5) is None
    assert tr.state.present  # still present — no false clear mid-crossing


def test_zone_corners_normalize_and_garbage_zones_are_dropped() -> None:
    """A zone drawn corner-to-corner in any direction is the same zone, and a
    malformed config entry costs that entry, not the sensor."""
    for zone in ([900, 2700, 1500, 3300], [1500, 3300, 900, 2700]):
        tr = PresenceTracker(ignore_zones=[zone, "garbage", [1, 2], None])
        assert tr.update(Frame(targets=(_t(x=1200, y=3000),)), now=0.0) is None
        state = tr.update(Frame(targets=(_t(x=0, y=1000),)), now=1.0)
        assert state is not None and state.present


def test_distance_jitter_is_not_an_event_but_a_second_person_is() -> None:
    tr = PresenceTracker()
    tr.update(Frame(targets=(_t(y=2000),)), now=0.0)
    assert tr.update(Frame(targets=(_t(y=2050),)), now=0.1) is None  # jitter: silent
    state = tr.update(Frame(targets=(_t(y=2050), _t(x=800, y=1500))), now=0.2)
    assert state is not None and state.targets == 2  # a transition: speaks
    # state (for heartbeats) still tracks the latest picture either way —
    # radially: hypot(800, 1500) = 1700, nearer than the 2050 sitter.
    assert tr.state.nearest_mm == 1700
