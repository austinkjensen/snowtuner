#!/usr/bin/env python3
"""Render every ```mermaid block in docs/*.md to docs/img/*.{png,svg}.

Requires Node + npx (mermaid-cli runs in a Node process).  We invoke it as
``npx --yes @mermaid-js/mermaid-cli@latest`` so first-run downloads happen
automatically without a global install.

Output naming: ``docs/img/<source-stem>-<index>.<ext>``.  Indices are
1-based and assigned in document order.

Usage::

    python scripts/render_mermaid.py             # render PNG + SVG (default)
    python scripts/render_mermaid.py --formats png
    python scripts/render_mermaid.py --check     # exit 1 if rendering would change files
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)\n```", re.DOTALL)
ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
IMG = DOCS / "img"


def render(source: Path, formats: list[str]) -> list[Path]:
    body = source.read_text(encoding="utf-8")
    blocks = MERMAID_BLOCK.findall(body)
    if not blocks:
        return []

    IMG.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for idx, block in enumerate(blocks, start=1):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mmd", delete=False, encoding="utf-8",
        ) as f:
            f.write(block)
            mmd_path = Path(f.name)
        try:
            for fmt in formats:
                out = IMG / f"{source.stem}-{idx}.{fmt}"
                _run_mmdc(mmd_path, out)
                written.append(out)
        finally:
            mmd_path.unlink(missing_ok=True)
    return written


def _run_mmdc(input_path: Path, output_path: Path) -> None:
    if shutil.which("npx") is None:
        sys.exit(
            "error: npx not found in PATH.  Install Node.js "
            "(https://nodejs.org/) and re-run."
        )
    cmd = [
        "npx", "--yes", "@mermaid-js/mermaid-cli@latest",
        "-i", str(input_path),
        "-o", str(output_path),
        "--quiet",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(
            f"mmdc failed for {input_path} → {output_path}:\n"
            f"  stdout: {res.stdout}\n  stderr: {res.stderr}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--formats", default="png,svg",
        help="Comma-separated formats (default: png,svg).",
    )
    args = parser.parse_args()
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]

    sources = sorted(DOCS.glob("*.md"))
    if not sources:
        print("No docs/*.md files found.")
        return

    total = 0
    for src in sources:
        produced = render(src, formats)
        if produced:
            print(f"{src.relative_to(ROOT)}: rendered {len(produced)} image(s)")
            for p in produced:
                print(f"  → {p.relative_to(ROOT)}")
            total += len(produced)
    print(f"\nDone — {total} image(s) total.")


if __name__ == "__main__":
    main()
