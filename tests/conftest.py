"""Shared frame builders: synthetic LD2450 byte streams with the sensor's
sign-magnitude encoding, for scenarios the captured fixtures don't cover
(the real capture is an empty room — transitions are built here)."""

from __future__ import annotations

from kenzy_ld2450.protocol import HEADER, TAIL


def slot(x: int, y: int, speed: int = 0, res: int = 300) -> bytes:
    def sm(v: int) -> bytes:
        raw = (0x8000 | v) if v >= 0 else -v
        return raw.to_bytes(2, "little")

    return sm(x) + sm(y) + sm(speed) + res.to_bytes(2, "little")


def frame(*targets: bytes) -> bytes:
    slots = list(targets) + [b"\x00" * 8] * (3 - len(targets))
    return HEADER + b"".join(slots) + TAIL
