"""The Levi signal taxonomy.

Levi does not screen on EBITDA. It screens on the *characteristics* that show up
online 12-36 months before a business crosses a fund's financial threshold.
Every signal here is:

  * observable from the outside (no NDA needed),
  * normalised to 0..1 so pillars can be blended per thesis,
  * tagged with a cost tier so the pipeline can run cheap signals first and spend
    LLM tokens only on companies that survive the cheap screen.

Cost tiers:
  free  - public HTTP endpoint, no key, zero LLM tokens
  key   - needs a third-party API key the user brings (Google Places, SerpAPI...)
  llm   - needs an LLM call (Codex subscription or BYO key)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

PILLARS = {
    "love": "Customer love - do customers rave about it?",
    "heat": "Attention & brand heat - are people talking about it right now?",
    "discover": "Discoverability - does Google and AI recommend it?",
    "velocity": "Operating velocity - is the business visibly scaling?",
    "moat": "Product & moat - is there something real and defensible here?",
    "dealability": "Deal-ability - can a fund actually get in, and get in first?",
    "financial": "Financial proxies - does the size fit the fund?",
    "risk": "Risk flags - things that subtract (scored as 1 = clean)",
}


@dataclass(frozen=True)
class Signal:
    id: str
    pillar: str
    name: str
    why: str
    how: str
    tier: str  # free | key | llm
    norm: str  # log | linear | bool | rating | ratio
    lo: float = 0.0  # value that maps to 0
    hi: float = 1.0  # value that maps to 1
    weight: float = 1.0
    higher_is_better: bool = True
    tags: tuple = field(default_factory=tuple)

    def normalise(self, value) -> float | None:
        if value is None:
            return None
        try:
            if self.norm == "bool":
                x = 1.0 if bool(value) else 0.0
            else:
                v = float(value)
                if self.norm == "log":
                    lo, hi = math.log10(max(self.lo, 1)), math.log10(max(self.hi, 2))
                    x = (math.log10(max(v, 1)) - lo) / (hi - lo)
                else:  # linear | rating | ratio
                    x = (v - self.lo) / (self.hi - self.lo)
        except (TypeError, ValueError):
            return None
        x = min(1.0, max(0.0, x))
        return x if self.higher_is_better else 1.0 - x


S = Signal
SIGNALS: list[Signal] = [
    # ---------------------------------------------------------------- love
    S("review_rating", "love", "Average review rating",
      "Sustained 4.7+ across hundreds of reviews is very hard to fake and predicts retention.",
      "Google Places / Amazon / Trustpilot / G2; Bayesian-adjusted toward 4.2 when counts are small.",
      "key", "rating", 4.0, 4.9, 2.0),
    S("review_count", "love", "Total review volume",
      "Review count is a rough proxy for cumulative customers (typically 1-5% of customers review).",
      "Sum of counts across platforms.", "key", "log", 50, 20000, 1.5),
    S("review_velocity", "love", "Reviews per month (last 90 days)",
      "Acceleration in reviews is one of the best leading indicators of revenue acceleration.",
      "Diff of review counts between Levi snapshots, or dated reviews.", "key", "log", 5, 1000, 2.0),
    S("review_sentiment_trend", "love", "Sentiment trend",
      "Rising sentiment while volume grows = the business is scaling without breaking.",
      "LLM classification of a sample of recent vs older reviews (cheap model).", "llm", "linear", -0.3, 0.3, 1.0),
    S("repeat_language", "love", "Repeat / loyalty language in reviews",
      "'my third order', 'we come every week' = recurring revenue in disguise.",
      "Keyword share in review text.", "llm", "ratio", 0.0, 0.25, 1.0),
    # ---------------------------------------------------------------- heat
    S("social_followers", "heat", "Social following (TikTok+IG+YT)",
      "Owned audience lowers CAC and is transferable equity value.",
      "Counts scraped from linked profiles on the company website.", "free", "log", 1000, 2_000_000, 1.5),
    S("social_growth_90d", "heat", "Follower growth (90d)",
      "Growth rate matters more than size: we want the curve, not the plateau.",
      "Diff of follower counts between snapshots.", "free", "linear", 0.0, 0.5, 2.0),
    S("engagement_rate", "heat", "Engagement rate",
      "High engagement on a small base beats a large dead audience.",
      "Avg (likes+comments)/followers on recent posts.", "key", "ratio", 0.005, 0.08, 1.0),
    S("search_interest_trend", "heat", "Branded search trend",
      "Google Trends slope on the brand name: people typing your name is pure demand.",
      "Google Trends (SerpAPI key) - 12m slope.", "key", "linear", -0.2, 1.0, 2.0),
    S("press_velocity", "heat", "Press mentions (last 6m)",
      "Earned media velocity; also feeds AI model recommendations.",
      "News search count.", "llm", "log", 1, 100, 1.0),
    S("community_mentions", "heat", "Reddit / HN / forum mentions",
      "Unprompted peer recommendations are the purest signal of product love.",
      "Reddit search + Hacker News Algolia API (free).", "free", "log", 3, 500, 1.5),
    # ---------------------------------------------------------------- discover
    S("ai_share_of_voice", "discover", "AI assistant share of voice",
      "Buyers increasingly ask ChatGPT/Claude/Perplexity 'best X near me'. Being the answer is a new moat.",
      "Levi asks N category prompts (no web search) and counts how often the company is recommended.",
      "llm", "ratio", 0.0, 0.6, 2.5),
    S("ai_rank", "discover", "Average rank when AI recommends it",
      "First-named recommendation captures most of the click.",
      "Mean position in AI answers (1 = first).", "llm", "linear", 1, 8, 1.0, higher_is_better=False),
    S("google_rank", "discover", "Google organic rank for category terms",
      "Owning page one for '<category> <city>' is compounding free demand.",
      "SerpAPI / Google CSE position for thesis keywords.", "key", "linear", 1, 30, 1.5, higher_is_better=False),
    S("local_pack", "discover", "Google local 3-pack presence",
      "For local businesses the map pack is the storefront.", "SerpAPI local results.", "key", "bool"),
    S("wikipedia_page", "discover", "Has a Wikipedia page",
      "Notability marker and a top source for LLM training data.", "Wikipedia API.", "free", "bool", weight=0.5),
    # ---------------------------------------------------------------- velocity
    S("revenue_growth", "velocity", "Revenue / system-sales growth (YoY)",
      "The single best predictor of where the business will be when you'd want to own it.",
      "On-record growth, Inc. 5000 3-yr CAGR, Future 50 / Technomic sales growth.", "llm", "linear", 0.0, 1.0, 2.5),
    S("headcount_growth", "velocity", "Headcount growth (12m)",
      "Companies hire ahead of revenue; 30%+ headcount growth almost always means real growth.",
      "Snapshots of careers page / LinkedIn employee count.", "free", "linear", 0.0, 0.6, 2.0),
    S("open_roles", "velocity", "Open roles",
      "Volume and *type* of roles (ops, GM, CFO/controller) reveal stage. A first CFO hire is a pre-transaction tell.",
      "Careers page + ATS (Greenhouse/Lever/Ashby/Workable) public JSON.", "free", "log", 1, 80, 1.5),
    S("finance_hire", "velocity", "Hiring CFO / controller / FP&A",
      "Classic pre-transaction signal: getting the books investor-ready.",
      "Role titles on ATS.", "free", "bool", weight=1.5, tags=("pre-transaction",)),
    S("new_locations_12m", "velocity", "New locations / doors (12m)",
      "Unit growth for multi-site; new retailer doors for consumer brands.",
      "Locations page diff, press, permits.", "llm", "log", 1, 50, 2.0),
    S("site_expansion", "velocity", "Website expansion",
      "More pages / products / locations on the site over time (Wayback).",
      "Wayback CDX snapshot counts year-over-year.", "free", "linear", 0.0, 2.0, 0.75),
    S("awards", "velocity", "Growth awards (Inc 5000 etc.)",
      "Inc. 5000 discloses 3-yr growth and a revenue band - free verified data.",
      "Inc 5000 lookups / press.", "llm", "bool", weight=1.0),
    S("trademark_filings", "velocity", "Recent trademark filings",
      "New marks = new product lines or geographies coming.", "USPTO search.", "free", "log", 1, 20, 0.75),
    S("ad_activity", "velocity", "Active paid ads",
      "Active Meta / Google ads show a working acquisition engine (and budget to run it).",
      "Meta Ad Library count.", "key", "log", 1, 200, 0.75),
    # ---------------------------------------------------------------- moat
    S("product_talkability", "moat", "Product talkability",
      "Is there a one-sentence reason people tell friends? Measured from reviews + social comments.",
      "LLM judgment over collected text, 0-1.", "llm", "ratio", 0.0, 1.0, 1.5),
    S("recurring_model", "moat", "Recurring / repeat revenue model",
      "Subscriptions, memberships, consumables, service contracts.", "Website + LLM.", "llm", "bool", weight=1.5),
    S("category_tailwind", "moat", "Category tailwind",
      "Riding a secular trend (GLP-1, pets, aging, AI adoption...) multiplies everything else.",
      "LLM judgment with thesis context.", "llm", "ratio", 0.0, 1.0, 1.0),
    S("pricing_power", "moat", "Pricing power",
      "Premium price vs peers while keeping ratings high.", "LLM over pricing pages + reviews.", "llm", "ratio", 0.0, 1.0, 1.0),
    # ---------------------------------------------------------------- dealability
    S("founder_owned", "dealability", "Founder / family owned",
      "No institutional owner = proprietary deal potential.", "Funding / ownership search.", "llm", "bool", weight=2.5),
    S("capital_raised", "dealability", "Outside capital raised",
      "Less raised = cleaner cap table and more likely to want a first institutional partner.",
      "Press / filings.", "llm", "log", 1_000_000, 100_000_000, 1.5, higher_is_better=False),
    S("institutional_awareness", "dealability", "Institutional awareness",
      "How 'shopped' the company already is: PE mentions, banker processes, M&A press. Low = first-mover edge.",
      "LLM over news search.", "llm", "ratio", 0.0, 1.0, 2.0, higher_is_better=False),
    S("succession_signal", "dealability", "Succession / liquidity signal",
      "Long founder tenure, founder age, 'next chapter' language, second-gen hesitancy.",
      "LLM over About page + press.", "llm", "ratio", 0.0, 1.0, 1.0),
    S("fragmented_market", "dealability", "Fragmented market (buy-and-build)",
      "Platform potential: many sub-scale peers to bolt on.", "LLM / thesis config.", "llm", "ratio", 0.0, 1.0, 1.0),
    # ---------------------------------------------------------------- financial
    S("revenue_fit", "financial", "Estimated revenue in fund's band",
      "Triangulated estimate (headcount, units x AUV, reviews, disclosed bands) vs thesis band.",
      "levi.financials triangulation.", "llm", "ratio", 0.0, 1.0, 2.0),
    S("revenue_confidence", "financial", "Confidence in revenue estimate",
      "Reported > Inc band > multi-method triangulation > single proxy.", "levi.financials.", "free", "ratio", 0.0, 1.0, 0.75),
    S("margin_profile", "financial", "Margin profile of the model",
      "Industry benchmark margin for this business model.", "Benchmarks table.", "free", "ratio", 0.0, 1.0, 1.0),
    # ---------------------------------------------------------------- risk (1 = clean)
    S("fad_risk", "risk", "Fad risk",
      "Spike without sustained baseline (viral once, then flat).", "Trend shape + LLM.", "llm", "ratio", 0.0, 1.0, 1.5,
      higher_is_better=False),
    S("review_integrity", "risk", "Review integrity issues",
      "Bursts of 5-star reviews, incentivised reviews, review-gating.", "Velocity anomaly + LLM.", "llm", "ratio",
      0.0, 1.0, 1.0, higher_is_better=False),
    S("legal_regulatory", "risk", "Legal / regulatory issues",
      "Lawsuits, recalls, health violations, FDA/FTC letters.", "News + court search via LLM.", "llm", "ratio",
      0.0, 1.0, 1.5, higher_is_better=False),
    S("key_person", "risk", "Key-person dependency",
      "Brand is the founder's personal face (influencer-led) - transferability risk.", "LLM.", "llm", "ratio",
      0.0, 1.0, 1.0, higher_is_better=False),
]

BY_ID = {s.id: s for s in SIGNALS}


def by_pillar() -> dict[str, list[Signal]]:
    out: dict[str, list[Signal]] = {p: [] for p in PILLARS}
    for s in SIGNALS:
        out[s.pillar].append(s)
    return out
