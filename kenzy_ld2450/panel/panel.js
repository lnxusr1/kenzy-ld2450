// The Room radar panel: live top-down target view + ignore-zone editor.
//
// Polling /api/addons/ld2450/state doubles as the live-view demand signal —
// the server half re-arms target streaming on every poll, so dots go live
// within a second of opening this panel and stop when it closes.
//
// Zones are rectangles in SENSOR coordinates (mm): drag on the view to draw
// one around a false-positive source (a ceiling fan is the canonical case —
// a mover that never changes position), then Save. Saves ride the
// set_addon_node_config mutation: per-addon read-merge-write server-side,
// live-pushed, so the node's presence logic applies the zone in seconds.
// Zoned-out targets still show as dots (hollow) — you must be able to SEE
// what you're ignoring, or a zone can't be checked.
import { html, useState, useEffect, useRef } from "/js/html.js";
import { send, notify } from "/js/store.js";

const REFRESH_MS = 1000;
// World → SVG: sensor at bottom-center. ±60° fan, 6 m shown.
const W = 600, H = 560, K = 0.0909; // px per mm; 6.16 m vertical span
const sx = (mm) => W / 2 + mm * K;
const sy = (mm) => H - 20 - mm * K;
const inv = (px, py) => [Math.round((px - W / 2) / K), Math.round((H - 20 - py) / K)];

function inZone(t, z) {
  return t.x >= z[0] && t.x <= z[2] && t.y >= z[1] && t.y <= z[3];
}

function RadarView({ node, zones, draft, onDraw }) {
  const svgRef = useRef(null);
  const [drag, setDrag] = useState(null);
  const toWorld = (e) => {
    const r = svgRef.current.getBoundingClientRect();
    return inv(((e.clientX - r.left) * W) / r.width, ((e.clientY - r.top) * H) / r.height);
  };
  const norm = (a, b) => [
    Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1]),
  ];
  const targets = node.live_targets || [];
  const live = node.live_age_s != null && node.live_age_s < 5;
  return html`<svg ref=${svgRef} viewBox="0 0 ${W} ${H}"
      style="width:100%;max-width:640px;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);cursor:crosshair;touch-action:none"
      onPointerDown=${(e) => { e.preventDefault(); setDrag({ a: toWorld(e), b: toWorld(e) }); }}
      onPointerMove=${(e) => drag && setDrag({ ...drag, b: toWorld(e) })}
      onPointerUp=${() => {
        if (drag) {
          const z = norm(drag.a, drag.b);
          // A sub-15cm box is a click, not a zone.
          if (z[2] - z[0] > 150 && z[3] - z[1] > 150) onDraw(z);
          setDrag(null);
        }
      }}>
    <!-- range rings every metre + the ±60° field edge -->
    ${[1000, 2000, 3000, 4000, 5000].map(
      (r) => html`<circle cx=${sx(0)} cy=${sy(0)} r=${r * K} fill="none"
        stroke="var(--border)" stroke-dasharray="3 5" />`,
    )}
    <line x1=${sx(0)} y1=${sy(0)} x2=${sx(-5200)} y2=${sy(3002)} stroke="var(--border)" />
    <line x1=${sx(0)} y1=${sy(0)} x2=${sx(5200)} y2=${sy(3002)} stroke="var(--border)" />
    <text x=${sx(0)} y=${H - 4} text-anchor="middle" fill="var(--text-dim)" font-size="11">
      sensor${live ? "" : " · waiting for live data…"}</text>
    ${[1, 2, 3, 4, 5].map(
      (m) => html`<text x=${sx(80)} y=${sy(m * 1000) - 3} fill="var(--text-dim)"
        font-size="9">${m}m</text>`,
    )}
    <!-- saved zones, then the in-progress drag -->
    ${(zones || []).map(
      (z, i) => html`<rect key=${i} x=${sx(z[0])} y=${sy(z[3])}
        width=${(z[2] - z[0]) * K} height=${(z[3] - z[1]) * K}
        fill="var(--led-busy)" fill-opacity="0.12" stroke="var(--led-busy)"
        stroke-dasharray="4 3" />`,
    )}
    ${draft || drag
      ? (() => {
          const z = drag ? norm(drag.a, drag.b) : draft;
          return html`<rect x=${sx(z[0])} y=${sy(z[3])} width=${(z[2] - z[0]) * K}
            height=${(z[3] - z[1]) * K} fill="var(--accent)" fill-opacity="0.15"
            stroke="var(--accent)" />`;
        })()
      : null}
    <!-- targets: filled = counted, hollow = ignored (in a zone) -->
    ${live
      ? targets.map((t, i) => {
          const ignored = (zones || []).some((z) => inZone(t, z));
          return html`<g key=${i}>
            <circle cx=${sx(t.x)} cy=${sy(t.y)} r="7"
              fill=${ignored ? "none" : "var(--accent)"}
              stroke="var(--accent)" stroke-width="2" />
            <text x=${sx(t.x) + 10} y=${sy(t.y) + 4} fill="var(--text-dim)" font-size="10">
              ${(Math.hypot(t.x, t.y) / 1000).toFixed(1)}m${t.speed ? ` · ${t.speed}cm/s` : ""}
            </text></g>`;
        })
      : null}
  </svg>`;
}

// The operator-facing settings; the rest (heartbeat_s and the server-side
// stale_after_s) is ops plumbing that stays YAML-level. Rendered with the node
// editor's .cfg-grid convention — mono key, help line, styled input,
// "inherit (<default>)" placeholder, accent bar when overridden — so this
// reads as one more config surface, not a foreign form.
const SETTINGS = [
  {
    key: "device",
    ph: "/dev/serial0",
    hint: "Serial device the sensor is wired to. GPIO UART is /dev/serial0; a USB-TTL adapter is /dev/ttyUSB0.",
  },
  {
    key: "max_range_mm",
    ph: "6000",
    num: true,
    hint: "Radial range gate — targets past this are the next room, not this one.",
  },
  {
    key: "clear_after_s",
    ph: "30",
    num: true,
    hint: "How long the room must read empty before it is empty. Applies live.",
  },
];

function NodePanel({ node }) {
  const [cfg, setCfg] = useState(null); // the FULL addon slice; null until loaded
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const zones = (cfg && cfg.ignore_zones) || [];
  const loadCfg = () =>
    fetch(`/api/nodes/${node.node_id}/config`)
      .then((r) => r.json())
      .then((c) => {
        setCfg((c.config && c.config.addons && c.config.addons.ld2450) || {});
        setDirty(false);
      })
      .catch(() => setCfg({}));
  useEffect(() => {
    loadCfg();
  }, [node.node_id]);
  const patch = (changes) => {
    // Always spread the whole slice: the mutation replaces this addon's dict
    // wholesale, so dropping a key here would silently unset it on the node.
    const next = { ...cfg, ...changes };
    for (const k of Object.keys(next)) {
      if (next[k] === undefined || next[k] === "" || (Array.isArray(next[k]) && !next[k].length))
        delete next[k]; // absent key ⇒ the code default applies
    }
    setCfg(next);
    setDirty(true);
  };
  const save = async () => {
    setSaving(true);
    try {
      await send("set_addon_node_config", {
        node: node.node_id,
        addon: "ld2450",
        config: cfg,
      });
      notify("Radar settings saved — applied live");
      setDirty(false);
    } catch (e) {
      notify("Save failed: " + e, "err");
    } finally {
      setSaving(false);
    }
  };
  const status = node.fault
    ? html`<span class="micro warn">sensor fault: ${node.fault}</span>`
    : node.stale
      ? html`<span class="micro warn">silent ${node.age_s}s — hold released</span>`
      : node.present
        ? html`<b>occupied</b> · ${node.targets} ${node.targets === 1 ? "target" : "targets"}`
        : "clear";
  return html`<div class="card pad">
    <p><b>${node.room || node.node_id}</b> — ${status}
      <span class="micro"> (${node.age_s}s ago)</span></p>
    <${RadarView} node=${node} zones=${zones}
      onDraw=${(z) => patch({ ignore_zones: [...zones, z] })} />
    <p class="micro" style="margin-top:var(--s2)">
      Drag on the view to draw an <b>ignore zone</b> around a false source (a ceiling
      fan shows as a dot with speed that never moves). Hollow dots are being ignored.
    </p>
    ${zones.length
      ? html`<dl class="kv">
          ${zones.map(
            (z, i) => html`<dt>zone ${i + 1}</dt>
              <dd><span class="mono">${z[0]},${z[1]} → ${z[2]},${z[3]} mm</span>
                <button class="btn-ghost" style="margin-left:var(--s3)"
                  onClick=${() => patch({ ignore_zones: zones.filter((_, j) => j !== i) })}>
                  remove</button></dd>`,
          )}
        </dl>`
      : null}
    ${cfg
      ? html`<div class="cfg-grid" style="margin-top:var(--s4)">
          ${SETTINGS.map(
            (s) => html`<div key=${s.key} class=${"cfg-row" + (cfg[s.key] != null ? " overridden" : "")}>
              <div class="cfg-key">
                <span class="mono">${s.key}</span>
                <span class="cfg-help">${s.hint}</span>
              </div>
              <div class="cfg-input">
                <input placeholder=${"inherit (" + s.ph + ")"}
                  value=${cfg[s.key] != null ? String(cfg[s.key]) : ""}
                  onInput=${(e) => {
                    const raw = e.target.value.trim();
                    // Number.isFinite, not `|| undefined`: 0 is a legal value
                    // (clear_after_s: 0 = clear instantly), not an unset.
                    const n = Number(raw);
                    patch({
                      [s.key]:
                        raw === "" ? undefined : s.num ? (Number.isFinite(n) ? n : undefined) : raw,
                    });
                  }} />
              </div>
            </div>`,
          )}
        </div>`
      : null}
    ${dirty
      ? html`<div class="cfg-actions">
          <button class="btn-primary" disabled=${saving} onClick=${save}>
            ${saving ? "Saving…" : "Save changes"}</button>
        </div>`
      : null}
  </div>`;
}

// Tab label indicator: at-a-glance state for every room without visiting its
// tab — amber for a node needing attention, filled/hollow for occupied/clear.
function tabDot(n) {
  if (n.fault || n.stale) return html`<span class="tab-dot" style="color:var(--led-busy)">⚠</span>`;
  return html`<span class="tab-dot" style=${n.present ? "" : "opacity:.35"}>●</span>`;
}

export default function RadarPanel() {
  const [state, setState] = useState(null);
  const [err, setErr] = useState(null);
  const [sel, setSel] = useState(null); // node_id of the open tab
  // The poll carries the open tab, so ONLY that node's live stream is armed —
  // a 4-node fleet with one panel open streams one radar, not four. A ref
  // keeps the interval's closure honest when the tab changes.
  const selRef = useRef(sel);
  selRef.current = sel;
  useEffect(() => {
    let alive = true;
    const load = () =>
      fetch(
        "/api/addons/ld2450/state" +
          (selRef.current ? `?node=${encodeURIComponent(selRef.current)}` : ""),
      )
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
        .then((s) => {
          if (alive) {
            setState(s);
            setErr(null);
          }
        })
        .catch((e) => alive && setErr(String(e)));
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  if (err) return html`<div class="card pad">Radar state unavailable: ${err}</div>`;
  if (!state) return html`<div class="micro">Loading…</div>`;
  if (!state.nodes.length) {
    return html`<div class="card pad">
      <p>No radar reports yet. A node with the sensor and this add-on installed starts
      reporting within seconds of connecting.</p>
    </div>`;
  }
  const nodes = state.nodes;
  const active = nodes.find((n) => n.node_id === sel) || nodes[0];
  return html`<div>
    ${nodes.length > 1
      ? html`<div class="tabbar" style="margin-bottom:var(--s4)">
          <div class="tabs" role="tablist">
            ${nodes.map(
              (n) => html`<button key=${n.node_id} role="tab"
                aria-selected=${n.node_id === active.node_id} class="tab"
                onClick=${() => setSel(n.node_id)}>
                ${n.room || n.node_id.slice(0, 8)}${tabDot(n)}
              </button>`,
            )}
          </div>
        </div>`
      : null}
    <${NodePanel} key=${active.node_id} node=${active} />
  </div>`;
}
