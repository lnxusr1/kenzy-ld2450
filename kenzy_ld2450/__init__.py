"""kenzy-ld2450 — in-node mmWave presence (Kenzy 5.1's first plugin).

An HLK-LD2450 radar on the node's UART gives every room the sensor class most
houses have in at most one room: LEVEL presence — someone sitting still is
still *here*, where a PIR sees an empty room after its timeout. The node half
(:mod:`.driver`) reads the sensor and reports presence transitions; the server
half (below) feeds them into the occupancy tracker as held level evidence.

Two live-lock defenses shape the server half, both discovered by reading the
tracker rather than by an incident (for once):

- **The heartbeat re-assert.** ``prune_held`` rebuilds holds from the HA
  evidence map on every refetch, and this sensor is not an HA entity — so its
  hold is dropped each time, and the node's ~5 s heartbeat is what restores
  it. (The belief fades from full in between; in practice invisible.)
- **The staleness sweep.** A held level never decays, so a node that dies
  mid-presence would pin its room "occupied" forever. The sweep releases any
  node silent for ``stale_after_s`` with ``available=False`` — the tracker's
  own "stop trusting this assertion" path, which claims nothing about the
  room itself.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from kenzy.plugins import PluginManifest

from kenzy_ld2450 import driver

__version__ = "0.1.0"

MANIFEST = PluginManifest(
    id="ld2450",
    label="Room radar",
    api=1,
    roles=("node", "server"),
    ico="◉",
    panel_dir=Path(__file__).parent / "panel",
    config_defaults={
        # Server-half tuning. stale_after_s is deliberately 3× the node
        # heartbeat: one lost heartbeat is wifi, three is a dead node.
        "stale_after_s": 15.0,
    },
)

#: Server-half state: node_id → what it last said and when (monotonic).
#: Module-level on purpose — the observable surface the panel reads via the
#: sweep task, and what the tests assert against.
NODES: dict[str, dict[str, Any]] = {}

_ENTITY_PREFIX = "kenzy-ld2450/"


def _publish_radar(ctx: Any, node_id: str, state: dict[str, Any]) -> None:
    """Hand the raw per-sensor reading to the integrations hub (→ HA via MQTT
    Discovery, when the operator enabled the bridge). Deliberately the RAW
    radar, not Kenzy's fused belief: HA automations get each room's sensor as
    its own occupancy entity, joined to the node's existing HA device. No hub
    (integrations off) ⇒ free no-op."""
    if ctx.integrations is None:
        return
    from kenzy.integrations import schema

    ctx.integrations.emit(
        schema.radar(
            node_id=node_id,
            room=str(state.get("room") or "") or None,
            present=bool(state.get("present")),
            targets=int(state.get("targets") or 0),
            nearest_mm=state.get("nearest_mm"),
        )
    )


def _evidence(node_id: str, room_slug: str, *, present: bool, available: bool = True) -> Any:
    from kenzy.server.ha_events import Evidence

    return Evidence(
        entity_id=f"{_ENTITY_PREFIX}{node_id}",
        room=room_slug,
        kind="level",
        scope="room",
        present=present,
        ts=time.monotonic(),  # the tracker's clock — ha_events uses monotonic too
        available=available,
    )


async def on_server_event(ctx: Any, payload: dict[str, Any]) -> None:
    """Node half: the server half asking for the live target stream (the
    panel is open). Re-armed by every request; expires on its own."""
    if payload.get("kind") == "stream_targets":
        seconds = min(float(payload.get("seconds") or 15.0), 60.0)
        driver.STREAM_UNTIL = time.monotonic() + seconds


async def on_plugin_frame(ctx: Any, node_id: str, payload: dict[str, Any]) -> None:
    kind = payload.get("kind")
    if kind == "fault":
        ctx.log.warning("[%s] sensor fault: %s", node_id, payload.get("error"))
        NODES.setdefault(node_id, {})["fault"] = str(payload.get("error") or "")
        return
    if kind == "targets":
        # Live-view data: raw targets, zoned ones included. Kept beside the
        # presence state, not in it — the panel's business, not occupancy's.
        state = NODES.setdefault(node_id, {})
        state["live_targets"] = payload.get("targets") or []
        state["live_ts"] = time.monotonic()
        return
    if kind != "presence":
        return
    from kenzy.server.occupancy import room_slug

    room = ctx.room_of(node_id)
    state = NODES.setdefault(node_id, {})
    state.update(
        {
            "present": bool(payload.get("present")),
            "targets": int(payload.get("targets") or 0),
            "nearest_mm": payload.get("nearest_mm"),
            "room": room,
            "ts": time.monotonic(),
            "fault": "",
            # A node heard from is not stale — without this, one sweep would
            # exempt it from every future sweep.
            "stale": False,
        }
    )
    _publish_radar(ctx, node_id, state)  # HA sees the raw sensor either way
    if not room:
        # A node the server can't place can't feed a ROOM belief. Once, not
        # per-heartbeat — the state dict remembers we said it.
        if not state.get("warned_no_room"):
            state["warned_no_room"] = True
            ctx.log.warning(
                "[%s] presence from a node with no room — not fed to occupancy", node_id
            )
        return
    if ctx.occupancy is not None:
        ctx.occupancy.on_evidence(
            _evidence(node_id, room_slug(room), present=bool(payload.get("present")))
        )


async def server_start(ctx: Any) -> None:
    """The staleness sweep (see module docstring). Runs for the server's
    lifetime; a sweep pass failing is logged, never fatal."""
    stale_after = float(ctx.config.get("stale_after_s", 15.0))
    from kenzy.server.occupancy import room_slug

    while True:
        await asyncio.sleep(max(stale_after / 3.0, 1.0))
        try:
            now = time.monotonic()
            for node_id, state in NODES.items():
                if not state.get("present") or state.get("stale"):
                    continue
                if now - float(state.get("ts") or 0.0) < stale_after:
                    continue
                state["stale"] = True
                state["present"] = False
                state["targets"] = 0
                state["nearest_mm"] = None
                ctx.log.warning(
                    "[%s] no radar heartbeat for %.0fs — releasing its hold", node_id, stale_after
                )
                if ctx.occupancy is not None and state.get("room"):
                    ctx.occupancy.on_evidence(
                        _evidence(
                            node_id, room_slug(str(state["room"])), present=False, available=False
                        )
                    )
                # HA's copy goes honest too. (When the whole NODE drops, the
                # existing availability topic already flips the entity to
                # unavailable — this covers the sensor dying under a live node.)
                _publish_radar(ctx, node_id, state)
        except Exception as exc:
            ctx.log.error("sweep pass failed: %s", exc, exc_info=True)


async def panel_state(ctx: Any, query: dict[str, str] | None = None) -> dict[str, Any]:
    """What the panel shows: every node half heard from, freshest first —
    including each one's latest live targets.

    Polling this IS the live-view demand signal: each call re-arms target
    streaming for ~15 s, so the stream runs exactly while a panel is open and
    dies on its own when the tab closes. ``?node=<id>`` scopes the arming to
    the tab actually being looked at — with a 4-node prod fleet, one open
    panel must not put four radars' streams on the wire. Status for EVERY
    node still rides the answer (the tab dots need it); only the live-target
    stream is scoped.
    """
    now = time.monotonic()
    wanted = (query or {}).get("node", "")
    if ctx.send_to_node is not None:
        for node_id in list(NODES):
            if not wanted or node_id == wanted:
                await ctx.send_to_node(node_id, {"kind": "stream_targets", "seconds": 15.0})
    nodes = [
        {
            "node_id": node_id,
            "room": state.get("room") or "",
            "present": bool(state.get("present")),
            "targets": int(state.get("targets") or 0),
            "nearest_mm": state.get("nearest_mm"),
            "age_s": round(now - float(state.get("ts") or now), 1),
            "stale": bool(state.get("stale")),
            "fault": state.get("fault") or "",
            "live_targets": state.get("live_targets") or [],
            "live_age_s": (
                round(now - float(state["live_ts"]), 1) if state.get("live_ts") else None
            ),
        }
        for node_id, state in NODES.items()
    ]
    nodes.sort(key=lambda n: float(n["age_s"]))  # type: ignore[arg-type]
    return {"nodes": nodes, "stale_after_s": float(ctx.config.get("stale_after_s", 15.0))}


async def node_run(ctx: Any) -> None:
    await driver.run(ctx)
