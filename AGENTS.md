# AGENTS.md

Self-hosted daemon that enriches an existing **Sure** instance (personal finance app, ex-Maybe) with
purchase-level data and syncs meal-voucher wallets. The providers are Italian/European: currency EUR, calendar
dates in `Europe/Rome`, timestamps persisted in UTC. Read `README.md` for usage and the provider status table.

## Setup and checks

- Install: `uv sync` (Python 3.13, deps pinned in `uv.lock`). Postgres driver: `uv sync --extra postgres`.
- Tests: `uv run pytest -q`. They must pass before every commit. No network, no real credentials.
- Run against the real Sure: `uv run --env-file .env spe ...`. `.env` is gitignored; never print its values.
- New migration: change `src/spe/db.py`, then
  `DATABASE_URL=sqlite:////tmp/gen.db uv run alembic revision --autogenerate --rev-id 000N -m "..."`, delete
  `/tmp/gen.db`, review the file. Never edit a migration that has been committed; add a new one.

## Layout

- `src/spe/sure.py`: Sure REST + MCP client, acting as one user. `auth.py`: sign-in through Sure's OAuth, per-user
  tokens. `db.py`: models (`users`, `connections`, `sure_ops` queue/ledger, `evidence`, all scoped by user) +
  `migrate()`. `writer.py`: the only code that mutates Sure. `scheduler.py`, `web.py`: daemon. `__main__.py`: CLI.
- Domain: `vouchers.py` (voucher movements → creates), `purchases.py` (receipt/order → payment legs → edits of
  existing transactions), `payin3.py` (PayPal plans from the PayPal feed in Sure).
- `providers/*.py`: one module per source, registered on import. Sessions come from `loginbrowser.py` (CDP into
  `docker/login-browser`) and are stored with `vault.py`. Each module's docstring records the observed endpoints.
- `tests/fixtures/`: synthetic data only. Never real names, amounts tied to a person, card numbers, tokens.

## Code style

- Smallest change that works. Reuse what exists before adding helpers, layers or dependencies.
  No abstractions with one implementation, no config for values that never change, no scaffolding "for later".
- Money: `Decimal` or integer minor units, never `float`, never parse Sure's formatted `amount` string
  (use `amount_decimal`). Send amounts to Sure as whole-cent decimal strings.
- UI strings are English in the code, wrapped in `_("…")` (templates) or `tr("…")` (Python) with `%(name)s`
  placeholders; their Italian goes in `spe/i18n.py` (`tests/test_i18n.py` fails on a missing one).
- Typed Python, stdlib first. Comments explain *why*; mark deliberate shortcuts with `ponytail:` + the ceiling.

## Financial invariants (non-negotiable)

1. Re-running any import creates zero duplicates, across restarts and retries. Every Sure create carries
   `source` + stable `external_id`, and goes through the `writer` ledger.
2. Bank-imported Sure transactions are the source of truth for amounts and dates. Enrichment edits their
   metadata (category, merchant, notes, tags, splits); it never creates a second expense for the same payment.
3. One checkout paid with several rails (e.g. €24 Edenred + €24 card) is one logical purchase with several
   payment legs. Never create an extra expense for the order/receipt total.
4. Every Sure mutation is recorded locally with provenance, request, before/after and status.
5. Missing/failed upstream data (login expired, 429, schema change, missing balance) never zeroes or
   overwrites a value in Sure. Report `needs_auth` / `blocked` / `error` instead.
6. Economically distinct assets are counted once. Moving vouchers between card and Cloud is not income.
7. Never overwrite user edits in Sure (`user_modified`, manual categories, existing splits).
8. Retroactive edits, splits and installment bookkeeping need dry-run preview and explicit approval.
9. Ambiguous matches (merchant-only, same-day same-amount only) go to the review queue, never auto-written.
10. Matching is deterministic. No LLM decides financial writes.

## Sure API rules

- Use only the REST API (`/api/v1`) or MCP tools, always with the token of the user the work is for, whose schema was
  read from `tools/list` on the live instance. Never write to Sure's database.
- Verify contracts against the deployed Sure build, not memory. Upstream: https://github.com/we-promise/sure
  (`app/controllers/api/v1/*`, `app/views/api/v1/**`, `docs/api/openapi.yaml`).
- Accounts cannot be created via API: users create them once in Sure and pick them in Connections.
- Experiments that write (splits, loans, transfers) run against a test family / local Sure, never someone's real data.

## Providers and external services

- Never invent endpoints or copy another country's Edenred API (PT/ES/FR). Implement only shapes observed
  on the real Italian service with the account holder's consent.
- Never bypass CAPTCHA, MFA, bot protection or rate limits. OTPs are typed by the account holder, never asked in chat.
- Routine sync at most 1–2×/day per provider, backoff with jitter on 429/5xx.
- A provider counts as working only when it fetched real data, reconciled to the value shown by the source,
  and resumed after a restart. Mark everything else fixture-only/blocked in the README table.

## Security

- No credentials, cookies, tokens, OTPs or full HARs in logs, fixtures, commits, screenshots or chat.
- Session material is encrypted at rest with a deployment secret. The UI requires signing in through Sure; every
  Sure call uses the token of the user it is for.

## Commits

- Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), optional scope (`feat(edenred): ...`).
  Short body only when the why is not obvious. One conceptual change per commit.
- Work on the current branch; do not create branches or MRs unless asked.
