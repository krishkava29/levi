# Levi signal reference (generated from `levi/signals.py`)

Tier: `free` = public data, zero tokens · `key` = bring-your-own API key · `llm` = uses your Codex/LLM budget.

## Customer love - do customers rave about it?

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| Average review rating | key | 2.0 | Sustained 4.7+ across hundreds of reviews is very hard to fake and predicts retention. | Google Places / Amazon / Trustpilot / G2; Bayesian-adjusted toward 4.2 when counts are small. |
| Total review volume | key | 1.5 | Review count is a rough proxy for cumulative customers (typically 1-5% of customers review). | Sum of counts across platforms. |
| Reviews per month (last 90 days) | key | 2.0 | Acceleration in reviews is one of the best leading indicators of revenue acceleration. | Diff of review counts between Levi snapshots, or dated reviews. |
| Sentiment trend | llm | 1.0 | Rising sentiment while volume grows = the business is scaling without breaking. | LLM classification of a sample of recent vs older reviews (cheap model). |
| Repeat / loyalty language in reviews | llm | 1.0 | 'my third order', 'we come every week' = recurring revenue in disguise. | Keyword share in review text. |

## Attention & brand heat - are people talking about it right now?

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| Social following (TikTok+IG+YT) | free | 1.5 | Owned audience lowers CAC and is transferable equity value. | Counts scraped from linked profiles on the company website. |
| Follower growth (90d) | free | 2.0 | Growth rate matters more than size: we want the curve, not the plateau. | Diff of follower counts between snapshots. |
| Engagement rate | key | 1.0 | High engagement on a small base beats a large dead audience. | Avg (likes+comments)/followers on recent posts. |
| Branded search trend | key | 2.0 | Google Trends slope on the brand name: people typing your name is pure demand. | Google Trends (SerpAPI key) - 12m slope. |
| Press mentions (last 6m) | llm | 1.0 | Earned media velocity; also feeds AI model recommendations. | News search count. |
| Reddit / HN / forum mentions | free | 1.5 | Unprompted peer recommendations are the purest signal of product love. | Reddit search + Hacker News Algolia API (free). |

## Discoverability - does Google and AI recommend it?

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| AI assistant share of voice | llm | 2.5 | Buyers increasingly ask ChatGPT/Claude/Perplexity 'best X near me'. Being the answer is a new moat. | Levi asks N category prompts (no web search) and counts how often the company is recommended. |
| Average rank when AI recommends it | llm | 1.0 | First-named recommendation captures most of the click. | Mean position in AI answers (1 = first). |
| Google organic rank for category terms | key | 1.5 | Owning page one for '<category> <city>' is compounding free demand. | SerpAPI / Google CSE position for thesis keywords. |
| Google local 3-pack presence | key | 1.0 | For local businesses the map pack is the storefront. | SerpAPI local results. |
| Has a Wikipedia page | free | 0.5 | Notability marker and a top source for LLM training data. | Wikipedia API. |

## Operating velocity - is the business visibly scaling?

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| Revenue / system-sales growth (YoY) | llm | 2.5 | The single best predictor of where the business will be when you'd want to own it. | On-record growth, Inc. 5000 3-yr CAGR, Future 50 / Technomic sales growth. |
| Headcount growth (12m) | free | 2.0 | Companies hire ahead of revenue; 30%+ headcount growth almost always means real growth. | Snapshots of careers page / LinkedIn employee count. |
| Open roles | free | 1.5 | Volume and *type* of roles (ops, GM, CFO/controller) reveal stage. A first CFO hire is a pre-transaction tell. | Careers page + ATS (Greenhouse/Lever/Ashby/Workable) public JSON. |
| Hiring CFO / controller / FP&A | free | 1.5 | Classic pre-transaction signal: getting the books investor-ready. | Role titles on ATS. |
| New locations / doors (12m) | llm | 2.0 | Unit growth for multi-site; new retailer doors for consumer brands. | Locations page diff, press, permits. |
| Website expansion | free | 0.75 | More pages / products / locations on the site over time (Wayback). | Wayback CDX snapshot counts year-over-year. |
| Growth awards (Inc 5000 etc.) | llm | 1.0 | Inc. 5000 discloses 3-yr growth and a revenue band - free verified data. | Inc 5000 lookups / press. |
| Recent trademark filings | free | 0.75 | New marks = new product lines or geographies coming. | USPTO search. |
| Active paid ads | key | 0.75 | Active Meta / Google ads show a working acquisition engine (and budget to run it). | Meta Ad Library count. |

## Product & moat - is there something real and defensible here?

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| Product talkability | llm | 1.5 | Is there a one-sentence reason people tell friends? Measured from reviews + social comments. | LLM judgment over collected text, 0-1. |
| Recurring / repeat revenue model | llm | 1.5 | Subscriptions, memberships, consumables, service contracts. | Website + LLM. |
| Category tailwind | llm | 1.0 | Riding a secular trend (GLP-1, pets, aging, AI adoption...) multiplies everything else. | LLM judgment with thesis context. |
| Pricing power | llm | 1.0 | Premium price vs peers while keeping ratings high. | LLM over pricing pages + reviews. |

## Deal-ability - can a fund actually get in, and get in first?

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| Founder / family owned | llm | 2.5 | No institutional owner = proprietary deal potential. | Funding / ownership search. |
| Outside capital raised | llm | 1.5 | Less raised = cleaner cap table and more likely to want a first institutional partner. | Press / filings. |
| Institutional awareness | llm | 2.0 | How 'shopped' the company already is: PE mentions, banker processes, M&A press. Low = first-mover edge. | LLM over news search. |
| Succession / liquidity signal | llm | 1.0 | Long founder tenure, founder age, 'next chapter' language, second-gen hesitancy. | LLM over About page + press. |
| Fragmented market (buy-and-build) | llm | 1.0 | Platform potential: many sub-scale peers to bolt on. | LLM / thesis config. |

## Financial proxies - does the size fit the fund?

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| Estimated revenue in fund's band | llm | 2.0 | Triangulated estimate (headcount, units x AUV, reviews, disclosed bands) vs thesis band. | levi.financials triangulation. |
| Confidence in revenue estimate | free | 0.75 | Reported > Inc band > multi-method triangulation > single proxy. | levi.financials. |
| Margin profile of the model | free | 1.0 | Industry benchmark margin for this business model. | Benchmarks table. |

## Risk flags - things that subtract (scored as 1 = clean)

| Signal | Tier | Weight | Why it matters | How Levi measures it |
|---|---|---|---|---|
| Fad risk | llm | 1.5 | Spike without sustained baseline (viral once, then flat). | Trend shape + LLM. |
| Review integrity issues | llm | 1.0 | Bursts of 5-star reviews, incentivised reviews, review-gating. | Velocity anomaly + LLM. |
| Legal / regulatory issues | llm | 1.5 | Lawsuits, recalls, health violations, FDA/FTC letters. | News + court search via LLM. |
| Key-person dependency | llm | 1.0 | Brand is the founder's personal face (influencer-led) - transferability risk. | LLM. |

