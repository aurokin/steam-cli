# Agent Instructions

## Current Phase

- M1 installed-library is accepted; keep new work outside its boundary in design until the next milestone is explicitly selected.
- Treat capabilities beyond the credential-free installed-library slice as research and design until explicitly assigned.
- Do not turn proposed directions into accepted decisions without recording evidence in `docs/adr/README.md`.
- Treat `steam-library-agent-research-handoff.md` as unverified source material, not a specification.
- Preserve the M1 last-good rule: partial or failed scans must not replace a complete installed projection.
- Local filesystem paths are private: omit them from normal query output and avoid personal paths in fixtures or docs.

## External References

| Need | File |
| --- | --- |
| Project status and document index | `README.md` |
| Supported user questions and evidence distinctions | `docs/design/product-questions.md` |
| Provider support levels, terms, and limitations | `docs/design/evidence-matrix.md` |
| Proposed layers and language criteria | `docs/design/architecture.md` |
| Agent-facing command and JSON behavior | `docs/design/cli-contract.md` |
| Historical pricing providers and fallback semantics | `docs/design/pricing-strategy.md` |
| Steam actions, policy, and confirmation classes | `docs/design/actions.md` |
| Validation sequence | `docs/design/roadmap.md` |
| M1 implementation scope and Linear work graph | `docs/design/m1-execution.md` |
| Open decisions and ADR threshold | `docs/adr/README.md` |

## Design Conventions

- Preserve `unknown`, `false`, empty, inaccessible, and stale as distinct states.
- Keep hard eligibility separate from subjective ranking.
- Attach provider, retrieval time, context, and support level to normalized evidence.
- Keep provider adapters replaceable; do not depend on SteamDB scraping.
- Keep secrets out of argv, logs, reports, fixtures, and committed files.
- Use integer minor units with currency and country for prices.
- Model multiple accounts and machines even when a slice exercises only one.
- Keep JSON machine output deterministic; send diagnostics to stderr.
- Keep `return_url`, `open_for_human`, `agent_read`, and `automated_ingest` distinct.
- Do not automate Steam client/web interactions unless the capability has a reviewed policy basis.

## Documentation Changes

- Label unsettled documents and choices as working, proposed, open, or deferred.
- Prefer primary provider documentation and link it directly.
- Re-verify time-sensitive provider terms, limits, licenses, and endpoint support.
- Keep `AGENTS.md` concise and reference design documents instead of duplicating them.
