"""The LD2450 wire format: 30-byte periodic frames on a 256000-baud UART.

Layout (little-endian throughout)::

    AA FF 03 00 | target1 (8B) | target2 (8B) | target3 (8B) | 55 CC

Each target slot is ``x(2) y(2) speed(2) resolution(2)``. An all-zero slot
means "no target". x/y (mm) and speed (cm/s) use the sensor's sign-magnitude
convention: the high bit SET means positive, clear means negative — this is
NOT two's complement, and decoding it as int16 puts every target on the wrong
side of the room. Verified against a live capture (tests/fixtures): a person
sitting ~2.5 m in front of the sensor reads x=+78 mm, y=+2471 mm, speed 0.

The parser is incremental and resyncs: serial reads arrive at arbitrary
boundaries, and a byte lost to line noise must cost one frame, not the stream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

HEADER = b"\xaa\xff\x03\x00"
TAIL = b"\x55\xcc"
FRAME_LEN = 30
_TARGETS_PER_FRAME = 3


@dataclass(frozen=True)
class Target:
    """One tracked target. Coordinates are sensor-relative: x lateral
    (+ right of boresight), y outward from the face."""

    x_mm: int
    y_mm: int
    speed_cms: int
    resolution_mm: int

    @property
    def distance_mm(self) -> int:
        return int(math.hypot(self.x_mm, self.y_mm))


@dataclass(frozen=True)
class Frame:
    """One report: the present targets only (empty tuple = empty room,
    as far as the radar can tell)."""

    targets: tuple[Target, ...]


def _signmag(raw: int) -> int:
    """The LD2450's sign-magnitude int16: MSB set ⇒ +(raw & 0x7fff), else -raw."""
    return (raw & 0x7FFF) if raw & 0x8000 else -raw


def decode_frame(buf: bytes) -> Frame:
    """Decode one aligned 30-byte frame (header/tail already verified)."""
    targets = []
    for i in range(_TARGETS_PER_FRAME):
        o = len(HEADER) + i * 8
        chunk = buf[o : o + 8]
        if chunk == b"\x00" * 8:
            continue  # empty slot
        x = _signmag(int.from_bytes(chunk[0:2], "little"))
        y = _signmag(int.from_bytes(chunk[2:4], "little"))
        speed = _signmag(int.from_bytes(chunk[4:6], "little"))
        resolution = int.from_bytes(chunk[6:8], "little")
        targets.append(Target(x, y, speed, resolution))
    return Frame(targets=tuple(targets))


class FrameParser:
    """Incremental parser: ``feed(chunk)`` returns every complete frame in
    arrival order. Alignment is re-found by scanning for the header and the
    tail is verified per frame, so garbage (a partial first read, noise, an
    unplug mid-frame) is skipped byte-wise until the stream locks again."""

    def __init__(self) -> None:
        self._buf = bytearray()
        #: Bytes discarded while hunting for alignment — observability for the
        #: panel/status line; a healthy line stays at 0 after the first frame.
        self.desynced_bytes = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buf += chunk
        frames: list[Frame] = []
        while True:
            start = self._buf.find(HEADER)
            if start < 0:
                # No header anywhere: keep only a potential header prefix.
                keep = len(HEADER) - 1
                if len(self._buf) > keep:
                    self.desynced_bytes += len(self._buf) - keep
                    del self._buf[:-keep]
                break
            if start:
                self.desynced_bytes += start
                del self._buf[:start]
            if len(self._buf) < FRAME_LEN:
                break
            candidate = bytes(self._buf[:FRAME_LEN])
            if candidate.endswith(TAIL):
                frames.append(decode_frame(candidate))
                del self._buf[:FRAME_LEN]
            else:
                # Header bytes inside noise: drop one byte and re-hunt.
                self.desynced_bytes += 1
                del self._buf[:1]
        return frames
