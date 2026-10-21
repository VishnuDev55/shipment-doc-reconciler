"""Shared bootstrap for the offline scripts.

Every script in this directory that does not make a network call needs the repo
root, both as a path and on ``sys.path`` so ``app`` and ``fixtures`` import
before the package is installed. The path is derived once here; each script
inserts it and imports the rest from this module.

The live scripts (``live_*.py``, ``run_stress.py``, ``probe_live.py``,
``capture_*.py``, ``eval_model.py``, ``prewarm.py``, ``spend.py``,
``live_compare.py``) do not import this: they manage their own keys and
settings, and this module must never grow anything that would make an offline
script reach for the network.
"""

from __future__ import annotations

import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def html_to_pdf_bytes(html: str) -> bytes:
    """Render an HTML string to PDF bytes with xhtml2pdf.

    Shared by the two offline builders (``build_fixtures.py`` and
    ``build_stress_inputs.py``) so "render then rasterise" is one code path
    instead of two that can drift apart.
    """
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    pisa.CreatePDF(io.StringIO(html), dest=buf)
    pdf = buf.getvalue()
    if len(pdf) < 600:
        raise RuntimeError("render produced a suspiciously small PDF")
    return pdf
