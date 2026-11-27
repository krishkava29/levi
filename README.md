# Levi: momentum-first deal sourcing for private equity

Levi finds private, founder-owned businesses by their **characteristics** (customer love, social heat,
Google and ChatGPT visibility, operating velocity, deal-ability), so a fund can reach them *before* they
show up on an EBITDA screen or in a banker's process.

- **Runs on your Codex subscription.** Levi drives the `codex` CLI you're already logged into. There is no separate API bill. OpenAI and Anthropic API keys also work.
- **Token-frugal by design.** Zero-token collectors run first. LLM calls are structured JSON only, cached, and capped per run.
- **Honest financials.** Revenue is triangulated from several methods, each with a range, a confidence and sources. Benchmark P&Ls are labelled as estimates.

## Quick start

```bash
npm i -g @openai/codex && codex login        # use your ChatGPT/Codex plan
pip install -e .                             # stdlib only, no dependencies
levi doctor                                  # checks backend and optional keys
levi import data/seeds/*.json                # 46 researched targets to start from
levi top                                     # leaderboard by experimental Levi Alpha
levi show "Branch Basics" --md               # full dossier
levi evidence "Branch Basics"                # claims, citations, and evidence gaps
levi run multi-site-services                 # discover -> AI probe -> collect -> score -> enrich -> memo
levi run "independent veterinary clinics in Texas" --max-llm-calls 25
```

Or just open `codex` in this repo and ask, for example "find me founder-owned HVAC companies in Florida
that are blowing up on Google reviews". `AGENTS.md` teaches Codex to use Levi as its toolset.

Optional keys that unlock more signals: `GOOGLE_PLACES_API_KEY` (ratings and review velocity across
locations) and `SERPAPI_API_KEY` (Google rank, map pack, Google Trends).

## How it works

```
discover (LLM + web, 6 angles)  ──┐
AI share-of-voice probe          ─┤   ~10 LLM calls per thesis
free collectors (0 tokens):       │   website, socials, ATS job boards, Wayback,
  every company                   │   Reddit, Hacker News, Wikipedia, Places, SerpAPI
score (0 tokens)                 ─┤
deep enrich top-K (LLM + web)    ─┤   1 call per shortlisted company
revenue triangulation (0 tokens) ─┤
challenge + memo top-N (LLM)     ─┘   2 calls per finalist
```

**Levi Score** blends 8 pillars (love, heat, discover, velocity, moat, dealability, financial, risk).
Each thesis sets its own weights.
**Levi Alpha (experimental)** = Levi Score × institutional-unawareness factor × founder factor.
The dossier shows each factor.
**Trajectory fit**: a $4M company doubling every year scores as if it is in a $10–100M band, because in
24 months it will be.

`levi evidence <company> --json` includes accepted claims, the evidence pinned to the score,
and rejected inputs. Use `--as-of YYYY-MM-DD` to inspect evidence available by the end of a UTC date.
Historical queries use retrieval time and source publication dates when known.
`levi memo <company>` stores a challenge before drafting and requires a response to each objection.
Regenerate legacy memos with this command to display them alongside a challenge.

## Why it's cheap to run

| Lever | Effect |
|---|---|
| Funnel: free collectors for all, LLM for the shortlist | ~90% of companies never cost a token |
| JSON Schema outputs (`codex exec --output-schema`) | No prose, short outputs |
| Compacted evidence (HTML stripped, capped) | 10–30x fewer input tokens than raw pages |
| Low reasoning effort for extraction, medium only for discovery and memos | Fewer reasoning tokens |
| Content-addressed cache (14-day TTL) | The same question is never paid for twice |
| Hard budget (`LEVI_MAX_LLM_CALLS`, `--max-llm-calls`) | Fails closed |
| Codex runs in an empty temp directory | It doesn't ingest repo files on every call |
| `levi usage` | Shows calls, cache hits and characters in/out by purpose |

## Docs

- [`docs/PLAYBOOK.md`](docs/PLAYBOOK.md): the characteristics Levi hunts for, and where real financials can be found
- [`docs/SIGNALS_TABLE.md`](docs/SIGNALS_TABLE.md): every signal, its weight and how it's measured
- [`docs/ROADMAP.md`](docs/ROADMAP.md): the feature roadmap to becoming the #1 sourcing agent
- [`data/target_list.csv`](data/target_list.csv): the scored target list, including experimental Levi Alpha

## Tests

```bash
python -m unittest discover -s tests
```
