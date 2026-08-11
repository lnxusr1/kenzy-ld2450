"""The wire parser, pinned against a REAL capture (pi-a, 2026-08-10: 15 s of
an EMPTY office — the clear-room baseline; a spot-read minutes earlier showed
a still person held at x=+78 mm, y=+2471 mm, speed 0, which anchors the
sign-magnitude decode) plus synthetic damage a clean capture can't show. The
capture is the contract: if a parser change reads those bytes differently,
that's a decoding change, not a refactor."""

from __future__ import annotations

from pathlib import Path

from conftest import frame, slot

from kenzy_ld2450.protocol import FRAME_LEN, HEADER, FrameParser, decode_frame

FIXTURE = Path(__file__).parent / "fixtures" / "office-empty-15s.bin"


def test_the_real_capture_parses_completely() -> None:
    data = FIXTURE.read_bytes()
    parser = FrameParser()
    frames = parser.feed(data)
    assert len(frames) == len(data) // FRAME_LEN == 180  # 12 Hz × 15 s, no losses
    assert parser.desynced_bytes == 0  # a clean line stays clean
    # An empty office reads as exactly that — the radar does NOT invent
    # targets from furniture, which is the baseline every presence rule
    # stands on.
    assert all(not f.targets for f in frames)


def test_sign_magnitude_decoding_both_sides_of_boresight() -> None:
    # The live spot-read: a person ~2.5 m out, slightly right, holding still.
    f = decode_frame(frame(slot(78, 2471, 0, 360)))
    (t,) = f.targets
    assert (t.x_mm, t.y_mm, t.speed_cms, t.resolution_mm) == (78, 2471, 0, 360)
    # Left of boresight, walking away: negative x, negative speed.
    f = decode_frame(frame(slot(-450, 1800, -25, 240)))
    (t,) = f.targets
    assert (t.x_mm, t.y_mm, t.speed_cms) == (-450, 1800, -25)
    assert t.distance_mm == 1855  # radial, not just y


def test_empty_slots_and_multiple_targets() -> None:
    assert decode_frame(frame()).targets == ()
    f = decode_frame(frame(slot(0, 1000), slot(500, 2000, 10)))
    assert len(f.targets) == 2


def test_arbitrary_chunk_boundaries_lose_nothing() -> None:
    """Serial reads split anywhere; a parser that assumes frame-aligned reads
    works on the bench and drops frames in the wall."""
    data = FIXTURE.read_bytes()
    for size in (1, 7, 29, 31, 300):
        parser = FrameParser()
        frames = []
        for i in range(0, len(data), size):
            frames += parser.feed(data[i : i + size])
        assert len(frames) == 180, f"chunk size {size} lost frames"
        assert parser.desynced_bytes == 0


def test_garbage_costs_the_garbage_not_the_stream() -> None:
    """Join mid-frame with noise on the line: the parser must resync and then
    read every subsequent frame — including noise that CONTAINS header bytes."""
    data = FIXTURE.read_bytes()
    noise = b"\x00\x55\xcc" + HEADER[:2] + b"\x99" * 11
    parser = FrameParser()
    frames = parser.feed(data[17:60] + noise + data[60:])
    # Lost: the torn first frame and the noise. Kept: everything after.
    assert len(frames) >= 178
    assert parser.desynced_bytes > 0


def test_payload_bytes_that_mimic_the_header_do_not_derail_alignment() -> None:
    """A target whose raw bytes contain header-like sequences must not eat the
    tail of its own frame."""
    evil = slot(0x7FAA, 0x03FF, 0, 0x100)
    parser = FrameParser()
    out = parser.feed(frame(evil) * 3)
    assert len(out) == 3
