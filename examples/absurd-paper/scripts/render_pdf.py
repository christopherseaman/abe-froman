#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown>=3.6", "weasyprint>=62.0"]
# ///
"""Render a markdown paper to a journal-styled PDF.

Deterministic: Markdown → HTML (python-markdown) → PDF (WeasyPrint).
Styling is two-column body with a single-column title block, serif
body text, small-caps section headers. PEP 723 inline deps mean no
project-wide dependency install — `uv run --script` resolves on demand.

Usage: render_pdf.py <input.md> <output.pdf>
"""
from __future__ import annotations

import sys
from pathlib import Path

import markdown as md
from weasyprint import CSS, HTML

CSS_STYLE = """
@page {
  size: A4;
  margin: 22mm 18mm 22mm 18mm;
  @bottom-center { content: counter(page); font: 9pt 'Serif'; color: #555; }
}

html { font-family: 'Times New Roman', 'DejaVu Serif', serif; font-size: 10.5pt;
       line-height: 1.35; color: #111; }

/* Title block is full-width; body columns kick in after. */
h1:first-of-type { column-span: all; font-size: 16pt; font-weight: 700;
                   text-align: center; margin: 0 0 6mm 0; line-height: 1.2; }
h1:first-of-type + p { column-span: all; text-align: center; margin: 0 0 2mm 0; }

body { column-count: 2; column-gap: 8mm; column-rule: none;
       text-align: justify; hyphens: auto; }

h2 { font-variant: small-caps; font-size: 10.5pt; font-weight: 700;
     margin: 4mm 0 1mm 0; letter-spacing: 0.04em; border-bottom: 0.5pt solid #333;
     padding-bottom: 0.5mm; break-after: avoid; }
h3 { font-size: 10.5pt; font-style: italic; font-weight: 600;
     margin: 3mm 0 0.5mm 0; break-after: avoid; }

p { margin: 0 0 2mm 0; text-indent: 3mm; }
h2 + p, h3 + p { text-indent: 0; }

ul { margin: 1mm 0 2mm 4mm; padding: 0; font-size: 9.5pt; }
li { margin-bottom: 0.8mm; }

strong { font-weight: 700; }
em { font-style: italic; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 9.5pt; }

/* References should flow across both columns as a continuous list */
h2:last-of-type + ul { font-size: 9pt; }
"""


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: render_pdf.py <input.md> <output.pdf>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    text = src.read_text()
    html_body = md.markdown(text, extensions=["extra", "smarty"])
    html_doc = f"<!doctype html><meta charset='utf-8'><body>{html_body}</body>"
    dst.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_doc).write_pdf(str(dst), stylesheets=[CSS(string=CSS_STYLE)])
    print(f"wrote {dst.stat().st_size} bytes to {dst.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
