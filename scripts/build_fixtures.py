"""Build the synthetic fixtures and the DEMO_MODE response cache.

    python scripts/build_fixtures.py

For each document in each shipment:
  1. render the HTML template to a PDF (xhtml2pdf, pure wheel)
  2. rasterise the PDF to page images (pypdfium2)
  3. degrade them deterministically to look like phone photos
  4. write the PNGs to fixtures/images/
  5. run them through the REAL ingestion pipeline and key the cached response
     on the resulting page hashes — so a cache hit in DEMO_MODE is produced by
     exactly the same code path a live run would take.

Degradation is seeded, so re-running this script reproduces byte-identical
PNGs and therefore stable cache keys. The one exception is the multi-page
image-only PDFs PIL writes for 2-page documents: PIL stamps a CreationDate and
ModDate into them, so their bytes change on every run even when the embedded page
images do not. That is cosmetic — cache keys are taken from the re-encoded JPEGs
the ingester produces, never from the PDF's bytes — but it does mean `git status`
after a re-run shows those PDFs as modified when nothing that matters changed.
"""

from __future__ import annotations

import io
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageFilter  # noqa: E402

from app.cache import cache_key  # noqa: E402
from app.config import PROMPT_VERSION  # noqa: E402
from app.ingest import ingest  # noqa: E402
from app.models import extraction_json_schema  # noqa: E402
from fixtures.shipments import SHIPMENTS  # noqa: E402

IMAGE_DIR = ROOT / "fixtures" / "images"
CACHE_DIR = ROOT / "fixtures" / "responses"


def html_to_pages(html: str, *, scale: float = 2.0) -> list[Image.Image]:
    from xhtml2pdf import pisa

    import pypdfium2 as pdfium

    buf = io.BytesIO()
    pisa.CreatePDF(io.StringIO(html), dest=buf)
    pdf = buf.getvalue()
    if len(pdf) < 600:
        raise RuntimeError("render produced a suspiciously small PDF")
    doc = pdfium.PdfDocument(pdf)
    try:
        if len(doc) == 0:
            raise RuntimeError("render produced zero pages")
        return [doc[i].render(scale=scale).to_pil() for i in range(len(doc))]
    finally:
        doc.close()


def degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    """Simulate a phone photo: slight rotation, JPEG artefacts, mild noise."""
    angle = rng.uniform(-1.6, 1.6)
    img = img.rotate(angle, expand=True, resample=Image.BICUBIC, fillcolor=(250, 250, 250))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=rng.choice([72, 78, 82]))
    buf.seek(0)
    img = Image.open(buf).convert("RGB")

    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.004)):
        x, y = rng.randrange(w), rng.randrange(h)
        r, g, b = px[x, y]
        d = rng.randint(-14, 14)
        px[x, y] = (
            max(0, min(255, r + d)),
            max(0, min(255, g + d)),
            max(0, min(255, b + d)),
        )
    return img.filter(ImageFilter.SMOOTH_MORE)


def build_extraction(doc_type: str, spec: dict, page_count: int) -> dict:
    """Author the model response for one document.

    A None entry means the field is genuinely not on this document type:
    present=false, value=null. page_index is clamped to the real page count so
    the fixture can never claim a value came from a page that does not exist.
    """
    fields = {}
    for key, item in spec.items():
        if item is None:
            fields[key] = {
                "present": False,
                "value": None,
                "evidence_text": "",
                "page_index": 1,
                "confidence": 0.0,
            }
        else:
            value, evidence, page, conf = item
            fields[key] = {
                "present": True,
                "value": value,
                "evidence_text": evidence,
                "page_index": min(page, page_count),
                "confidence": conf,
            }
    return {
        "doc_type": doc_type,
        "doc_type_reason": f"Header and layout indicate a {doc_type.replace('_', ' ')}.",
        "fields": fields,
    }


def _reset_output_dirs() -> None:
    """Clear what this script owns -- and only what it owns.

    Without this, changing a template leaves orphaned cache entries keyed on
    images that no longer exist, and a demo silently mixes generations.

    The cache directory is NOT wiped wholesale: it also holds live-captured
    model responses (fixtures/responses/*.json) and AI narratives
    (fixtures/responses/narratives/), which are real data this script did not
    create and must not destroy. Only entries this generator authored -- marked
    HAND-AUTHORED -- are eligible for removal.
    """
    if IMAGE_DIR.is_dir():
        removed = 0
        for f in IMAGE_DIR.iterdir():
            if f.is_file() and f.suffix in {".png", ".pdf"}:
                f.unlink()
                removed += 1
        if removed:
            print(f"cleared {removed} generated image(s) from {IMAGE_DIR.relative_to(ROOT)}")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    removed = kept = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            payload = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            f.unlink()
            removed += 1
            continue
        if "HAND-AUTHORED" in str(payload.get("_provenance", "")).upper():
            f.unlink()
            removed += 1
        else:
            kept += 1
    print(f"cache: replaced {removed} authored entr(ies), preserved {kept} "
          f"live-captured entr(ies) under {CACHE_DIR.relative_to(ROOT)}")


def save_pages_as_pdf(pages: list[Image.Image], out: Path) -> None:
    """Bundle degraded page images into one PDF.

    The app uploads one file per document, so a multi-page document must be a
    single artifact. PIL writes a PDF that embeds each page image losslessly.
    """
    first, *rest = pages
    first.save(out, format="PDF", save_all=True, append_images=rest)


# Authored fixtures are looked up by page hashes, not by cache key, so this
# label only needs to be stable and unique per document. It deliberately does
# NOT name a model: the same fixture serves whichever vision provider is
# configured, and baking a model id in here would re-create the bug where
# changing NEBIUS_MODEL silently broke DEMO_MODE.
AUTHORED_LABEL = "hand-authored-fixture"


def main() -> int:
    model = AUTHORED_LABEL
    _reset_output_dirs()

    # The schema we send and the fixtures we author must describe the same six
    # fields, or DEMO_MODE would be testing a shape the live path never sees.
    schema_fields = set(extraction_json_schema()["properties"]["fields"]["properties"])
    for shipment_name, shipment in SHIPMENTS.items():
        for doc_type, doc in shipment["docs"].items():
            authored = set(doc["extraction"](shipment))
            if authored != schema_fields:
                raise AssertionError(
                    f"{shipment_name}/{doc_type} extraction keys {sorted(authored)} "
                    f"do not match schema {sorted(schema_fields)}"
                )

    index: dict[str, dict] = {}
    written = 0

    for shipment_name, shipment in SHIPMENTS.items():
        rng = random.Random(shipment_name)  # seeded per shipment -> reproducible
        index[shipment_name] = {"documents": []}

        for doc_type, doc in shipment["docs"].items():
            pages = [degrade(p, rng) for p in html_to_pages(doc["html"](shipment))]
            stem = Path(doc["filename"]).stem

            # One document == one upload artifact. A multi-page document is
            # bundled into a PDF, because that is what a user would actually
            # upload; keying on separately-ingested pages would never match a
            # real run.
            if len(pages) == 1:
                upload_name = f"{stem}.png"
                pages[0].save(IMAGE_DIR / upload_name, "PNG", optimize=True)
            else:
                upload_name = f"{stem}.pdf"
                save_pages_as_pdf(pages, IMAGE_DIR / upload_name)

            ingested = ingest(upload_name, (IMAGE_DIR / upload_name).read_bytes())
            key = cache_key([p.sha256 for p in ingested], model)
            filenames = [upload_name]
            all_pages = ingested

            extraction = build_extraction(doc_type, doc["extraction"](shipment), len(all_pages))
            (CACHE_DIR / f"{key}.json").write_text(
                json.dumps(
                    {
                        "cache_key": key,
                        "model": model,
                        "prompt_version": PROMPT_VERSION,
                        "page_sha256": [p.sha256 for p in all_pages],
                        "response": extraction,
                        "_provenance": (
                            "HAND-AUTHORED fixture response. Not produced by any model. "
                            "Seeds DEMO_MODE so the app can be built and shown without a "
                            "live Nebius API key."
                        ),
                    },
                    indent=2,
                )
            )

            index[shipment_name]["documents"].append(
                {
                    "doc_type": doc_type,
                    "files": filenames,
                    "pages": len(all_pages),
                    "cache_key": key,
                }
            )
            written += 1
            print(f"  {shipment_name}/{doc_type}: {len(all_pages)} page(s) -> {key}")

    (ROOT / "fixtures" / "index.json").write_text(
        json.dumps(
            {
                "generated_by": "scripts/build_fixtures.py",
                "prompt_version": PROMPT_VERSION,
                "model": model,
                "note": (
                    "Fixtures and cached responses are hand-authored synthetic data. "
                    "They are NOT model output and prove nothing about live API behaviour."
                ),
                "shipments": index,
            },
            indent=2,
        )
    )
    print(f"\n{written} documents rendered; cache written to {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
