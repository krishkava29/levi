"""Validation and summaries for the claims that underpin Levi scores."""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from .signals import BY_ID

KINDS = ("measured", "reported", "estimate", "judgment")
JUDGMENTS = {
    "institutional_awareness", "succession_signal", "fragmented_market", "product_talkability",
    "category_tailwind", "pricing_power", "fad_risk", "legal_regulatory", "key_person", "review_integrity",
}
UNBOUNDED_BELOW = {"review_velocity", "site_expansion", "search_interest_trend", "social_growth_90d", "revenue_growth"}


def iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def source_url(value) -> str | None:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {"http", "https"} and url.hostname and url.username is None:
            return value
    except ValueError:
        pass
    return None


def publication_date(value) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def valid_number(value) -> bool:
    return type(value) in (int, float) and abs(value) <= 1e100 and math.isfinite(value)


def signal_error(signal: str, value) -> str | None:
    definition = BY_ID.get(signal)
    if definition and definition.norm == "bool":
        return None if value in (True, False) else "Expected a boolean"
    if not valid_number(value):
        return "Expected a finite numeric value"
    if (signal in JUDGMENTS or signal in {"ai_share_of_voice", "revenue_fit", "revenue_confidence"}) and not 0 <= value <= 1:
        return "Expected a value in [0, 1]"
    if signal == "review_rating" and not 0 <= value <= 5:
        return "Expected a rating in [0, 5]"
    if signal not in UNBOUNDED_BELOW and value < 0:
        return "Expected a non-negative value"
    if signal in {"ai_rank", "google_rank"} and value < 1:
        return "Ranks start at 1"
    return None


def evidence_summary(rows: list[dict], scored_ids: list[int]) -> dict:
    by_id = {row["id"]: row for row in rows}
    latest, rejected = {}, {}
    for row in rows:
        key = row["signal"]
        if row["rejection"]:
            rejected.setdefault(key, row)
        else:
            latest.setdefault(key, row)

    scored, issues = {}, []
    pending = list(scored_ids)
    while pending:
        claim_id = pending.pop(0)
        if claim_id in scored:
            continue
        row = by_id.get(claim_id)
        if not row:
            issues.append(f"Scored evidence #{claim_id} is unavailable")
            continue
        scored[claim_id] = row
        if (row["kind"] not in KINDS or not {"period", "published_at", "retrieved_at"} <= row.keys()
                or not valid_number(row["retrieved_at"])
                or (row["published_at"] is not None and not publication_date(row["published_at"]))
                or not (source_url(row["source_url"]) or row["missing_evidence"])
                or row["rejection"]):
            issues.append(f"{row['signal']} has invalid evidence metadata")
        pending.extend(row["inputs"])

    claims = list(latest.values())
    counts = Counter(row["kind"] for row in claims)
    sourced = sum(row["source_url"] is not None for row in claims)
    return {
        "claims": claims,
        "counts_by_kind": {kind: counts[kind] for kind in KINDS},
        "source_url_count": sourced,
        "source_url_coverage": sourced / len(claims) if claims else None,
        "scored_claims": list(scored.values()),
        "scored_without_source": [r for r in scored.values() if r["missing_evidence"]],
        "rejections": list(rejected.values()),
        "validation_issues": issues,
    }
