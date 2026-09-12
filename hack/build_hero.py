#!/usr/bin/env python3
"""Generate static/hero.svg.

Same visual family as polarpoint-io/helm-mirofish's hero (near-black
ground, copper/amber accent, condensed-mono wordmark, "> " stat lines
along the foot) so the org's repos read as one family, reworked here as a
honeycomb of approved-memory nodes instead of a ship's helm - AgentHive's
mark is the team/agent/task graph sitting inside a hive cell, not a wheel.

Pure SVG, no headless-browser render step: safe for GitHub's markdown
image sanitizer (no embedded @font-face, no <script>), and there is no
raster step to keep byte-for-byte reproducible - the vector source *is*
the shipped asset.

Rendering is deterministic: node placement comes from a seeded RNG, so
re-running this reproduces the same file byte for byte.

Usage:
    python3 hack/build_hero.py
"""
import math
import pathlib
import random

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "hero.svg"

# ---- palette (same family as helm-mirofish) --------------------------------
BG_A = "#0a0e0f"
BG_B = "#0d1113"
AMBER_LT = "#f3c56b"
AMBER = "#e8a53d"
AMBER_MD = "#c98a2e"
AMBER_DK = "#8a5a1d"
AMBER_XD = "#4d3110"
MUTED = "#8f9498"
CELL_LINE = "#2a2420"

W, H = 2400, 960
CX, CY, HR = 560, 480, 300  # honeycomb center / cell radius
TX = 1030  # wordmark left edge

rnd = random.Random(11)


def hexagon(cx, cy, r, rot=0.0):
    pts = []
    for k in range(6):
        a = rot + k * math.pi / 3
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def hex_path(cx, cy, r, rot=0.0):
    pts = hexagon(cx, cy, r, rot)
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"
    return d


# ---- surrounding honeycomb (flat-top cells, axial layout) ------------------
cell_r = HR * 0.42
comb = []
for q in range(-3, 4):
    for s in range(-3, 4):
        x = CX + cell_r * 1.5 * q
        y = CY + cell_r * math.sqrt(3) * (s + q / 2)
        d = math.hypot(x - CX, y - CY)
        if d < HR * 0.98 or d > HR * 2.05:
            continue
        comb.append(
            f'<path d="{hex_path(x, y, cell_r * 0.94)}" fill="none" '
            f'stroke="{CELL_LINE}" stroke-width="2.5" opacity="{rnd.uniform(0.35, 0.7):.2f}"/>'
        )

# ---- the central cell: team / agent / task graph, approved nodes lit ------
central = hex_path(CX, CY, HR * 0.98)

nodes = []
for i in range(7):
    a = -math.pi / 2 + i * math.tau / 7
    d = HR * rnd.uniform(0.28, 0.62)
    nodes.append((CX + d * math.cos(a), CY + d * math.sin(a)))
nodes.append((CX, CY))  # anchor node, center

edges = []
anchor = nodes[-1]
for i, p in enumerate(nodes[:-1]):
    edges.append((len(nodes) - 1, i))
order = sorted(range(len(nodes) - 1), key=lambda i: math.atan2(nodes[i][1] - CY, nodes[i][0] - CX))
for a_i, b_i in zip(order, order[1:]):
    if rnd.random() < 0.55:
        edges.append((a_i, b_i))

graph = []
for i, j in edges:
    x1, y1 = nodes[i]
    x2, y2 = nodes[j]
    graph.append(
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="url(#edge)" stroke-width="3" opacity=".8"/>'
    )
for idx, (x, y) in enumerate(nodes):
    is_anchor = idx == len(nodes) - 1
    rr = 22 if is_anchor else rnd.uniform(11, 16)
    fill = "url(#knob)" if is_anchor else AMBER_DK
    graph.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rr:.1f}" fill="{fill}" opacity=".95"/>')
    graph.append(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rr*0.45:.1f}" fill="{AMBER_LT}" opacity=".9"/>'
    )

svg = f'''<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{BG_B}"/><stop offset="1" stop-color="{BG_A}"/>
    </linearGradient>
    <radialGradient id="glow" cx="24%" cy="46%" r="55%">
      <stop offset="0" stop-color="{AMBER}" stop-opacity=".14"/>
      <stop offset="1" stop-color="{AMBER}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="rim" x1="0" y1="0" x2="0.7" y2="1">
      <stop offset="0" stop-color="{AMBER_LT}"/>
      <stop offset=".45" stop-color="{AMBER}"/>
      <stop offset=".8" stop-color="{AMBER_DK}"/>
      <stop offset="1" stop-color="{AMBER_XD}"/>
    </linearGradient>
    <radialGradient id="knob" cx="35%" cy="30%" r="75%">
      <stop offset="0" stop-color="{AMBER_LT}"/><stop offset="1" stop-color="{AMBER_DK}"/>
    </radialGradient>
    <radialGradient id="cell" cx="34%" cy="26%" r="90%">
      <stop offset="0" stop-color="#1c1712"/><stop offset="1" stop-color="#090a0a"/>
    </radialGradient>
    <linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{AMBER_LT}"/><stop offset="1" stop-color="{AMBER_DK}"/>
    </linearGradient>
    <clipPath id="cellClip"><path d="{central}"/></clipPath>
  </defs>

  <rect width="{W}" height="{H}" fill="url(#bg)"/>
  <rect width="{W}" height="{H}" fill="url(#glow)"/>

  <!-- ============================ the mark ============================ -->
  <g>
    {''.join(comb)}
    <path d="{central}" fill="url(#cell)" stroke="url(#rim)" stroke-width="{HR*0.05:.1f}"/>
    <path d="{hex_path(CX, CY, HR*1.015)}" fill="none" stroke="{AMBER_LT}" stroke-width="2" opacity=".35"/>
    <g clip-path="url(#cellClip)">
      {''.join(graph)}
    </g>
  </g>

  <!-- ============================ wordmark ============================ -->
  <text x="{TX}" y="446" font-family="Consolas, 'Courier New', monospace" font-weight="700"
        font-size="128" letter-spacing="6" fill="#ffffff">AGENTHIVE</text>
  <rect x="{TX}" y="486" width="1180" height="5" fill="{AMBER_MD}"/>
  <text x="{TX}" y="558" font-family="Consolas, 'Courier New', monospace" font-weight="500"
        font-size="42" letter-spacing="1" fill="{AMBER_MD}">Shared, reviewed memory for a team of coding agents</text>

  <!-- ============================ footer ============================== -->
  <g font-family="Consolas, 'Courier New', monospace" font-weight="400" font-size="36" fill="{MUTED}">
    <text x="86" y="820">&gt; team / agent / task graph, approval gate before anything is shared</text>
    <text x="86" y="868">&gt; bounded retrieval by traversal, not a proxy on every LLM call</text>
    <text x="86" y="916">&gt; SQLite or Postgres, metrics + OpenTelemetry tracing built in</text>
    <text x="{W-86}" y="916" text-anchor="end" fill="{AMBER_MD}" opacity=".85">polarpoint-io/agenthive</text>
  </g>
</svg>'''

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(svg)
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
