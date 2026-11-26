"""Revenue & P&L triangulation for private companies.

Private companies do not publish financial statements. Anyone who tells you they
can "pull the financials" of a founder-owned business from the internet is
guessing. Levi guesses *transparently*: several independent methods, each with a
range and a confidence, combined into one estimate with an audit trail, plus an
industry-benchmarked indicative P&L. Real statements come later, under NDA,
and Levi's outreach module is built to get you there first.

Method confidence (rough prior):
  reported          0.90  founder/company quoted a number on record
  inc5000_band      0.80  Inc. 5000 verified revenue (band or exact)
  system_sales      0.75  franchisor-disclosed system sales / FDD Item 19
  units_x_auv       0.60  unit count x disclosed or benchmark AUV
  headcount         0.45  employees x revenue/employee benchmark
  reviews           0.30  review count -> customers -> ticket size
  press_estimate    0.50  third-party estimate (Latka, trade press)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Benchmarks: (revenue per employee USD, gross margin, EBITDA margin, review_rate, avg annual spend per customer)
# Sources are public industry rules of thumb (RMA / IBISWorld-style ranges); override per thesis.
BENCHMARKS: dict[str, dict] = {
    "dtc_consumer":       dict(rpe=450_000, gm=0.60, ebitda=0.12, review_rate=0.02, spend=90),
    "food_beverage_cpg":  dict(rpe=550_000, gm=0.38, ebitda=0.10, review_rate=0.005, spend=60),
    "beauty":             dict(rpe=500_000, gm=0.70, ebitda=0.15, review_rate=0.02, spend=120),
    "restaurant":         dict(rpe=75_000, gm=0.68, ebitda=0.12, review_rate=0.01, spend=40, auv=1_500_000),
    "franchisor":         dict(rpe=250_000, gm=0.80, ebitda=0.30, review_rate=0.01, spend=40),
    "med_spa":            dict(rpe=200_000, gm=0.65, ebitda=0.22, review_rate=0.05, spend=1_800, auv=2_000_000),
    "dental":             dict(rpe=160_000, gm=0.60, ebitda=0.18, review_rate=0.04, spend=900, auv=1_500_000),
    "veterinary":         dict(rpe=180_000, gm=0.60, ebitda=0.18, review_rate=0.04, spend=700, auv=2_500_000),
    "fitness":            dict(rpe=90_000, gm=0.70, ebitda=0.20, review_rate=0.03, spend=1_200, auv=1_000_000),
    "home_services":      dict(rpe=200_000, gm=0.48, ebitda=0.15, review_rate=0.06, spend=1_100),
    "car_wash":           dict(rpe=250_000, gm=0.75, ebitda=0.35, review_rate=0.005, spend=250, auv=1_800_000),
    "vertical_saas":      dict(rpe=180_000, gm=0.78, ebitda=0.20, review_rate=0.03, spend=6_000),
    "tech_services":      dict(rpe=150_000, gm=0.40, ebitda=0.15, review_rate=0.02, spend=20_000),
    "default":            dict(rpe=220_000, gm=0.50, ebitda=0.14, review_rate=0.02, spend=500),
}

PRIOR_CONF = {
    "reported": 0.90, "inc5000_band": 0.80, "system_sales": 0.75, "units_x_auv": 0.60,
    "press_estimate": 0.50, "headcount": 0.45, "reviews": 0.30,
}


@dataclass
class Estimate:
    method: str
    low: float
    high: float
    confidence: float
    note: str = ""
    source: str | None = None

    @property
    def mid(self) -> float:
        return math.sqrt(self.low * self.high)  # geometric mid - revenue is log-normal


@dataclass
class RevenueView:
    low: float | None
    mid: float | None
    high: float | None
    confidence: float
    estimates: list = field(default_factory=list)
    pnl: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "low": self.low, "mid": self.mid, "high": self.high, "confidence": self.confidence,
            "methods": [e.__dict__ | {"mid": e.mid} for e in self.estimates], "indicative_pnl": self.pnl,
        }


def bench(model: str | None) -> dict:
    return BENCHMARKS.get((model or "default").lower(), BENCHMARKS["default"])


def collect_estimates(facts: dict, model: str | None) -> list[Estimate]:
    """facts keys (all optional): reported_revenue, reported_source, inc_band (lo,hi),
    system_sales, units, auv, employees, review_count, press_estimate, press_source."""
    b = bench(model)
    out: list[Estimate] = []
    if facts.get("reported_revenue"):
        r = float(facts["reported_revenue"])
        out.append(Estimate("reported", r * 0.9, r * 1.1, PRIOR_CONF["reported"],
                            "on-record figure", facts.get("reported_source")))
    if facts.get("inc_band"):
        lo, hi = facts["inc_band"]
        out.append(Estimate("inc5000_band", float(lo), float(hi), PRIOR_CONF["inc5000_band"], "Inc. 5000 band"))
    if facts.get("system_sales"):
        s = float(facts["system_sales"])
        # franchisor revenue ~ royalties+fees (8-12% of system sales) + company-owned units
        out.append(Estimate("system_sales", s * 0.08, s * 0.25, PRIOR_CONF["system_sales"],
                            f"franchisor take on ${s/1e6:.0f}M system sales"))
    if facts.get("units"):
        auv = float(facts.get("auv") or b.get("auv") or 0)
        if auv:
            u = float(facts["units"])
            conf = PRIOR_CONF["units_x_auv"] if facts.get("auv") else PRIOR_CONF["units_x_auv"] - 0.15
            if (model or "") == "franchisor":
                # units x AUV is *system* sales; the franchisor books royalties + fees
                sysv = u * auv
                out.append(Estimate("units_x_auv", sysv * 0.07, sysv * 0.2, conf - 0.1,
                                    f"franchisor take on {u:.0f} units x ${auv/1e6:.2f}M AUV"))
            else:
                out.append(Estimate("units_x_auv", u * auv * 0.7, u * auv * 1.2, conf,
                                    f"{u:.0f} units x ${auv/1e6:.1f}M AUV"))
    if facts.get("employees"):
        e = float(facts["employees"])
        out.append(Estimate("headcount", e * b["rpe"] * 0.6, e * b["rpe"] * 1.5, PRIOR_CONF["headcount"],
                            f"{e:.0f} employees x ${b['rpe']/1e3:.0f}k/employee"))
    if facts.get("review_count") and (model or "") != "franchisor":
        rc = float(facts["review_count"])
        customers = rc / b["review_rate"]
        out.append(Estimate("reviews", customers * b["spend"] * 0.4, customers * b["spend"] * 2.0,
                            PRIOR_CONF["reviews"], f"{rc:.0f} reviews -> ~{customers:,.0f} customers"))
    if facts.get("press_estimate"):
        p = float(facts["press_estimate"])
        out.append(Estimate("press_estimate", p * 0.75, p * 1.3, PRIOR_CONF["press_estimate"],
                            "third-party estimate", facts.get("press_source")))
    return [e for e in out if e.low > 0 and e.high >= e.low]


def triangulate(facts: dict, model: str | None = None) -> RevenueView:
    ests = collect_estimates(facts, model)
    if not ests:
        return RevenueView(None, None, None, 0.0, [])
    # confidence-weighted mean in log space; conf^4 so an on-record number dominates weak proxies
    w = [e.confidence ** 4 for e in ests]
    wsum = sum(w)
    lmid = sum(math.log(e.mid) * wi for e, wi in zip(ests, w)) / wsum
    llo = sum(math.log(e.low) * wi for e, wi in zip(ests, w)) / wsum
    lhi = sum(math.log(e.high) * wi for e, wi in zip(ests, w)) / wsum
    mid = math.exp(lmid)
    # agreement between methods raises confidence; disagreement lowers it
    best = max(e.confidence for e in ests)
    if len(ests) > 1:
        spread = max(abs(math.log(e.mid) - lmid) for e in ests)
        agree = max(0.0, 1.0 - spread / math.log(4))  # 4x apart -> no agreement bonus
        conf = min(0.95, best + 0.1 * agree * (len(ests) - 1))
    else:
        conf = best
    view = RevenueView(round(math.exp(llo)), round(mid), round(math.exp(lhi)), round(conf, 2), ests)
    view.pnl = indicative_pnl(mid, model)
    return view


def indicative_pnl(revenue: float, model: str | None) -> dict:
    b = bench(model)
    gp = revenue * b["gm"]
    ebitda = revenue * b["ebitda"]
    return {
        "basis": f"industry benchmark for '{model or 'default'}' - NOT company-reported",
        "revenue": round(revenue), "gross_profit": round(gp), "gross_margin": b["gm"],
        "opex_ex_da": round(gp - ebitda), "ebitda": round(ebitda), "ebitda_margin": b["ebitda"],
        "ebitda_range": [round(revenue * b["ebitda"] * 0.5), round(revenue * b["ebitda"] * 1.5)],
    }


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}k"
    return f"${v:.0f}"
