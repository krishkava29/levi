"""Turn raw signal values into pillar scores, a Levi Score, and a Levi Alpha.

Levi Score (0-100): how attractive the business looks on characteristics.
Levi Alpha (0-100, experimental): Levi Score discounted for institutional awareness and ownership.
Trajectory fit:     a company under the fund's revenue floor still scores if its
                    growth puts it inside the band within ~24 months - that is the
                    whole point of hunting characteristics instead of EBITDA.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .signals import BY_ID, PILLARS

DEFAULT_PILLAR_WEIGHTS = {
    "love": 1.2, "heat": 1.0, "discover": 1.0, "velocity": 1.3,
    "moat": 0.9, "dealability": 1.2, "financial": 1.0, "risk": 0.8,
}


@dataclass
class ScoreCard:
    score: float
    alpha: float
    coverage: float
    pillars: dict
    contributions: list  # (signal_id, normalised, weight) sorted by impact
    missing: list
    alpha_breakdown: dict

    def top_reasons(self, n: int = 4) -> list[str]:
        good = [c for c in self.contributions if c[1] >= 0.66]
        good.sort(key=lambda c: c[1] * c[2], reverse=True)
        return [BY_ID[c[0]].name for c in good[:n]]

    def concerns(self, n: int = 3) -> list[str]:
        bad = [c for c in self.contributions if c[1] <= 0.33]
        bad.sort(key=lambda c: (1 - c[1]) * c[2], reverse=True)
        return [BY_ID[c[0]].name for c in bad[:n]]


def bayesian_rating(rating: float | None, count: float | None, prior: float = 4.2, m: float = 50) -> float | None:
    """Shrink small-sample ratings toward the prior (IMDb-style)."""
    if rating is None:
        return None
    c = max(float(count or 0), 0.0)
    return (c / (c + m)) * float(rating) + (m / (c + m)) * prior


def revenue_fit(rev_mid: float | None, growth: float | None, band: tuple[float, float]) -> float | None:
    """1.0 inside the band, decays by log-distance outside it.

    Companies below the floor get credit for where they will be in 2 years at their
    current growth rate (discounted 10% for execution risk).
    """
    if not rev_mid or rev_mid <= 0:
        return None
    lo, hi = band

    def fit(r: float) -> float:
        if lo <= r <= hi:
            return 1.0
        edge = lo if r < lo else hi
        return max(0.0, 1.0 - abs(math.log10(r / edge)) / 1.0)  # 0 at 10x outside

    now = fit(rev_mid)
    if growth and growth > 0:
        projected = rev_mid * (1 + min(growth, 3.0)) ** 2
        now = max(now, 0.9 * fit(projected))
    return round(now, 3)


def score(values: dict, pillar_weights: dict | None = None) -> ScoreCard:
    """values: {signal_id: raw_value}. Unknown ids are ignored."""
    pw = dict(DEFAULT_PILLAR_WEIGHTS)
    if pillar_weights:
        pw.update(pillar_weights)

    sums: dict[str, list[float]] = {p: [0.0, 0.0, 0.0] for p in PILLARS}  # weighted, weight_avail, weight_total
    contributions, missing = [], []
    for sid, sig in BY_ID.items():
        sums[sig.pillar][2] += sig.weight
        n = sig.normalise(values.get(sid))
        if n is None:
            missing.append(sid)
            continue
        sums[sig.pillar][0] += n * sig.weight
        sums[sig.pillar][1] += sig.weight
        contributions.append((sid, round(n, 3), sig.weight))

    pillars, num, den, cov_num, cov_den = {}, 0.0, 0.0, 0.0, 0.0
    for p, (w_sum, w_av, w_tot) in sums.items():
        cov_num += w_av * pw.get(p, 1.0)
        cov_den += w_tot * pw.get(p, 1.0)
        if w_av == 0:
            pillars[p] = None
            continue
        ps = w_sum / w_av
        pillars[p] = round(ps * 100, 1)
        # a pillar we only half-observed counts for less
        eff = pw.get(p, 1.0) * (0.5 + 0.5 * w_av / w_tot)
        num += ps * eff
        den += eff

    coverage = cov_num / cov_den if cov_den else 0.0
    raw = num / den if den else 0.0
    # thin evidence cannot produce a top score
    s = raw * (0.65 + 0.35 * min(1.0, coverage / 0.6))

    aware = BY_ID["institutional_awareness"].normalise(values.get("institutional_awareness"))
    founder = values.get("founder_owned")
    unknown_factor = aware if aware is not None else 0.6  # 1 = unknown to buyers
    unawareness_factor = 0.5 + 0.5 * unknown_factor
    founder_factor = 0.6 if founder is False else 1.0
    fit = s * 100

    return ScoreCard(
        score=round(fit, 1), alpha=round(fit * unawareness_factor * founder_factor, 1),
        coverage=round(coverage, 3),
        pillars=pillars, contributions=contributions, missing=missing,
        alpha_breakdown={"fit": fit, "institutional_unawareness_factor": unawareness_factor,
                         "founder_factor": founder_factor},
    )
