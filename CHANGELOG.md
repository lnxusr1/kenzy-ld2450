# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0]

Initial release — Kenzy's first add-on, built against the plugin seam
introduced in Kenzy 5.1.

### Added

- **The sensor, end to end.** An HLK-LD2450 on the node's UART (or a USB-TTL
  adapter) becomes level presence for its room: someone sitting still is still
  *here*. The wire parser is pinned against real captures — arbitrary read
  boundaries and line noise cost frames, never the stream — and a failed or
  unplugged sensor costs the feature, never the node.

- **Presence tuned from measurement, not guesswork.** Present asserts on the
  first sighting; clear waits out coverage gaps (default 30 s — a real desk
  capture held a continuous 7.6 s gap that a shorter clear falsely ended). A
  radial range gate keeps the next room out, and the sensor feeds Kenzy's
  occupancy model as held evidence: a dead node's hold is released by a
  staleness sweep instead of pinning its room occupied forever.

- **The Room radar dashboard panel.** A live top-down target view (dots with
  distance and speed, one tab per room on multi-node fleets), drag-to-draw
  **ignore zones** for false sources — a ceiling fan is a dot with speed that
  never moves; zoned-out targets stay visible (hollow) so you can see what
  you're ignoring — and the sensor's settings (serial device, range, clear
  time) editable per node, applied live.

- **Home Assistant, automatically.** With Kenzy's MQTT integration enabled,
  each sensor-bearing node publishes an occupancy `binary_sensor` plus target
  count and nearest distance onto the node's existing HA device, via MQTT
  Discovery. Only nodes whose sensor has actually reported get entities, and
  they share the node's availability — an offline node reads *unavailable*,
  never "clear".
