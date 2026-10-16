"""What ran, per step, and what it cost -- one honest table.

The web panel, the standalone HTML and the PDF all answer the same question:
"which model produced this, and did it run now?" They must agree on the answer,
so it is computed here once and rendered three times. Everything reads fields
the pipeline already recorded (`Report.documents`, `run`, `narrative`,
`briefing`, each comparison's `adjudication`); nothing is re-decided, and
nothing here can change any of them.
"""

from __future__ import annotations

from .models import Report, TokenUsage

# Report.source / per-step source strings -> the words a reader sees. The value
# space is wide ('live_api', 'cached_live', 'cached_authored', 'generated',
# 'cached', 'authored', 'none') because each stage names its own provenance;
# this is the one place they are translated to a human sentence.
SOURCE_WORDS = {
    "live_api": "live call on this run",
    "cached_live": "cached model output",
    "cached_authored": "hand-authored fixture",
    "authored": "hand-authored fixture",
    "generated": "live call on this run",
    "cached": "cached",
    "none": "no answer",
}


def source_word(source: str) -> str:
    return SOURCE_WORDS.get(source, source or "—")


def _usage_total(usage: TokenUsage | None) -> int:
    return usage.total if usage else 0


def model_steps(report: Report) -> list[dict]:
    """Every model step in run order, as render-neutral rows.

    A row is `{step, provider, model, source, tries, usage, latency_ms}`.
    Extraction is per document; the consensus read, summary, briefing and each
    advisor opinion are their own rows, only when one actually happened.
    """
    steps: list[dict] = []
    run = report.run

    for doc in report.documents:
        steps.append({
            "step": doc.label,
            "provider": doc.provider or "",
            "model": doc.model or "",
            "source": doc.source or "none",
            "tries": doc.attempts,
            "usage": doc.usage,
            "latency_ms": doc.timings_ms,
        })
        consensus = doc.consensus
        if consensus and consensus.status in ("ok", "cached", "failed"):
            steps.append({
                "step": f"second reader · {doc.label}",
                "provider": consensus.provider or "",
                "model": consensus.model or "",
                "source": consensus.source or consensus.status,
                "tries": 1,
                "usage": consensus.usage,
                "latency_ms": consensus.latency_ms,
            })

    narrative = report.narrative
    if narrative and narrative.status in ("ok", "cached") and narrative.text:
        steps.append({
            "step": "Executive summary",
            "provider": narrative.provider or run.summary_provider,
            "model": narrative.model or run.summary_model,
            "source": "live_api" if narrative.source == "generated"
                      else "cached" if narrative.source == "cached"
                      else narrative.source,
            "tries": 1,
            "usage": narrative.usage,
            "latency_ms": narrative.latency_ms,
        })

    briefing = report.briefing
    if briefing and briefing.status in ("ok", "cached") and briefing.text:
        steps.append({
            "step": "Draft discrepancy email",
            "provider": briefing.provider or run.summary_provider,
            "model": briefing.model or run.summary_model,
            "source": "live_api" if briefing.source == "generated"
                      else "cached" if briefing.source == "cached"
                      else briefing.source,
            "tries": 1,
            "usage": briefing.usage,
            "latency_ms": briefing.latency_ms,
        })

    for comp in report.comparisons:
        adjudication = comp.adjudication
        if adjudication and adjudication.status in ("ok", "cached"):
            steps.append({
                "step": f"Second opinion · {comp.label}",
                "provider": adjudication.provider,
                "model": adjudication.model,
                "source": "live_api" if adjudication.source == "generated"
                          else adjudication.source,
                "tries": 1,
                "usage": adjudication.usage,
                "latency_ms": adjudication.latency_ms,
            })

    return steps


def totals(steps: list[dict]) -> tuple[int, int]:
    tokens = sum(_usage_total(s["usage"]) for s in steps)
    ms = sum(s["latency_ms"] or 0 for s in steps)
    return tokens, ms


def powered_by(report: Report) -> str:
    """The branding line, made honest by the call count and the provenance.

    "Powered by Nebius Token Factory + NVIDIA Nemotron" is what the submission
    is built on; the suffix says whether any of that actually ran now, because a
    replayed answer wearing a live badge is exactly the overclaim this app
    exists to avoid. Hand-authored values are a third case: they are neither a
    live call nor a replayed one, and calling them "replayed" would be as wrong
    as calling them "live".
    """
    calls = report.run.live_calls
    if calls:
        suffix = f"{calls} live API call(s) on this run."
    elif report.run.result_source == "authored":
        suffix = "no live call on this run — values are hand-authored, not model output."
    else:
        suffix = "no live call on this run — responses were replayed from cache."
    return f"Powered by Nebius Token Factory + NVIDIA Nemotron · {suffix}"


def cost_line(report: Report, tokens: int, ms: int) -> str:
    secs = ms / 1000
    if tokens == 0:
        return f"{secs:.1f} s total · no tokens spent on this run"
    rate = report.run.usd_per_mtok
    if rate > 0:
        return (f"{tokens} tokens · {secs:.1f} s · ≈$"
                f"{(tokens / 1e6) * rate:.5f} at ${rate}/1M (configured)")
    return f"{tokens} tokens · {secs:.1f} s · price not configured, so none is shown"
