#!/usr/bin/env python3
"""Generate the architecture diagram SVGs used by README.md.

Why a generator instead of two hand-written SVGs:

  - The source stays text that diffs cleanly in review (the one real
    advantage Mermaid had over a committed image).
  - Light and dark variants can't drift apart -- they're the same geometry
    with a different palette.
  - CI can re-run this and fail if the committed SVGs don't match, so the
    diagram can't silently go stale. See scripts/check_docs.py.

GitHub sanitizes SVG and does NOT apply any external stylesheet, so every
colour here is baked in as a presentation attribute. No <style>, no classes,
no script.

Usage:
    python3 scripts/gen_architecture.py            # write docs/*.svg
    python3 scripts/gen_architecture.py --check     # exit 1 if out of date
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = {
    'light': ROOT / 'docs' / 'architecture-light.svg',
    'dark': ROOT / 'docs' / 'architecture-dark.svg',
}

PALETTE = {
    'light': {
        'surface': '#ffffff',
        'ink': '#0f1a1f',
        'muted': '#56686f',
        'line': '#c4d3d7',
        'accent': '#0e7c8b',
        'accent_ink': '#ffffff',
    },
    'dark': {
        'surface': '#141e22',
        'ink': '#e4eef0',
        'muted': '#8ba1a8',
        'line': '#2b3a41',
        'accent': '#45b9c8',
        'accent_ink': '#06181c',
    },
}

W, H = 1000, 430

SANS = 'IBM Plex Sans, Segoe UI, Helvetica, Arial, sans-serif'
MONO = 'IBM Plex Mono, SFMono-Regular, Consolas, monospace'

# Boxes: (x, y, w, h, title, sublines, owned_by_user)
# owned_by_user=True renders filled -- "you install and run this".
NODES = [
    (30, 163, 140, 100, 'Hayward panel', ['ProLogic /', 'AquaLogic'], False),
    (248, 60, 164, 86, 'AquaConnect box', ['existing hardware'], False),
    (248, 280, 164, 86, 'Pad Pi bridge', ['Pi Zero + USB-RS485', 'rs485_bridge.py'], True),
    (490, 163, 180, 100, 'Python sidecar', ['pool_service.py', 'source of truth'], True),
    (748, 60, 192, 86, 'Homebridge plugin', ['polls every 5s'], True),
    (748, 280, 192, 86, 'Web cockpit', ['served by the sidecar --', 'nothing extra to install'], False),
]

# Arrows: (x1, y1, x2, y2, label, label_x, label_y)
EDGES = [
    (170, 194, 248, 113, 'RS-485', 209, 143),
    (170, 232, 248, 313, 'RS-485', 207, 287),
    (412, 113, 490, 185, 'local HTTP', 455, 137),
    (412, 313, 490, 242, 'HTTP / Tailscale', 459, 296),
    (670, 185, 748, 113, 'REST', 712, 137),
    (670, 242, 748, 313, 'REST + SSE', 714, 296),
]


def esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def build(theme: str) -> str:
    c = PALETTE[theme]
    o: list[str] = []
    add = o.append

    add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'width="{W}" height="{H}" role="img" '
        f'aria-label="Architecture: the Hayward pool panel connects over RS-485 to either '
        f'an AquaConnect box or a Pi Zero pad bridge. Exactly one of those connects to the '
        f'Python sidecar, which serves both the Homebridge plugin and the web cockpit. '
        f'The pad bridge, sidecar and plugin are filled, marking them as pieces you install '
        f'and run yourself.">')

    add('<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{c["ink"]}" opacity="0.5"/></marker></defs>')

    # "pick one" grouping around the two backends
    add(f'<rect x="228" y="38" width="204" height="350" rx="4" fill="none" '
        f'stroke="{c["line"]}" stroke-width="1" stroke-dasharray="4 4"/>')
    add(f'<text x="330" y="26" text-anchor="middle" font-family="{MONO}" font-size="11" '
        f'letter-spacing="0.8" fill="{c["muted"]}">PANEL LINK — PICK ONE</text>')

    # Edges first so boxes sit on top of the line ends. Each label gets an
    # opaque backing rect ("halo") because the labels sit on the diagonals --
    # without it the stroke runs straight through the text. Width is estimated
    # from the character count; the mono face is ~6.4px per glyph at 11px.
    for x1, y1, x2, y2, label, lx, ly in EDGES:
        add(f'<path d="M {x1} {y1} L {x2} {y2}" fill="none" stroke="{c["ink"]}" '
            f'stroke-width="1.4" opacity="0.5" marker-start="url(#ar)" marker-end="url(#ar)"/>')
        hw = len(label) * 6.4 / 2 + 4
        add(f'<rect x="{lx - hw:.1f}" y="{ly - 11}" width="{hw * 2:.1f}" height="15" '
            f'rx="2" fill="{c["surface"]}"/>')
        add(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-family="{MONO}" '
            f'font-size="11" fill="{c["muted"]}">{esc(label)}</text>')

    # nodes
    for x, y, w, h, title, subs, mine in NODES:
        fill = c['accent'] if mine else c['surface']
        stroke = c['accent'] if mine else c['line']
        t_fill = c['accent_ink'] if mine else c['ink']
        s_fill = c['accent_ink'] if mine else c['muted']
        s_op = '0.82' if mine else '1'

        add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.5"/>')

        cx = x + w // 2
        # Vertically centre the title + sublines block within the box.
        block = 20 + 16 * len(subs)
        ty = y + (h - block) // 2 + 15
        add(f'<text x="{cx}" y="{ty}" text-anchor="middle" font-family="{SANS}" '
            f'font-size="15" font-weight="600" fill="{t_fill}">{esc(title)}</text>')
        for i, s in enumerate(subs):
            add(f'<text x="{cx}" y="{ty + 20 + i * 16}" text-anchor="middle" '
                f'font-family="{MONO}" font-size="11" fill="{s_fill}" '
                f'opacity="{s_op}">{esc(s)}</text>')

    # endpoint captions
    add(f'<text x="844" y="42" text-anchor="middle" font-family="{SANS}" font-size="12.5" '
        f'fill="{c["muted"]}">Apple Home · HAP</text>')
    add(f'<text x="844" y="386" text-anchor="middle" font-family="{SANS}" font-size="12.5" '
        f'fill="{c["muted"]}">any browser</text>')

    # legend
    add(f'<rect x="30" y="398" width="22" height="13" rx="2" fill="{c["accent"]}" '
        f'stroke="{c["accent"]}"/>')
    add(f'<text x="60" y="409" font-family="{SANS}" font-size="12.5" fill="{c["muted"]}">'
        f'You install and run this</text>')
    add(f'<rect x="250" y="398" width="22" height="13" rx="2" fill="{c["surface"]}" '
        f'stroke="{c["line"]}" stroke-width="1.5"/>')
    add(f'<text x="280" y="409" font-family="{SANS}" font-size="12.5" fill="{c["muted"]}">'
        f'Already exists, or comes for free</text>')

    add('</svg>')
    return '\n'.join(o) + '\n'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='exit 1 if the committed SVGs differ from freshly generated ones')
    args = ap.parse_args()

    stale = []
    for theme, path in OUT.items():
        want = build(theme)
        if args.check:
            have = path.read_text() if path.exists() else None
            if have != want:
                stale.append(path.relative_to(ROOT))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(want)
            print(f'wrote {path.relative_to(ROOT)}')

    if stale:
        print('Architecture SVGs are out of date:', file=sys.stderr)
        for p in stale:
            print(f'  {p}', file=sys.stderr)
        print('\nRegenerate with:  python3 scripts/gen_architecture.py', file=sys.stderr)
        return 1
    if args.check:
        print('architecture SVGs up to date')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
