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

import hashlib
import io
import json
import random
import shutil
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._common import ROOT, html_to_pdf_bytes  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402

from app.cache import cache_key  # noqa: E402
from app.config import LINEITEMS_PROMPT_VERSION, PROMPT_VERSION  # noqa: E402
from app.ingest import ingest  # noqa: E402
from app.models import extraction_json_schema  # noqa: E402
from app.textlayer import page_texts_from_pdf, save_sidecar  # noqa: E402
from fixtures.planted import CASES, extraction, line_items, render_page  # noqa: E402
from fixtures.shipments import SHIPMENTS  # noqa: E402

IMAGE_DIR = ROOT / "fixtures" / "images"
CACHE_DIR = ROOT / "fixtures" / "responses"
# The text the renderer had before the rasteriser discarded it. Kept so the
# fixture set has something to check a quoted line against that is not the model
# that wrote the quote; see app/textlayer.py.
TEXT_DIR = ROOT / "fixtures" / "text"

# The planted-error bundle, in its own tree: it is scored, not shown.
PLANTED_DIR = ROOT / "fixtures" / "planted"
PLANTED_IMG = PLANTED_DIR / "images"
PLANTED_TEXT = PLANTED_DIR / "text"
PLANTED_CACHE = PLANTED_DIR / "responses"


def pdf_to_pages(pdf: bytes, *, scale: float = 2.0) -> list[Image.Image]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    try:
        if len(doc) == 0:
            raise RuntimeError("render produced zero pages")
        return [doc[i].render(scale=scale).to_pil() for i in range(len(doc))]
    finally:
        doc.close()


def html_to_pages(html: str, *, scale: float = 2.0) -> list[Image.Image]:
    return pdf_to_pages(html_to_pdf_bytes(html), scale=scale)


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

    # Sidecars belong to the images as much as the images do: a text file that
    # outlives the page it describes would let the verifier check a quote against
    # a document that no longer exists, which is the exact failure mode it exists
    # to catch, in the one place we are supposed to be able to trust.
    if TEXT_DIR.is_dir():
        for f in TEXT_DIR.iterdir():
            if f.is_file() and f.suffix == ".json":
                f.unlink()
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

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


def build_planted() -> int:
    """The planted-error bundle: pages, text sidecars and authored responses.

    A separate directory tree on purpose. These fixtures exist to be scored, and
    mixing them into the demo cache would let a test set answer a demo question.
    Both the scalar and the goods-line contract are authored here, because three
    of the four arithmetic rules read the rows -- and a bundle that could not
    exercise them would report a clean run for the wrong reason.

    Pages are written once and shared by content. Most cases reuse the same
    invoice, and a cache key is a hash of the page images, so a page that appears
    in six shipments has exactly one response for it. That is also why a case that
    claims a value its page does not print has to differ from an otherwise
    identical page in some *printed* way (see `invented_note`): two authored
    responses for one set of page hashes would make the score depend on which file
    the cache happened to read first.
    """
    for directory in (PLANTED_IMG, PLANTED_TEXT, PLANTED_CACHE):
        if directory.is_dir():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    model = AUTHORED_LABEL
    index: dict[str, dict] = {}
    written = 0

    for case in CASES:
        docs = []
        for doc_type, doc in case["docs"]:
            html = render_page(doc_type, doc)
            stem = "page_" + hashlib.sha256(html.encode()).hexdigest()[:12]

            if not (PLANTED_IMG / f"{stem}.manifest.json").is_file():
                pdf = html_to_pdf_bytes(html)
                pages = [degrade(p, random.Random(stem)) for p in pdf_to_pages(pdf)]
                sidecar = [replace(t, source="fixture_sidecar")
                           for t in page_texts_from_pdf(pdf, max_pages=max(len(pages), 1))]
                if sidecar:
                    save_sidecar(PLANTED_TEXT / f"{stem}.json", sidecar)
                if len(pages) == 1:
                    upload = f"{stem}.png"
                    pages[0].save(PLANTED_IMG / upload, "PNG", optimize=True)
                else:
                    upload = f"{stem}.pdf"
                    save_pages_as_pdf(pages, PLANTED_IMG / upload)

                ingested = ingest(upload, (PLANTED_IMG / upload).read_bytes())
                hashes = [p.sha256 for p in ingested]
                _write_authored(
                    PLANTED_CACHE / f"{cache_key(hashes, model)}.json", model,
                    PROMPT_VERSION, hashes,
                    build_extraction(doc_type, extraction(doc), len(ingested)),
                )
                items = line_items(doc)
                if items is not None:
                    _write_authored(
                        PLANTED_CACHE / f"{cache_key(hashes, model, prompt_version=LINEITEMS_PROMPT_VERSION)}.json",
                        model, LINEITEMS_PROMPT_VERSION, hashes, {"items": items},
                    )
                (PLANTED_IMG / f"{stem}.manifest.json").write_text(json.dumps(
                    {"doc_type": doc_type, "file": upload, "pages": len(ingested)},
                    indent=2))
                written += 1

            manifest = json.loads(
                (PLANTED_IMG / f"{stem}.manifest.json").read_text())
            docs.append({"doc_type": doc_type, "file": manifest["file"],
                         "pages": manifest["pages"]})

        index[case["name"]] = {
            "group": case["group"], "category": case["category"],
            "expect": case["expect"], "note": case["note"], "documents": docs,
        }

    (PLANTED_DIR / "index.json").write_text(json.dumps(
        {
            "generated_by": "scripts/build_fixtures.py",
            "prompt_version": PROMPT_VERSION,
            "lineitems_prompt_version": LINEITEMS_PROMPT_VERSION,
            "model": model,
            "note": (
                "Hand-authored planted-error set. The pages, their text and the "
                "model responses all come from fixtures/planted.py, and no entry "
                "here was produced by a model or by an API call. Pages are stored "
                "once and shared by content hash."
            ),
            "cases": index,
        },
        indent=2,
    ))
    return written


def _write_authored(path: Path, model: str, prompt_version: str,
                    hashes: list[str], response: dict) -> None:
    path.write_text(json.dumps({
        "cache_key": path.stem,
        "model": model,
        "prompt_version": prompt_version,
        "page_sha256": hashes,
        "response": response,
        "_provenance": (
            "HAND-AUTHORED fixture response. Not produced by any model. Seeds "
            "DEMO_MODE for the planted-error evaluation so it runs offline."
        ),
    }, indent=2))


def _cache_provenance() -> tuple[int, int]:
    """Count the two kinds of cache entry, so the note cannot claim one kind."""
    authored = live = 0
    for path in CACHE_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        marker = str(payload.get("_provenance", "")).upper()
        if "HAND-AUTHORED" in marker:
            authored += 1
        elif marker:
            live += 1
    return authored, live


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
            pdf = html_to_pdf_bytes(doc["html"](shipment))
            pages = [degrade(p, rng) for p in pdf_to_pages(pdf)]
            stem = Path(doc["filename"]).stem

            # Save the text before the degradation reaches it: this is what the
            # page said as typeset, and it is the only thing on the fixture side
            # that the vision model never had a hand in.
            raw_texts = page_texts_from_pdf(pdf, max_pages=max(len(pages), 1))
            if raw_texts:
                save_sidecar(
                    TEXT_DIR / f"{stem}.json",
                    [replace(t, source="fixture_sidecar") for t in raw_texts],
                )

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

    authored, live = _cache_provenance()
    (ROOT / "fixtures" / "index.json").write_text(
        json.dumps(
            {
                "generated_by": "scripts/build_fixtures.py",
                "prompt_version": PROMPT_VERSION,
                "model": model,
                "note": (
                    f"Fixture images and the {authored} hand-authored cache entries "
                    "are synthetic and prove nothing about live API behaviour. "
                    f"The {live} live entries are captured model output from "
                    "NVIDIA and Nebius, replayed in DEMO_MODE: real model reads "
                    "of these pages, not calls made now."
                ),
                "provenance": {"authored": authored, "live": live},
                "shipments": index,
            },
            indent=2,
        )
    )
    print(f"\n{written} documents rendered; cache written to {CACHE_DIR}")
    planted = build_planted()
    print(f"{planted} planted-error documents rendered under "
          f"{PLANTED_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
