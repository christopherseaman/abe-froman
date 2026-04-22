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
/* arXiv-ish single-column preprint style. Computer Modern isn't shipped
   by default, so fall back through Latin Modern / Nimbus Roman / Times. */

@page {
  size: letter;
  margin: 25mm 25mm 25mm 25mm;
  @bottom-center { content: counter(page); font: 10pt 'Latin Modern Roman',
                   'Nimbus Roman', 'Times New Roman', serif; color: #222; }
}

html { font-family: 'Latin Modern Roman', 'Nimbus Roman', 'Times New Roman',
       'DejaVu Serif', serif; font-size: 11pt; line-height: 1.35; color: #111; }
body { text-align: justify; hyphens: auto; counter-reset: secnum; }

/* --- Title block --- */
h1 { font-size: 17pt; font-weight: 700; text-align: center;
     margin: 0 0 6mm 0; line-height: 1.2; letter-spacing: 0.01em; }
h1 + p { text-align: center; margin: 0 0 2mm 0; font-size: 11pt; }

/* --- Abstract (first h2 in document) --- */
h2:first-of-type {
  font-size: 11pt; font-weight: 700; text-align: center;
  margin: 8mm 0 2mm 0; border: none;
  counter-increment: secnum 0;
}
h2:first-of-type::before { content: ""; }
h2:first-of-type + p {
  margin: 0 12mm 6mm 12mm; font-size: 10pt;
  text-align: justify; text-indent: 0;
}

/* --- References (last h2) — unnumbered --- */
h2:last-of-type { counter-increment: secnum 0; }
h2:last-of-type::before { content: ""; }
h2:last-of-type + ul {
  font-size: 9.5pt;
  padding-left: 6mm;
}
h2:last-of-type + ul li {
  text-indent: -4mm; padding-left: 4mm;
  margin-bottom: 1mm;
}

/* --- Numbered body sections --- */
h2 { counter-increment: secnum;
     font-size: 12pt; font-weight: 700; text-align: left;
     margin: 5mm 0 2mm 0; break-after: avoid; }
h2::before { content: counter(secnum) "  "; }

h3 { font-size: 11pt; font-style: italic; font-weight: 600;
     margin: 3mm 0 1mm 0; break-after: avoid; }

p { margin: 0 0 2mm 0; text-indent: 5mm; }
h1 + p, h2 + p, h3 + p, li > p:first-child { text-indent: 0; }

ul { margin: 1mm 0 2mm 4mm; padding: 0; }
li { margin-bottom: 0.8mm; }

strong { font-weight: 700; }
em { font-style: italic; }
code { font-family: 'Latin Modern Mono', 'DejaVu Sans Mono', monospace;
       font-size: 10pt; }
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
