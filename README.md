# sure-enricher

A self-hosted companion for [Sure](https://github.com/we-promise/sure) (the personal finance app, ex-Maybe) that
fills in what your bank feed doesn't know:

- **What you bought.** Esselunga receipts, Amazon (all European stores) and AliExpress orders are matched to the
  card charges Sure already has, and each charge is split into its products, so Sure's own classifier and your
  rules can categorize them.
- **Meal vouchers.** Edenred Ticket Restaurant (Italy) movements are written to a voucher account in Sure, one
  transaction per checkout, with the shop as merchant; the balance is checked against Edenred's.
- **PayPal Pay in 3.** Plans are detected in the PayPal account Sure syncs; optionally the debt goes to a Loan
  account and the spending is spread over the instalments.

Nothing is written to Sure until you approve it. Users sign in with their Sure account; each one has their own
connections and only touches what they can see in Sure. The UI is in English and Italian (it follows the language
of your Sure family).

## Status

| Source | Status | How it reads |
| --- | --- | --- |
| Edenred Ticket Restaurant Italia | working (verified on a real account) | sign-in once, then a refresh token over plain HTTP |
| Esselunga | working (verified on a real account) | session cookies; receipt PDFs parsed, optionally archived |
| Amazon (Europe) | working on amazon.it, which lists every EU store's orders | session cookies; payments page + order details |
| AliExpress | working (verified on a real account) | session cookies; AliExpress's own signed order API |
| PayPal Pay in 3 | working; debt writeback validated on a test Sure | no PayPal login: reads the PayPal account in Sure |

amazon.com is not supported (charges in USD, card billed in EUR: exact matching can't work).

## Quick start

Images for `linux/amd64` and `linux/arm64`:

- `ghcr.io/depau/sure-enricher` — the daemon and its UI
- `ghcr.io/depau/sure-enricher-login-browser` — the Chromium users sign in to providers with, from the UI

1. Next to your Sure deployment, get [`compose.yaml`](compose.yaml) and [`.env.example`](.env.example):

   ```sh
   curl -O https://raw.githubusercontent.com/depau/sure-enricher/main/compose.yaml
   curl -o .env https://raw.githubusercontent.com/depau/sure-enricher/main/.env.example
   chmod 600 .env
   ```

2. Fill in `.env`:

   | Variable | Meaning |
   | --- | --- |
   | `SURE_URL` | how the enricher reaches Sure, e.g. `http://web:3000` on Sure's compose network |
   | `SURE_PUBLIC_URL` | where browsers reach Sure, e.g. `https://sure.example.org` (defaults to `SURE_URL`) |
   | `SPE_PUBLIC_URL` | where browsers reach the enricher, e.g. `https://sure.example.org/enrich`; its path is the prefix it serves under |
   | `SPE_SECRET_KEY` | encrypts tokens and provider sessions at rest; `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `DATABASE_URL` | defaults to SQLite in the data volume; Postgres: `postgresql+psycopg://user:pass@host/db` |
   | `SPE_RECEIPTS_DIR` | keep every Esselunga receipt PDF (set to `/receipts` in compose) |
   | `SPE_DISCONNECTED_WIPE_DAYS` | erase provider logins of users Sure no longer accepts after this many days (30) |

3. Route `SPE_PUBLIC_URL`'s path to port 8790 in the reverse proxy in front of Sure, websockets included. Caddy:

   ```
   sure.example.org {
       handle /enrich* {
           reverse_proxy enricher:8790
       }
       reverse_proxy web:3000
   }
   ```

4. `docker compose up -d`, open `https://sure.example.org/enrich` and approve the "Enrich" app in Sure.

Migrations run on startup. Back up the data volume (or `pg_dump`) before upgrading.

## Using it

- **Connections**: add a source. Those with a sign-in (Edenred, Esselunga, Amazon, AliExpress) open a live view of
  a browser running on the server: sign in as you would on the site, including the codes it sends you. The session
  is imported as soon as the site shows a signed-in page (or with "Import session"), stored encrypted, and the
  browser profile is erased. Pastes work (⌘V/Ctrl+V, or "Paste text…" for a password manager).
- **To approve**: every change, grouped per purchase: which transaction gets which notes, how it is split into
  products, and that the products add up to it. Approve, reject, or approve all.
- **On hold**: what isn't in Sure yet and why. When a payment matches several transactions, pick the right one.
- **History**: every write, with a link to the transaction in Sure.

Sure can't create accounts through its API, so the voucher account (Edenred) and the optional Pay in 3 loan are
created by hand in Sure; Connections shows the exact values to type, including the opening balance that makes the
voucher account land on Edenred's balance.

## Users and administration

There are no local accounts: the UI signs you in through Sure (OAuth, scope `read_write`; listed and revocable in
Sure under Settings → MCP). The same token is used for everything done for you, REST and MCP alike.

When Sure stops accepting a user's token (revoked, user removed; checked daily and on every use) the user becomes
`disconnected`: nothing runs for them, their queue is kept, and signing in again resumes it. After
`SPE_DISCONNECTED_WIPE_DAYS` their provider logins are erased; the rest stays until an admin removes it:

```sh
docker compose exec enricher spe users list
docker compose exec enricher spe users pause|resume ID
docker compose exec enricher spe users delete ID        # token, connections, logins, queue, history; Sure untouched
docker compose exec enricher spe connections list [--user ID]
docker compose exec enricher spe connections delete ID
```

Health: `GET /healthz` (liveness) and `GET /readyz` (database and scheduler), at the root and under the prefix.

## How it stays safe with your money

- **Approval first.** Every Sure mutation is queued (`planned → approved → sending → done`) with what it saw
  before; applying refuses if the transaction changed in the meantime. Only Edenred movements can be set to apply
  without asking, per connection.
- **No duplicates.** Created transactions carry `source` + a stable `external_id`, which Sure deduplicates on; an op
  that may have landed (crash, timeout) is looked up before it is resent.
- **The bank is the truth.** Amounts and dates come from the bank transactions; the enricher adds notes, merchant,
  tags and product splits, and never books a purchase total as an extra expense.
- **Deterministic matching.** Exact amount, a date window, the right account (voucher wallet or not), not already
  claimed, and the merchant's name on the charge. Anything ambiguous waits in On hold for you.
- **Hands off your edits.** Transactions you edited (`user_modified`) or split yourself are left alone; a merchant
  you chose is never replaced.
- **Contained sign-ins.** Each sign-in runs in its own browser profile, erased afterwards. While you use it, the tab
  can only show the provider's own sites, nothing on the server's network (Sure, the browser's control port, the
  LAN, cloud metadata) can be loaded, and popups are disabled. What you type is not logged or stored. Still keep
  the `login-browser` container off networks it doesn't need, and never publish its port.

## Sources in detail

### Edenred Ticket Restaurant (Italy)

- Sign-in once (password + SMS code); then everything is plain HTTP with a refresh token (rotated on every use)
  through `beneficiari.edenred.it` (`get-session-object`, `pb.getSaldoCarte`, `pb.getLogOperazioni`). When the
  refresh token is refused the connection asks to reconnect.
- Card, Cloud and smartphone are supports of one voucher pool, mapped to one Sure account; moves between supports
  (codes 02, 20–24) are never written. Code 02 ("Ricarica sulla TESSERA") is treated as such a move: an inference.
- Each voucher is a row on the portal; rows with the same second, operation, merchant and terminal are one checkout
  (3 × €8 = one €24 expense). Employer orders are income, redemptions expenses; unknown codes are held for review.
- Every sync compares Edenred's balance with the Sure account's and reports a mismatch instead of fixing it.

### Esselunga

- Session cookies (incl. the remember-me cookie), refreshed after every run. Each new receipt's PDF is parsed:
  items, Fidaty discounts, and payments (card, meal vouchers, cash); items must add up to the total.
- Voucher legs take food first (VAT 4–10%), the card the rest; an item straddling two legs is shared.
- With `SPE_RECEIPTS_DIR`, every receipt PDF the site still lists is kept (the first run backfills).

### Amazon (Europe)

- One connection signed in on amazon.it covers the whole European account: its payments page lists every EU
  store's charges, and amazon.it shows the details of orders placed on the others. Orders are labelled with their
  store; dates and prices are read in any EU store's language.
- One order = one purchase whose legs are its real card charges (per shipment, per instalment, refunds); each
  charge is spread over the order's items in proportion to their prices.

### AliExpress

- Orders come from AliExpress's own signed API, read as one growing page (its paging repeats and skips orders).
- One checkout pays several orders with one card charge: orders whose `paymentOutId` differ only in their last six
  characters are one purchase. Each order's total (shipping, customs, coins) is spread over its items; a cancelled
  order is a refund leg dated when it ended. Refunds of returned items aren't listed by AliExpress and stay as is.

### PayPal Pay in 3

- Read from the PayPal account Sure syncs: the "credito acquirenti" inflow opens a plan, the same-day merchant
  charge is the purchase, PayPal's outflows at day 0 / +1 / +2 months are the instalments, refunds reduce or
  cancel it. Plans that don't replay cleanly are held for review.
- With a Loan account chosen, each clean plan gets (for approval) the inflow matched to the loan as debt, each
  instalment matched to the loan as a loan payment with the purchase's category, and the merchant charge split into
  one excluded child, so the price is spent once, spread over the instalments. Without it, plans get notes only.

## Sure API notes

Checked against we-promise/sure `79e8a26`; re-check on upgrade.

- Auth: the user's Doorkeeper OAuth token as `Bearer`, also accepted by `/mcp`; writes need `read_write`.
  Identity from `GET /oauth/token/info` and `GET /api/v1/family_settings`.
- Never parse the formatted `amount`: use `amount_decimal`. Amounts are sent as whole-cent decimal strings.
- API creates/updates lock the fields they set against later bank-sync overwrites.
- Splits: `POST/PATCH/DELETE /api/v1/transactions/{id}/split`; children must sum to the parent; PATCH recreates
  them. Merchants can only be created through the CSV import endpoint (`POST /api/v1/merchants`).
- Web URLs take the entry id (`entry_id` in the API), not the transaction id.

## Development

```sh
uv sync
uv run pytest -q
uv run --env-file .env spe serve      # http://127.0.0.1:8790; the login browser: see compose.yaml
docker build -t sure-enricher . && docker build -t sure-enricher-login-browser docker/login-browser
```

Images are built for amd64 and arm64 and pushed to ghcr by [`.github/workflows/images.yml`](.github/workflows/images.yml)
on every push to `main` (`latest`, `sha-…`) and on `v*` tags. See [AGENTS.md](AGENTS.md) for the project's rules.

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version for others, point the UI's "Source code" link
(`src/spe/templates/base.html`) at your modified source.
