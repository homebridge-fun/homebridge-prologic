#!/usr/bin/env python3
"""Documentation consistency checks. Run in CI; no hardware, no network.

The architecture diagram in the README is a committed SVG, which normally
means it can drift out of date silently. These checks are what make that
safe:

  1. The committed SVGs still match scripts/gen_architecture.py, so nobody
     hand-edits the output and nobody forgets to regenerate.
  2. Every backend the plugin actually offers appears in the diagram, so
     adding or renaming one fails CI until the picture is updated.
  3. Every source file the diagram names really exists.
  4. Every relative link and image path in the Markdown docs resolves.

Check 2 is the one that catches genuine staleness -- 1 only proves the SVG
matches its generator, not that the generator matches reality.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SVGS = [ROOT / 'docs' / 'architecture-light.svg', ROOT / 'docs' / 'architecture-dark.svg']

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def check_svgs_current() -> None:
    """1. Committed SVGs match the generator."""
    r = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / 'gen_architecture.py'), '--check'],
        capture_output=True, text=True)
    if r.returncode != 0:
        fail('architecture SVGs are out of date -- run: '
             'python3 scripts/gen_architecture.py\n' + r.stderr.strip())


def check_backends_in_diagram() -> None:
    """2. Every configurable backend appears in the diagram.

    The diagram labels backends by their human name, not the config value,
    so map the schema's enum values to the words used in the picture. A new
    backend with no mapping here is itself a failure -- that's the point.
    """
    schema = json.loads((ROOT / 'config.schema.json').read_text())
    backend = schema['schema']['properties']['backend']
    values = {v for branch in backend.get('oneOf', []) for v in branch.get('enum', [])}
    if not values:
        fail('could not read the backend enum from config.schema.json')
        return

    # config value -> a string that must appear in the diagram
    labels = {
        'aquaconnect': 'AquaConnect',
        'rs485bridge': 'Pad Pi bridge',
    }

    text = SVGS[0].read_text()
    for v in sorted(values):
        if v not in labels:
            fail(f'backend "{v}" exists in config.schema.json but the architecture '
                 f'diagram has no label mapped for it -- update the diagram '
                 f'(scripts/gen_architecture.py) and the labels map in this script')
        elif labels[v] not in text:
            fail(f'backend "{v}" is configurable but "{labels[v]}" does not appear '
                 f'in the architecture diagram')

    for v in labels:
        if v not in values:
            fail(f'the diagram still shows backend "{v}", which is no longer in '
                 f'config.schema.json')


def check_files_named_in_diagram() -> None:
    """3. Source files the diagram names actually exist."""
    text = SVGS[0].read_text()
    for name in re.findall(r'\b[a-z_]+\.py\b', text):
        if not list(ROOT.rglob(name)):
            fail(f'the architecture diagram names "{name}", which does not exist '
                 f'anywhere in the repo')


def check_markdown_links() -> None:
    """4. Relative links and image paths in Markdown resolve."""
    docs = [ROOT / 'README.md', ROOT / 'CHANGELOG.md']
    docs += sorted((ROOT / 'docs').glob('*.md'))
    docs += sorted((ROOT / 'deploy').glob('*.md'))

    link_re = re.compile(r'\[[^\]]*\]\(([^)\s]+)')
    src_re = re.compile(r'(?:src|srcset)="([^"]+)"')

    for doc in docs:
        if not doc.exists():
            continue
        body = doc.read_text()
        targets = link_re.findall(body) + src_re.findall(body)
        for t in targets:
            if t.startswith(('http://', 'https://', 'mailto:', '#', 'data:')):
                continue
            path = t.split('#')[0]
            if not path:
                continue
            resolved = (doc.parent / path).resolve()
            if not resolved.exists():
                fail(f'{doc.relative_to(ROOT)}: link target does not exist -> {t}')


def main() -> int:
    for svg in SVGS:
        if not svg.exists():
            fail(f'missing {svg.relative_to(ROOT)} -- run: '
                 f'python3 scripts/gen_architecture.py')
    if not failures:
        check_svgs_current()
        check_backends_in_diagram()
        check_files_named_in_diagram()
    check_markdown_links()

    if failures:
        print('Documentation checks FAILED:\n', file=sys.stderr)
        for f in failures:
            print(f'  - {f}', file=sys.stderr)
        return 1
    print('documentation checks passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
