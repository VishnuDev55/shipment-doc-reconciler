# Single stage: the app is pure Python with wheel-only dependencies, so there is
# no build stage to separate. No poppler, no node, NO apt-get at all.
#
# The "no system packages" claim was audited, not assumed (see PROGRESS.md):
# all 69 transitive dependencies resolve to binary wheels for cp311 /
# manylinux_2_28 x86-64 (none to sdists, so nothing compiles in the image), and
# every DT_NEEDED entry across all 210 of their shared-object edges is either
# (a) vendored inside the wheel, (b) on the manylinux_2_28 system whitelist, or
# (c) the dynamic loader itself.
#
# Specifically checked, because Pillow is the usual trap:
#   libjpeg / libtiff / libopenjp2 / libwebp / libfreetype / libharfbuzz /
#   liblzma / libzstd / libxcb   -> VENDORED by Pillow, resolved via
#                                    RPATH $ORIGIN/../pillow.libs
#   libz.so.1                     -> NOT vendored, taken from the base image.
#                                    Safe: it is on the manylinux_2_28 whitelist
#                                    and apt itself links against it, so any
#                                    Debian image that can run pip has it.
#   libstdc++.so.6 / libgcc_s     -> whitelisted; present in python:3.11-slim.
#   pypdfium2's libpdfium.so      -> self-contained (only libc/libm/libgcc_s).
#
# Built and run successfully on 2026-10-02 (python:3.11-slim, 475 MB, exit 0, no
# apt-get), confirming the static audit above: every dependency resolved to a
# wheel and nothing needed a system package at runtime.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web
COPY fixtures ./fixtures
COPY scripts ./scripts

# Unprivileged user; runs/ is the only writable path.
RUN useradd -m appuser && mkdir -p /app/runs && chown -R appuser:appuser /app
USER appuser

# Live by default: paste keys into the Configure panel in the browser, or pass
# NVIDIA_API_KEY / NEBIUS_API_KEY here. DEMO_MODE=true instead replays the
# bundled cache (real captured model responses + authored fallbacks), which is
# how the hosted keyless demo runs.
ENV DEMO_MODE=false \
    RUNS_DIR=/app/runs \
    CACHE_DIR=/app/fixtures/responses \
    VISION_PROVIDER=nvidia

EXPOSE 8000

# No curl in a -slim image and no apt-get by design, so health is probed with the
# stdlib already in the image.
#
# /api/health answers 200 whenever the app is serving, even with no API key: a
# container must not be killed for being unconfigured, since the operator is
# meant to paste keys into Configure. Provider readiness is reported inside the
# body (vision.key_present, summary.key_present, models[]) rather than as a
# non-2xx, so an orchestrator restarting on failure does not treat a keyless
# instance as a crashed one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# Live usage:
#   docker run -p 8000:8000 \
#     -e DEMO_MODE=false \
#     -e NVIDIA_API_KEY=... \
#     -e NEBIUS_API_KEY=... \
#     shipment-doc-reconciler
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
