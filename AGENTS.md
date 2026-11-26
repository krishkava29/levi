# Levi — instructions for Codex

You are running inside the Levi repo. Levi is a deal-sourcing agent for private equity. It finds private,
founder-owned businesses by their **characteristics** (customer love, attention, discoverability,
operating velocity, deal-ability) before they show up on anyone's EBITDA screen.

When the user asks you to find, screen, compare or monitor companies, **use the `levi` CLI as your
tools**. Don't re-implement its logic in chat. It is cheaper (cached, budgeted, zero-token collectors)
and it leaves an audit trail in `~/.levi/levi.db`.

## Workflow

1. Turn the request into a thesis. Pick a built-in one (`levi theses`) or pass free text:
   `levi run "independent veterinary clinics in Texas" --enrich-top 8 --memo-top 3`
2. Read the shortlist with `levi top --thesis <name>`. Open the best ones with `levi show "<Company>" --md`.
3. Go deeper on one company only when asked: `levi enrich "<Company>"`, then `levi memo "<Company>"`.
4. Track the pipeline with `levi status "<Company>" watching|contacted|passed|pipeline`.
5. For monitoring, `levi watch --thesis <name>` re-collects zero-token signals and lists the movers.

## Rules

- Never invent revenue, EBITDA or ownership. Private-company financials are **estimates**. Always show
  Levi's range, its confidence and the methods behind it, and label the P&L as industry-benchmarked.
- Prefer companies with `founder_owned=true` and low `institutional_awareness`. That is the "Levi Alpha".
- Respect the user's token budget. Use `--max-llm-calls` to cap a run, and don't enrich more companies
  than the user needs.
- Cite sources (URLs) for every factual claim you repeat to the user.
