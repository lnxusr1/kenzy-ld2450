"""The plugin's two halves against Kenzy's real seams: the node driver run on
the replay device (the whole point of replay — no sensor on this machine), and
the server half feeding a REAL OccupancyTracker, held level and all."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from kenzy.plugins import NodePluginContext, ServerPluginContext
from kenzy.server.occupancy import OccupancyTracker

import kenzy_ld2450 as plugin

FIXTURE = Path(__file__).parent / "fixtures" / "office-still-15s.bin"


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    plugin.NODES.clear()
    yield
    plugin.NODES.clear()


def _server_ctx(
    tracker: OccupancyTracker | None, room: str = "Office", **cfg: Any
) -> ServerPluginContext:
    return ServerPluginContext(
        config={"stale_after_s": 15.0, **cfg},
        occupancy=tracker,
        integrations=None,
        log=logging.getLogger("test.ld2450"),
        room_of=lambda node_id: room,
    )


# ---------------------------------------------------------------------------
# Node half on replay
# ---------------------------------------------------------------------------


async def test_the_driver_reports_a_walk_in_and_a_walk_out(tmp_path: Any) -> None:
    """A synthetic visit replayed at live pace: empty → someone appears (with a
    flicker mid-stay that must NOT read as leaving) → gone. The driver should
    put exactly two transitions on the wire, in order."""
    from conftest import frame, slot

    person = frame(slot(78, 2471))
    visit = (
        frame() * 6  # empty room
        + person * 12  # someone arrives and sits
        + frame() * 2  # the radar blinks (well under clear_after_s)
        + person * 12  # still there
        + frame() * 40  # genuinely gone
    )
    capture = tmp_path / "visit.bin"
    capture.write_bytes(visit)
    events: list[dict[str, Any]] = []

    async def send_event(payload: dict[str, Any]) -> None:
        events.append(payload)

    ctx = NodePluginContext(
        node_id="n1",
        # clear_after_s well past the 2-frame blink (~0.06s replayed) and well
        # inside the 40-frame absence (~1.2s); heartbeat out of the way.
        config={"device": f"replay:{capture}", "heartbeat_s": 60, "clear_after_s": 0.4},
        send_event=send_event,
        log=logging.getLogger("test.driver"),
    )
    task = asyncio.create_task(plugin.node_run(ctx))
    try:
        for _ in range(200):  # the replay paces at ~live rate; wait, don't sleep blind
            await asyncio.sleep(0.05)
            if len(events) >= 3:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [(e["kind"], e["present"]) for e in events] == [
        ("presence", False),  # the at-connect report: sensor exists, room clear
        ("presence", True),
        ("presence", False),
    ]
    assert events[1]["nearest_mm"] == 2472  # hypot(78, 2471), radial


async def test_a_missing_device_is_a_fault_event_not_a_dead_task() -> None:
    events: list[dict[str, Any]] = []

    async def send_event(payload: dict[str, Any]) -> None:
        events.append(payload)

    ctx = NodePluginContext(
        node_id="n1",
        config={"device": "/dev/does-not-exist"},
        send_event=send_event,
        log=logging.getLogger("test.driver"),
    )
    task = asyncio.create_task(plugin.node_run(ctx))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if events:
            break
    assert not task.done(), "the driver gave up instead of retrying"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events and events[0]["kind"] == "fault" and "/dev/does-not-exist" in events[0]["error"]


# ---------------------------------------------------------------------------
# Server half against a real tracker
# ---------------------------------------------------------------------------


async def test_presence_becomes_held_level_evidence_and_release_unpins() -> None:
    tracker = OccupancyTracker()
    ctx = _server_ctx(tracker)
    await plugin.on_plugin_frame(ctx, "n1", {"kind": "presence", "present": True, "targets": 1})
    snap = tracker.snapshot(["office"])
    room = next(r for r in snap["rooms"] if r["room"] == "office")
    assert room["state"] == "occupied"
    # Someone sitting still for an HOUR: held level means still occupied.
    later = time.monotonic() + 3600
    room = next(
        r for r in tracker.snapshot(["office"], now=later)["rooms"] if r["room"] == "office"
    )
    assert room["state"] == "occupied"
    # The radar reports clear → the hold releases and belief fades from full.
    await plugin.on_plugin_frame(ctx, "n1", {"kind": "presence", "present": False, "targets": 0})
    assert plugin.NODES["n1"]["present"] is False


async def test_the_sweep_releases_a_dead_nodes_hold() -> None:
    tracker = OccupancyTracker()
    ctx = _server_ctx(tracker, stale_after_s=0.2)
    await plugin.on_plugin_frame(ctx, "n1", {"kind": "presence", "present": True, "targets": 1})
    entity = "kenzy-ld2450/n1"
    assert entity in tracker._rooms["office"].held
    sweep = asyncio.create_task(plugin.server_start(ctx))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if entity not in tracker._rooms["office"].held:
                break
    finally:
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
    assert entity not in tracker._rooms["office"].held, "dead node still pins the room"
    assert plugin.NODES["n1"]["stale"] is True
    # The node comes back: presence re-asserts and the stale mark clears.
    await plugin.on_plugin_frame(ctx, "n1", {"kind": "presence", "present": True, "targets": 1})
    assert plugin.NODES["n1"]["stale"] is False
    assert entity in tracker._rooms["office"].held


async def test_a_roomless_node_is_recorded_but_never_fed_to_occupancy() -> None:
    tracker = OccupancyTracker()
    ctx = _server_ctx(tracker, room="")
    await plugin.on_plugin_frame(ctx, "n9", {"kind": "presence", "present": True, "targets": 1})
    assert plugin.NODES["n9"]["present"] is True
    assert not tracker._rooms  # nothing invented a room


async def test_the_live_view_streams_only_while_armed_and_shows_raw_targets(
    tmp_path: Any,
) -> None:
    """Arm streaming (as a panel poll would) and replay a visit: raw target
    events flow at a bounded rate — including targets a zone is ignoring,
    because the panel must SHOW what's being ignored. Unarmed, none flow."""
    from conftest import frame, slot

    from kenzy_ld2450 import driver

    capture = tmp_path / "visit.bin"
    capture.write_bytes(frame(slot(1200, 3000, 30)) * 40)  # a fan, in-zone
    events: list[dict[str, Any]] = []

    async def send_event(payload: dict[str, Any]) -> None:
        events.append(payload)

    ctx = NodePluginContext(
        node_id="n1",
        config={
            "device": f"replay:{capture}",
            "heartbeat_s": 60,
            "ignore_zones": [[900, 2700, 1500, 3300]],
        },
        send_event=send_event,
        log=logging.getLogger("test.driver"),
    )
    await plugin.on_server_event(ctx, {"kind": "stream_targets", "seconds": 30})
    task = asyncio.create_task(plugin.node_run(ctx))
    try:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if sum(1 for e in events if e["kind"] == "targets") >= 3:
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        driver.STREAM_UNTIL = 0.0
    stream = [e for e in events if e["kind"] == "targets"]
    assert len(stream) >= 3
    (t,) = stream[0]["targets"]
    assert (t["x"], t["y"], t["speed"]) == (1200, 3000, 30)  # raw: the zoned fan is visible
    assert stream[0]["present"] is False  # …while presence correctly ignores it
    assert not any(e["kind"] == "presence" and e["present"] for e in events)


async def test_a_panel_poll_arms_streaming_scoped_to_the_open_tab() -> None:
    """?node=<id> arms ONLY that node's stream (one open panel on a 4-node
    fleet streams one radar, not four) — while status for every node still
    rides the answer, because the tab dots need it. No query arms all."""
    sent: list[tuple[str, dict[str, Any]]] = []

    async def send_to_node(node_id: str, payload: dict[str, Any]) -> bool:
        sent.append((node_id, payload))
        return True

    tracker = OccupancyTracker()
    ctx = ServerPluginContext(
        config={},
        occupancy=tracker,
        integrations=None,
        log=logging.getLogger("test.ld2450"),
        room_of=lambda n: "Office",
        send_to_node=send_to_node,
    )
    for nid in ("n1", "n2"):
        await plugin.on_plugin_frame(ctx, nid, {"kind": "presence", "present": True, "targets": 1})
    await plugin.on_plugin_frame(
        ctx, "n1", {"kind": "targets", "targets": [{"x": 1, "y": 2, "speed": 0}]}
    )
    state = await plugin.panel_state(ctx, {"node": "n1"})
    assert sent == [("n1", {"kind": "stream_targets", "seconds": 15.0})]
    assert {n["node_id"] for n in state["nodes"]} == {"n1", "n2"}  # dots see everyone
    n1 = next(n for n in state["nodes"] if n["node_id"] == "n1")
    assert n1["live_targets"] == [{"x": 1, "y": 2, "speed": 0}]
    assert n1["live_age_s"] is not None and n1["live_age_s"] < 5
    # No query (or an unknown node) falls back to arming everyone.
    sent.clear()
    await plugin.panel_state(ctx)
    assert {nid for nid, _ in sent} == {"n1", "n2"}


async def test_the_raw_radar_reaches_the_integrations_hub() -> None:
    """Presence updates hand the RAW per-sensor reading to the hub (→ HA via
    MQTT Discovery), and the staleness sweep publishes the honest zero when a
    node's sensor goes silent. No hub configured ⇒ silently free."""
    from kenzy.integrations import IntegrationHub

    hub = IntegrationHub()
    events: list[dict[str, Any]] = []
    hub.subscribe(events.append)
    tracker = OccupancyTracker()
    ctx = ServerPluginContext(
        config={"stale_after_s": 0.2},
        occupancy=tracker,
        integrations=hub,
        log=logging.getLogger("test.ld2450"),
        room_of=lambda n: "Office",
    )
    await plugin.on_plugin_frame(
        ctx, "n1", {"kind": "presence", "present": True, "targets": 2, "nearest_mm": 900}
    )
    (ev,) = events
    assert ev["type"] == "radar" and ev["present"] is True
    assert (ev["node_id"], ev["room"], ev["targets"], ev["nearest_mm"]) == ("n1", "Office", 2, 900)
    # The sweep's release reaches HA too — sensor died under a live node.
    sweep = asyncio.create_task(plugin.server_start(ctx))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(events) >= 2:
                break
    finally:
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
    assert events[-1]["present"] is False and events[-1]["targets"] == 0


async def test_panel_state_reports_what_the_panel_shows() -> None:
    ctx = _server_ctx(OccupancyTracker())
    await plugin.on_plugin_frame(
        ctx, "n1", {"kind": "presence", "present": True, "targets": 2, "nearest_mm": 1234}
    )
    await plugin.on_plugin_frame(ctx, "n2", {"kind": "fault", "error": "serial gone"})
    state = await plugin.panel_state(ctx)
    (n1,) = [n for n in state["nodes"] if n["node_id"] == "n1"]
    assert n1["present"] and n1["targets"] == 2 and n1["nearest_mm"] == 1234
    assert n1["room"] == "Office" and n1["age_s"] < 5
    (n2,) = [n for n in state["nodes"] if n["node_id"] == "n2"]
    assert n2["fault"] == "serial gone"
