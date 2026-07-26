"""Debug artefacts for the local bot tester: fault replays and heatmaps.

Pure standard library — writes plain text plus self-contained SVG, so the output
folder opens anywhere (terminal or browser) with nothing installed.
"""

import html
import os

# Sequential blue ramp, light -> dark. Light steps mean "near zero".
RAMP = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec",
    "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab",
    "#184f95", "#104281", "#0d366b",
]
# Steps at or past this index are dark enough to need light text on top.
_DARK_FROM = 7
SHADES = " .:-=+*#%@"  # ascii ramp, low -> high


def _step(value, vmax):
    """Index into RAMP for `value` (0 -> lightest, vmax -> darkest)."""
    if vmax <= 0:
        return 0
    frac = max(0.0, min(1.0, value / vmax))
    return min(len(RAMP) - 1, int(frac * (len(RAMP) - 1) + 0.5))


# -- ascii ---------------------------------------------------------------------

def ascii_heatmap(grid, fmt=lambda v: f"{v:.0f}"):
    """Shaded ascii grid plus the exact numbers, for terminals and diffs."""
    rows, cols = len(grid), len(grid[0]) if grid else 0
    values = [v for row in grid for v in row if v is not None]
    vmax = max(values) if values else 0
    out = ["    " + " ".join(f"{c:>2}" for c in range(cols))]
    for r in range(rows):
        cells = []
        for v in grid[r]:
            if v is None:
                cells.append(" _")   # no data, as distinct from "near zero"
            else:
                shade = SHADES[min(len(SHADES) - 1, _step(v, vmax) * len(SHADES) // len(RAMP))]
                cells.append(f" {shade}")
        out.append(f"{r:>3} " + " ".join(cells))
    out.append("")
    width = max((len(fmt(v)) for v in values), default=1)
    out.append("exact values")
    out.append("    " + " ".join(f"{c:>{width}}" for c in range(cols)))
    for r in range(rows):
        cells = ["-".rjust(width) if v is None else fmt(v).rjust(width) for v in grid[r]]
        out.append(f"{r:>3} " + " ".join(cells))
    out.append("")
    out.append(f"shading: ' {SHADES.strip()}' low -> high ('_' = no data), "
               f"max = {fmt(vmax) if values else '-'}")
    return "\n".join(out)


# -- svg -----------------------------------------------------------------------

_CELL = 44
_PAD_L = 34
_PAD_T = 58

_SVG_STYLE = """
  .surface { fill: #fcfcfb; }
  .title   { fill: #0b0b0b; font: 600 15px system-ui, sans-serif; }
  .sub     { fill: #52514e; font: 400 12px system-ui, sans-serif; }
  .axis    { fill: #52514e; font: 400 11px system-ui, sans-serif; }
  .val     { font: 400 11px system-ui, sans-serif; }
  .on-light { fill: #0b0b0b; }
  .on-dark  { fill: #ffffff; }
  .empty   { fill: #f0efec; }
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; }
    .title   { fill: #ffffff; }
    .sub, .axis { fill: #c3c2b7; }
    .on-light { fill: #0b0b0b; }
    .empty   { fill: #383835; }
  }
"""


def svg_heatmap(title, subtitle, grid, fmt=lambda v: f"{v:.0f}", tip=None):
    """A 10x10-style heatmap as one self-contained SVG string.

    `tip(r, c, value)` supplies the hover text for a cell (SVG <title>).
    """
    rows, cols = len(grid), len(grid[0]) if grid else 0
    values = [v for row in grid for v in row if v is not None]
    vmax = max(values) if values else 0
    w = _PAD_L + cols * _CELL + 16
    h = _PAD_T + rows * _CELL + 46
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(title)}">',
        f"<style>{_SVG_STYLE}</style>",
        f'<rect class="surface" width="{w}" height="{h}" rx="6"/>',
        f'<text class="title" x="{_PAD_L}" y="24">{html.escape(title)}</text>',
        f'<text class="sub" x="{_PAD_L}" y="42">{html.escape(subtitle)}</text>',
    ]
    for c in range(cols):
        x = _PAD_L + c * _CELL + _CELL / 2
        parts.append(f'<text class="axis" x="{x:.0f}" y="{_PAD_T - 6}" text-anchor="middle">{c}</text>')
    for r in range(rows):
        y = _PAD_T + r * _CELL + _CELL / 2 + 4
        parts.append(f'<text class="axis" x="{_PAD_L - 8}" y="{y:.0f}" text-anchor="end">{r}</text>')
        for c in range(cols):
            v = grid[r][c]
            x = _PAD_L + c * _CELL
            yy = _PAD_T + r * _CELL
            # 2px surface gap between fills keeps adjacent cells legible.
            rect = f'x="{x + 1}" y="{yy + 1}" width="{_CELL - 2}" height="{_CELL - 2}" rx="4"'
            label = "" if v is None else fmt(v)
            if v is None:
                parts.append(f'<rect class="empty" {rect}/>')
            else:
                step = _step(v, vmax)
                ink = "on-dark" if step >= _DARK_FROM else "on-light"
                title_txt = tip(r, c, v) if tip else f"[{r},{c}] {label}"
                parts.append(
                    f'<g><title>{html.escape(title_txt)}</title>'
                    f'<rect fill="{RAMP[step]}" {rect}/>'
                    f'<text class="val {ink}" x="{x + _CELL / 2:.0f}" y="{yy + _CELL / 2 + 4:.0f}" '
                    f'text-anchor="middle">{html.escape(label)}</text></g>'
                )
    # Legend: the ramp with its end values.
    ly = _PAD_T + rows * _CELL + 20
    lx = _PAD_L
    for i, colour in enumerate(RAMP):
        parts.append(f'<rect fill="{colour}" x="{lx + i * 16}" y="{ly}" width="14" height="10" rx="2"/>')
    parts.append(f'<text class="axis" x="{lx + len(RAMP) * 16 + 6}" y="{ly + 9}">'
                 f'0 &#8594; {html.escape(fmt(vmax) if values else "0")}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# -- boards & move logs --------------------------------------------------------

def ship_letters(names):
    """One distinct letter per ship name — first letter where possible (carrier and
    cruiser both start with 'c', so the second one falls through to its next letter)."""
    letters = {}
    used = set()
    for name in sorted(names):
        for ch in name + "0123456789":
            if ch.lower() not in used:
                used.add(ch.lower())
                letters[name] = ch.lower()
                break
    return letters


def board_picture(rows, cols, cell_to_ship, shots):
    """The bot's own board: ship letters (uppercase once hit), `*` for an incoming
    miss, `.` for an untouched empty cell. `shots` maps (r, c) -> result."""
    letters = ship_letters(set(cell_to_ship.values()))
    lines = ["    " + " ".join(f"{c:>2}" for c in range(cols))]
    for r in range(rows):
        cells = []
        for c in range(cols):
            ship = cell_to_ship.get((r, c))
            hit = (r, c) in shots
            if ship:
                letter = letters[ship]
                cells.append(f" {letter.upper() if hit else letter}")
            else:
                cells.append(" *" if hit else " .")
        lines.append(f"{r:>3} " + " ".join(cells))
    return "\n".join(lines)


def shot_picture(rows, cols, shots, cell_to_ship):
    """The board the bot was shooting at: `X` hit, `o` miss, `.` never fired, with the
    opponent's ship cells shown as letters where they were never found."""
    letters = ship_letters(set(cell_to_ship.values()))
    lines = ["    " + " ".join(f"{c:>2}" for c in range(cols))]
    for r in range(rows):
        cells = []
        for c in range(cols):
            result = shots.get((r, c))
            if result in ("hit", "sunk"):
                cells.append(" X")
            elif result == "miss":
                cells.append(" o")
            elif (r, c) in cell_to_ship:
                cells.append(f" {letters[cell_to_ship[(r, c)]]}")
            else:
                cells.append(" .")
        lines.append(f"{r:>3} " + " ".join(cells))
    return "\n".join(lines)


def move_log_table(moves_log, until=None):
    """Both sides' shots, one row per round. `until` cuts the log off after that round
    — after a forfeit the opponent plays on alone, which is rarely what you came for."""
    by_round = {}
    for m in moves_log:
        by_round.setdefault(m["round"], {})[m["side"]] = m
    shown = [r for r in sorted(by_round) if until is None or r <= until]
    dropped = len(by_round) - len(shown)
    head = f"{'round':>5}  {'you':<16}{'result':<10}{'opponent':<16}{'result':<10}"
    lines = [head, "-" * len(head)]
    for rnd in shown:
        row = by_round[rnd]
        out = [f"{rnd:>5}  "]
        for side in ("a", "b"):
            m = row.get(side)
            if m is None:
                out.append(f"{'-':<16}{'-':<10}")
            else:
                cell = "[{},{}]".format(m["row"], m["col"])
                sunk = " " + m["sunk_ship"] if m["sunk_ship"] else ""
                out.append(f"{cell:<16}{m['result'] + sunk:<10}")
        lines.append("".join(out))
    if dropped:
        lines.append(f"... {dropped} later round(s) hidden — you were out of the game by "
                     "then, the opponent played on alone")
    return "\n".join(lines)


# -- index page ----------------------------------------------------------------

_INDEX_CSS = """
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; padding: 32px; max-width: 900px;
         background: #fcfcfb; color: #0b0b0b; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 32px 0 8px; }
  p.sub { color: #52514e; margin: 0 0 24px; }
  pre { overflow-x: auto; background: #f0efec; padding: 12px; border-radius: 6px; font-size: 12px; }
  ul { padding-left: 20px; } li { margin: 2px 0; }
  a { color: #2a78d6; }
  @media (prefers-color-scheme: dark) {
    body { background: #1a1a19; color: #fff; }
    p.sub { color: #c3c2b7; }
    pre { background: #383835; }
    a { color: #3987e5; }
  }
"""


def write_index(path, title, subtitle, summary, svgs, fault_files):
    """A single page tying the run together: summary, heatmaps, fault list."""
    parts = [
        f"<title>{html.escape(title)}</title>",
        f"<style>{_INDEX_CSS}</style>",
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="sub">{html.escape(subtitle)}</p>',
        "<h2>Summary</h2>",
        f"<pre>{html.escape(summary)}</pre>",
    ]
    if fault_files:
        parts.append(f"<h2>Faults ({len(fault_files)})</h2><ul>")
        for name, desc in fault_files:
            parts.append(f'<li><a href="faults/{html.escape(name)}">{html.escape(name)}</a> '
                         f"&mdash; {html.escape(desc)}</li>")
        parts.append("</ul>")
    else:
        parts.append("<h2>Faults</h2><p>None &mdash; no illegal moves, crashes, or bad layouts.</p>")
    parts.append("<h2>Heatmaps</h2>")
    for svg in svgs:
        parts.append(svg)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")
