#!/usr/bin/env python3
"""
Generate a printer‑friendly architecture diagram from a Mermaid definition.

This script:
- Ensures Playwright + Chromium are available (installs if missing)
- Renders the Mermaid diagram via a local HTML and headless Chromium
- Exports to a single‑page PDF (Letter by default) and an SVG

Usage (from repo root):
  python mermaid_architecture.py                 # outputs architecture.pdf + architecture.svg
  python mermaid_architecture.py --out out.pdf   # custom PDF filename
  python mermaid_architecture.py --size A4 --landscape

Requires internet access to load Mermaid from a CDN.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
import subprocess


MERMAID_CODE = r"""
flowchart LR
  subgraph Sources["Data Sources"]
    A1[run.py (Live)]
  end

  subgraph Processes["Processes"]
    B1[Streamer (IB/Kraken)]
    B3[Strategy (SMA Cross)]
    B4[OrderRouter (IB/Kraken)]
    B5[Dash App (Read-only)]
  end

  subgraph Lib["DBActivator (Library)"]
    L1[record_5s_bar<br/>- write 5s<br/>- update 1m ring (vol_up/down)<br/>- persist evolving 1m]
    L2[snapshot(symbol)<br/>- in-memory minute ring]
    L3[flush_minute_bars(df_1m)<br/>- UPSERT 1m (preserve vol_up/down)]
    L4[record_signal_async(...)]
    L5[fetch_signals_new_all()]
    L6[mark_signal_status(...)]
    L7[insert_fill_for_order(...)]
  end

  subgraph SQLite["SQLite (WAL)"]
    direction TB
    S1[(data/trades.db)]
    S1a[[ohlcv_5s]]
    S1b[[ohlcv_1m<br/>open,high,low,close,volume,<br/>vol_up,vol_down]]
    S1c[[fills]]
    S1 --- S1a
    S1 --- S1b
    S1 --- S1c

    S2[(data/signals_orders.db)]
    S2a[[signals<br/>UNIQUE(symbol, ts_utc, strategy)]]
    S2 --- S2a

    S3[(data/pnl.db)]
    S3a[[pnl<br/>(realized, unrealized)]]
    S3 --- S3a
  end

  %% Live path
  A1 -->|starts| B1
  B1 -->|5s ticks| L1
  L1 -->|UPSERT 5s| S1a
  L1 -->|update ring| L2
  L1 -->|persist evolving 1m| L3
  L3 -->|UPSERT 1m| S1b

  %% Strategy (signals only)
  B3 -->|read bars (memory or DB)| L2
  B3 -->|or read 1m| S1b
  B3 -->|emit signals| L4
  L4 -->|UPSERT signals| S2a

  %% Order router (live only)
  B4 -->|poll NEW| L5
  L5 -->|read NEW| S2a
  B4 -->|place orders, mark SENT| L6
  L6 -->|UPDATE status| S2a

  %% Dash (read-only)
  B5 -->|read 1m| S1b
  B5 -->|read signals| S2a
  B5 -->|read realized PnL| S3a
"""


HTML_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <style>
      html, body { background: #ffffff; margin: 0; padding: 24px; }
      .mermaid { width: 100%; }
      /* Force monochrome-ish styling for printability */
      svg { background: #ffffff; }
    </style>
  </head>
  <body>
    <div class="mermaid">%%CODE%%</div>
    <script>
      mermaid.initialize({ startOnLoad: true, theme: 'base', themeVariables: { background: '#ffffff' } });
    </script>
  </body>
  </html>
"""


def ensure_playwright() -> None:
    try:
        import playwright  # noqa: F401
    except Exception:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright>=1.45"], stdout=sys.stdout)
    # Ensure Chromium is installed
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright>=1.45"], stdout=sys.stdout)
    # Install browser if needed
    try:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"], stdout=sys.stdout)
    except Exception:
        # Try without --with-deps on macOS
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"], stdout=sys.stdout)


def render_mermaid_to_pdf_svg(code: str, out_pdf: Path, out_svg: Path, page_size: str, landscape: bool) -> None:
    from playwright.sync_api import sync_playwright

    html = HTML_TEMPLATE.replace("%%CODE%%", code)
    with tempfile.TemporaryDirectory() as tmpd:
        html_path = Path(tmpd) / "diagram.html"
        html_path.write_text(html, encoding="utf-8")

        pdf_opts = {
            "path": str(out_pdf),
            "format": page_size,
            "print_background": False,
            "landscape": landscape,
            "margin": {"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
        }

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1800, "height": 1200})
            page.goto(f"file://{html_path}")
            # Wait a bit for Mermaid to render
            page.wait_for_timeout(1000)
            # Export SVG by grabbing the first <svg>
            svg_content = page.locator("svg").first.evaluate("node => node.outerHTML")
            out_svg.write_text(svg_content, encoding="utf-8")
            # Export single-page PDF
            page.pdf(**pdf_opts)
            browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a printer-friendly Mermaid architecture diagram")
    ap.add_argument("--out", default="architecture.pdf", help="Output PDF path (default: architecture.pdf)")
    ap.add_argument("--svg", default="architecture.svg", help="Output SVG path (default: architecture.svg)")
    ap.add_argument("--size", default="Letter", choices=["Letter", "A4"], help="Page size (PDF)")
    ap.add_argument("--landscape", action="store_true", help="Landscape orientation for PDF")
    args = ap.parse_args()

    out_pdf = Path(args.out).resolve()
    out_svg = Path(args.svg).resolve()

    ensure_playwright()
    render_mermaid_to_pdf_svg(MERMAID_CODE, out_pdf, out_svg, args.size, args.landscape)
    print(f"Wrote: {out_pdf}\nWrote: {out_svg}")


if __name__ == "__main__":
    main()
