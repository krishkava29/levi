# Levi roadmap: what makes it the #1 sourcing agent

Think of this as an investment-committee memo on the product. The test for every feature: **does it get a
partner in front of a great founder-owned business earlier, with better information, than anyone else?**

## Shipped in v0.1 (this repo)

- **Characteristic-based scoring.** 40 signals in 8 pillars produce a Levi Score and a **Levi Alpha** (score discounted by how shopped the company already is).
- **Trajectory fit.** A company under the revenue floor still scores if its growth puts it in the band within 24 months.
- **AI share-of-voice probe.** Measures whether ChatGPT and other assistants recommend the company. Names the AI recommends that Levi isn't tracking yet become new leads.
- **Revenue triangulation** with an audit trail and an indicative, benchmark-labelled P&L.
- **Codex-native.** Runs on the user's Codex (ChatGPT) subscription via `codex exec`, or on a bring-your-own OpenAI or Anthropic key. An `AGENTS.md` lets users simply talk to Codex in this repo.
- **Token discipline.** A funnel (zero-token collectors first), structured-output-only calls, a cache, a per-run budget, and an empty working directory for each Codex call.
- **Time-series snapshots**, so a second run measures *velocity*, not just levels. `levi watch` reports the movers.
- **Pipeline states** (new, watching, contacted, passed, pipeline) and CSV export.

## Next 90 days: highest ROI

1. **FDD harvester.** Pull franchise disclosure documents from state registries and parse Item 19 (unit revenue), Item 20 (openings and closures) and Item 21 (**audited franchisor financial statements**). This gives *real* financials for every franchisor in the US, for free. No competitor does this well.
2. **Review-velocity engine.** Weekly Google Places and Yelp snapshots per location to compute review velocity, rating drift and integrity anomalies. This is the strongest single leading indicator for local and multi-site businesses.
3. **Hiring intelligence.** Pull Greenhouse, Lever, Ashby and Workable feeds and careers pages, and classify roles. Alert on a **first CFO or controller hire** (pre-transaction) or a jump in GM and ops hiring (scaling).
4. **List ingestion.** Annual Inc. 5000, Deloitte Fast 500, Future 50, Franchise 500, G2 grids and the PCT Top 100, matched to entities. Verified growth for thousands of private companies at once.
5. **Multi-model AI share of voice.** Track ChatGPT, Claude, Perplexity and Gemini by city and category over time. "Moved from 0/10 to 6/10 AI recommendations in 90 days" is a new kind of momentum signal.
6. **Weekly digest.** A Slack or email summary: new entrants above the threshold, biggest movers, pre-transaction tells, and companies that just raised or hired a banker (drop them).

## 6 months: compounding edge

7. **Backtesting and calibration.** Replay snapshots against companies that later sold to PE or strategics, then fit pillar weights per thesis. The pitch becomes: "Levi would have flagged Bachan's 14 months before the Marzetti deal."
8. **Lookalikes.** "Find me 20 more like Branch Basics": nearest neighbours on signal vectors and descriptions across the whole database.
9. **Category radar.** Detect emerging *categories* before companies, such as Yemeni coffee, sauna studios or tinned fish, from search trends, AI answers and opening velocity. Then sweep each category for the leaders.
10. **Proprietary-ness detector.** Flag banker processes (Axial teasers, "exploring strategic alternatives", Latka M&A flags, sell-side hires) so the team avoids auctions and prioritizes first-call opportunities.
11. **Add-on mapper.** For each portfolio company, map bolt-ons by geography, adjacency and signal fit, and rank them by integration ease.
12. **CRM sync** (DealCloud, Affinity, Salesforce, HubSpot). Dedupe against "already known" targets, push qualified ones, and log touches.

## 12 months: own the category

13. **Warm-path finder.** Join the founder graph (LinkedIn, CRM, portfolio CEOs, LPs) to find the shortest warm intro to each founder.
14. **Outreach copilot.** Founder-specific first lines built on real signals ("congrats on the Costco rollout"). Trigger-based timing (just hit 100 units, just made the Inc. 5000), sequencing and reply tracking.
15. **Alternative-data connectors** (bring your own license): Similarweb traffic, bill-of-lading import volumes (CPG), card-spend panels, app rankings, Placer.ai foot traffic, Meta Ad Library.
16. **Government data spine.** PPP payroll baselines, UCC liens (who is lending to them), trademark filings, building and liquor permits (new locations before they open), health inspections and court records.
17. **MCP server.** Expose Levi's tools to Codex, Claude and any agent, so analysts can ask questions in whatever assistant they use.
18. **Team mode.** A shared Postgres database, coverage assignment, notes, IC-ready one-pagers, and an audit trail for every number.
19. **Opt-in network effect.** Anonymized signal snapshots pooled across Levi users make velocity data denser over time. The time-series database *is* the moat.

## Guardrails (part of the product, not an afterthought)

- Respect robots.txt and rate limits, and use official APIs where they exist. No scraping behind logins.
- Never present estimates as reported numbers. Every figure carries a type, a confidence and a source URL.
- Collect as little personal data as possible: company-level signals only, and founder contact data only from public business sources.
