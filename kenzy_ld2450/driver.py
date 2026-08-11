"""The node half: read the UART, keep a presence state, tell the server.

What rides the wire (``plugin_event`` payloads, all ``kind``-tagged):

- ``{"kind": "presence", "present", "targets", "nearest_mm"}`` — on every
  presence TRANSITION, and again every ``heartbeat_s`` regardless. The
  heartbeat is load-bearing twice over: the server half re-asserts level
  evidence from it (the occupancy tracker's ``prune_held`` rebuilds holds from
  the HA map, which this sensor is not in), and its absence is how a dead node
  is noticed so its room doesn't stay pinned occupied forever.
- ``{"kind": "fault", "error"}`` — the serial device failed; retrying.

Device strings: a serial path (``/dev/serial0``, ``/dev/ttyUSB0``) or
``replay:<file>`` — a captured byte stream replayed at roughly live rate, which
is what makes the whole chain testable with no sensor on the machine.

Failure is cheap by design: the device not existing, pyserial missing, or the
sensor unplugging costs a fault event and a retry loop — never the node.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Protocol

from kenzy_ld2450.presence import PresenceState, PresenceTracker
from kenzy_ld2450.protocol import FrameParser

BAUD = 256000  # fixed by the sensor; not configurable on purpose
_CHUNK = 512
_RETRY_S = 5.0
_STREAM_HZ = 4.0  # live-view target rate — enough to watch a person walk

#: Monotonic deadline until which raw targets stream to the server (the
#: panel's live view). Armed by ``on_server_event({"kind": "stream_targets"})``
#: and re-armed by every panel poll, so the stream lives exactly as long as
#: someone is looking. One sensor per node ⇒ module state is per-node state.
STREAM_UNTIL = 0.0


class _Reader(Protocol):
    def read(self, n: int) -> bytes: ...
    def close(self) -> None: ...


class _ReplayReader:
    """Replays a captured stream at ~live pace (30-byte frame ≈ 12 Hz), then
    holds at EOF returning empty reads — a sensor watching an empty room."""

    def __init__(self, path: str) -> None:
        self._data = Path(path).read_bytes()
        self._pos = 0

    def read(self, n: int) -> bytes:
        time.sleep(0.03)
        chunk = self._data[self._pos : self._pos + 60]
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass


def _open_device(device: str) -> _Reader:
    if device.startswith("replay:"):
        return _ReplayReader(device[len("replay:") :])
    import serial  # deferred: the fault path must not be an ImportError at load

    return serial.Serial(device, BAUD, timeout=0.5)


def _payload(state: PresenceState) -> dict[str, Any]:
    return {
        "kind": "presence",
        "present": state.present,
        "targets": state.targets,
        "nearest_mm": state.nearest_mm,
    }


async def run(ctx: Any) -> None:
    """The ``node_run`` hook body. Blocking serial reads run in a worker thread
    (bounded 0.5 s timeout — never parked forever, the asyncio.to_thread
    lesson); state lives here in async-land."""
    cfg = ctx.config
    device = str(cfg.get("device") or "/dev/serial0")
    heartbeat_s = float(cfg.get("heartbeat_s", 5.0))
    tracker = PresenceTracker(
        clear_after_s=float(cfg.get("clear_after_s", 30.0)),
        max_range_mm=int(cfg.get("max_range_mm", 6000)),
        ignore_zones=cfg.get("ignore_zones") or (),
    )
    parser = FrameParser()
    last_sent = 0.0
    last_stream = 0.0

    while True:
        try:
            reader = await asyncio.to_thread(_open_device, device)
        except Exception as exc:
            ctx.log.warning("LD2450 device %s unavailable (%s) — retrying", device, exc)
            await ctx.send_event({"kind": "fault", "error": f"{device}: {exc}"})
            await asyncio.sleep(_RETRY_S)
            continue
        ctx.log.info("LD2450 open on %s", device)
        # Report the current state immediately: the server half (and the
        # panel) should know this sensor exists within a second of the node
        # starting, not one heartbeat later.
        await ctx.send_event(_payload(tracker.state))
        last_sent = time.monotonic()
        try:
            while True:
                chunk = await asyncio.to_thread(reader.read, _CHUNK)
                now = time.monotonic()
                frames = parser.feed(chunk)
                for frame in frames:
                    change = tracker.update(frame, now)
                    if change is not None:
                        await ctx.send_event(_payload(change))
                        last_sent = now
                if now - last_sent >= heartbeat_s:
                    await ctx.send_event(_payload(tracker.state))
                    last_sent = now
                # Live view (armed only): RAW targets, zoned/out-of-range ones
                # included — you must be able to SEE the dot you're ignoring.
                if frames and now < STREAM_UNTIL and now - last_stream >= 1.0 / _STREAM_HZ:
                    last_stream = now
                    await ctx.send_event(
                        {
                            "kind": "targets",
                            "targets": [
                                {"x": t.x_mm, "y": t.y_mm, "speed": t.speed_cms}
                                for t in frames[-1].targets
                            ],
                            "present": tracker.state.present,
                        }
                    )
        except asyncio.CancelledError:
            await asyncio.to_thread(reader.close)
            raise
        except Exception as exc:
            ctx.log.warning("LD2450 read failed (%s) — reopening %s", exc, device)
            await ctx.send_event({"kind": "fault", "error": str(exc)})
            try:
                await asyncio.to_thread(reader.close)
            except Exception:
                pass
            await asyncio.sleep(_RETRY_S)
