# KENZY-LD2450 &middot; [![GitHub license](https://img.shields.io/github/license/lnxusr1/kenzy-ld2450.svg)](https://github.com/lnxusr1/kenzy-ld2450/blob/main/LICENSE) ![Python Versions](https://img.shields.io/pypi/pyversions/kenzy-ld2450.svg) ![GitHub release (latest by date)](https://img.shields.io/github/v/release/lnxusr1/kenzy-ld2450.svg)

**[kenzy.ai](https://kenzy.ai)** &middot; [Documentation](https://docs.kenzy.ai/) &middot; [Install](https://docs.kenzy.ai/getting-started/)

In-node mmWave presence for [Kenzy](https://kenzy.ai): an HLK-LD2450 radar on
the node's UART gives every room *level* presence — someone sitting still is
still **here**, where a motion sensor sees an empty room after its timeout.

Installed, a node gains the sensor, a **Room radar** dashboard panel (live
target view, drag-to-draw ignore zones), per-room occupancy evidence in
Kenzy's presence model, and — when the MQTT integration is enabled — an
occupancy entity in Home Assistant. Not installed, none of that exists: no
config keys, no panel, no cost.

## Install

On the **server** host and on each **node** host that has a sensor:

```bash
pip install kenzy-ld2450
```

(into Kenzy's environment — for the standard install that is
`~/.local/share/kenzy/venv`). Restart `kenzy-server` / `kenzy-node`; add-ons
are discovered at process start. The dashboard's Settings → Add-ons card shows
what's installed and why anything failed to load.

## Wiring (Raspberry Pi GPIO UART)

The LD2450 talks 256000-baud UART (fixed by the sensor — not configurable):

| LD2450 pin | Pi physical pin | Pi GPIO |
|---|---|---|
| 5V | 2 or 4 | 5V |
| GND | 6 | GND |
| RX | 8 | GPIO14 (UART TX) |
| TX | 10 | GPIO15 (UART RX) |

Pi setup, once per node:

1. `enable_uart=1` in `/boot/firmware/config.txt` (or `raspi-config` →
   Interface Options → Serial Port: login shell **No**, hardware **Yes**).
2. No `console=serial0,…` in `/boot/firmware/cmdline.txt` — the kernel console
   must not own the port.
3. The kenzy user in the `dialout` group: `sudo usermod -aG dialout $USER`.
4. Reboot. The sensor is then `/dev/serial0` — the add-on's default device,
   so a standard wiring needs **no configuration at all**.

A USB-to-TTL adapter (CP2102 or similar, 3.3 V logic) works identically:
wire the same four lines and set the device to `/dev/ttyUSB0` in the panel.

## Configuration

Everything is editable from the **Room radar** panel per node (saved into
Kenzy's server-owned config and applied live — no restarts). The keys, for
YAML users (`addons.ld2450` in a node's config):

| Key | Default | What it does |
|---|---|---|
| `device` | `/dev/serial0` | Serial device to read. `replay:<file>` replays a captured stream (dev/test). |
| `max_range_mm` | `6000` | Radial range gate — targets past this are the next room, not this one. |
| `clear_after_s` | `30` | How long the room must read empty before it *is* empty. Measured default: real rooms show ~8 s gaps (occupant out of the ±60° beam) that a shorter clear falsely ends. |
| `ignore_zones` | `[]` | Rectangles (mm, `[x1, y1, x2, y2]`) whose targets never count — draw them on the panel's live view around a ceiling fan or a pet spot. Zoned-out targets still show (hollow) in the live view. |
| `heartbeat_s` | `5` | How often the node re-reports state (also the server's staleness signal). YAML-only plumbing. |

Server side (`configs/addons/ld2450.yaml`):

| Key | Default | What it does |
|---|---|---|
| `stale_after_s` | `15` | Silence after which a node's radar hold is released (a dead node must not pin its room occupied). 3× the heartbeat on purpose. |

## Home Assistant

With Kenzy's MQTT integration enabled (`integrations.mqtt` in server.yaml),
each sensor-bearing node publishes — via MQTT Discovery, onto the node's
existing Kenzy device in HA:

- `binary_sensor.…_radar` (device class `occupancy`)
- `sensor.…_radar_targets`, `sensor.…_radar_nearest`

Only nodes whose sensor has actually reported get entities; they share the
node's availability, so an offline node reads *unavailable*, never "clear".

## Compatibility

Requires the kenzy plugin seam (API v1). An incompatible pairing refuses to
load **visibly** (Settings → Add-ons says why) and Kenzy-driven upgrades move
core and add-ons as one resolver set, so `pip install -U` through Kenzy can't
strand a broken pair.
