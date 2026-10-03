# Evidence KB

General, athlete-independent knowledge base. **No athlete data here**: the links between claims and a
specific athlete's plan live only in private files.

| File | Contents |
|---|---|
| `sources.yaml` | Bibliographic metadata, verification status (Crossref / PubMed), how much was read |
| `claims.yaml` | Atomic claims summarised in our own words, linked to sources, with certainty and limitations |

Rules (see `AGENTS.md`):
- every source has a DOI or PMID actually checked against Crossref or PubMed; nothing is cited from memory;
- source hierarchy: position stands / guidelines / consensus → systematic reviews & meta-analyses → RCTs;
- `basis: expert_opinion` and `status: unverified` are explicit labels, never hidden;
- metadata and own-words summaries only — no full texts or PDFs (copyright).

Current version (2026-10-03): six key sources read in full (open access via PubMed Central); the rest on
abstracts only. `read_level` and `status` say which. Grading is preliminary.

The `population_scope` field describes only the studied domain/population; applicability to a specific
athlete is kept in private files.
