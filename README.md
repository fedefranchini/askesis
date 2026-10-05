# Askesis

A local-first, evidence-based **longitudinal AI athlete coach** for a single athlete.

Most fitness apps log workouts. Askesis is designed to answer a different question:
**"When we changed something, why did we change it — and did it work?"**

It tracks training (hypertrophy, strength, running), nutrition, body composition, recovery and sleep
over time; derives metrics deterministically; proposes interventions with explicit reasoning, data and
published evidence; and evaluates each intervention against what was expected.

> **Status:** early stage. The reference architecture and the MVP roadmap are written; the MVP
> (data core, CLI, core metrics, weekly review, intervention registry) is being built.
> See [`docs/roadmap.md`](docs/roadmap.md).

## The name

*Askesis* (Greek ἄσκησις) means exercise, practice, training. In ancient Greece it referred to the
regular training of athletes; later philosophers, the Stoics among them, used it for any disciplined,
repeated practice. Askesis is about that kind of training: small, consistent sessions over a long time,
reviewed honestly.

## Design principles

- **One canonical source of truth.** Raw data is append-only and never edited; corrections are new
  records that supersede old ones. Every record keeps its source and both *when it happened* and *when
  the system learned it* (bitemporal).
- **Code computes, the LLM reasons.** All numbers (weight trends, adaptive TDEE, e1RM, training volume,
  running load) come from deterministic, tested code. The language model never invents metrics; every
  number in an answer must trace back to data.
- **Evidence from verifiable publications.** Methodological choices come from published, citable
  sources: position stands and guidelines first, then systematic reviews and meta-analyses, then RCTs.
  Every claim in the knowledge base carries a DOI or PMID checked against Crossref or PubMed. Where
  evidence is weak or missing, the choice is labelled *expert opinion* or *personal preference*.
- **Explicit uncertainty.** Every output is labelled as fact, measurement, estimate, inference or
  hypothesis, with a confidence level and a data-quality grade.
- **Decisions are proposals.** The plan changes only with the athlete's explicit approval. Every
  decision records the data it used, the evidence, the reasoning and the expected outcome.
- **Pre-registered interventions.** Hypothesis, baseline, expected outcome and evaluation date are
  frozen before a change starts, so it can be evaluated honestly months later (N-of-1, with its limits
  stated).
- **Safety first.** Rules for injury, illness, excessive fatigue, too-rapid weight loss, low energy
  availability, disordered-eating signs and cardiovascular symptoms take precedence over coaching.

## Architecture (overview)

```
Sources ─► Ingestion ─► RAW (append-only) ─► Data quality ─► Analytics ─► Athlete state
                                                                              │
              Evidence KB ─► Safety gate ◄─► Decision engine ─► Interventions ─► Plan
```

Full specification: [`docs/architecture.md`](docs/architecture.md) (in Italian).

Planned stack: Python, SQLite, Pydantic, Polars, a Typer CLI (`askesis`, short alias `ak`);
Apple Health export / HealthKit as future data sources.

## Privacy and copyright model

This repository contains **only the system**: code, specifications, generic rules and general
evidence metadata. All personal and health data stays local and is excluded from version control.

| Location | Contents | In git? |
|---|---|---|
| `data/` | database, staging, reports, backups, photos, health exports | No |
| `private/` | athlete profile, personal plan, links between evidence and personal data, papers for reading | No |
| local assistant instructions | personal instructions for your AI assistant (any `*.local.md`, tool settings folders) | No |

The evidence knowledge base stores only bibliographic metadata, summaries in our own words and very
short quotations. It never stores full texts or PDFs of articles.

Versioned pre-commit and pre-push hooks (`.githooks/`) block commits and pushes of private paths,
databases, health exports, PDFs, secrets, local user paths, personal e-mail addresses and terms from a
private, untracked denylist. Enable them once per clone:

```bash
git config core.hooksPath .githooks
```

## Quick start

```bash
uv sync                      # Python 3.12 environment and dependencies
uv run pytest                # test suite (synthetic data only)
bin/ak init                  # create the local database (data/askesis.db, never committed)
bin/ak day "p 68.4 · cibo 1850 115 · pesi: squat 80x5 r2, 80x5 r1"   # fast daily line
bin/ak show week
bin/ak metrics rebuild        # recompute all derived metrics and check reproducibility
bin/ak review                # weekly review (Markdown) of the week just closed
bin/ak backup
```

Local dashboard (optional, this Mac only):

```bash
uv sync --extra dashboard
bin/ak web set-password      # stored only as an scrypt hash under data/
bin/ak web serve             # http://127.0.0.1:8765 — same core and checks as the CLI
```

`bin/ak` is a launcher for the `askesis` CLI. Personal settings (time zone, aliases for personal context
variables) go in `private/askesis.toml`, which is never committed.

## Repository layout

```
AGENTS.md            rules for an AI assistant (data, evidence, decisions, safety, privacy)
src/askesis/         data core: model, append-only store, data quality, ingestion, parsers, CLI
migrations/          forward-only SQL migrations
schemas/v1/          JSON Schema of the ingestion contract
config/              public default configuration
knowledge/           evidence KB, metric parameters (each with its basis), reference data
tests/               test suite (synthetic data only)
bin/ak               CLI launcher
docs/                architecture spec, roadmap, detailed assistant rules (docs/agent/)
.githooks/           privacy and attribution guards
```

## Using it with an AI assistant

Askesis works without AI: the CLI, metrics, reviews, safety rules and intervention registry are plain code.
An AI assistant makes it better — it reads your data through the CLI, writes the reports and proposes changes,
while the code computes every number and enforces approvals, privacy and safety.

The assistant rules are model-independent and live in [`AGENTS.md`](AGENTS.md) (details in `docs/agent/`):

- if your coding assistant reads `AGENTS.md`, nothing else is needed;
- otherwise create the instruction file your assistant expects (it stays local, never committed) and make it
  include or point to `AGENTS.md`, adding only tool-specific notes;
- keep personal preferences in a private local instructions file, and deny direct writes to `data/` in your
  assistant's permission settings (data goes only through `bin/ak`);
- disable automatic AI attribution in commits; the hooks reject any `Co-Authored-By` trailer anyway.

## Disclaimer

Askesis is a personal project. It is **not a medical device** and not a substitute for professional
medical, dietetic or physiotherapy advice. It describes patterns in data and suggests when to seek
professional evaluation; it does not diagnose. Consult a qualified professional before starting a new
training or nutrition programme, especially with existing health conditions.

## License

[MIT](LICENSE) © 2026 Federico Franchini
