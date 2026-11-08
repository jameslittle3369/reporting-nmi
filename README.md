# IMT — NMI Transaction Report

Reports the previous day's **authorizations, captures, sales and refunds**, and
which of the day's auths were captured, across every NMI merchant account. Sends
one HTML email via Amazon SES with summary bullets, a table grouped by website
and provider - merchant, and breakdown charts.

Merchants and gateway logins are read from the `TELCAN` database at run time, so
a new merchant is picked up without a config change. The Stripe account
(`usr_processors` rows with `cc_gateway='S'`) and the SlimCD merchants
(`cc_gateway='D'`) are merged into the same report.

---

## Files

| File | Purpose |
|---|---|
| `nmi_report.py` | Main script — loads accounts, queries NMI, builds charts, sends email |
| `summary_bullets.txt` | The summary bullets at the top of the email (see [Summary bullets](#summary-bullets)) |
| `.env` | Configuration (database, recipients, lookback window) — not committed |
| `.env.example` | Template for `.env` |
| `requirements.txt` | Python dependencies (requests, pyodbc, boto3, matplotlib, ...) |

---

## Setup

### 1. Create the venv and install dependencies (Python 3.14)

```powershell
py -3.14 -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
```

Requires **ODBC Driver 17 for SQL Server**.

### 2. Stripe API key

Set `STRIPE_API_KEY` in `.env` to a restricted key (`rk_live_...`) with **Read**
access to Balance transactions, Charges, Disputes and Payouts. It's the same key
as `Reporting\Stripe`. If it's blank or rejected, the Stripe row shows as
Unavailable.

### 3. Configure AWS credentials (for SES)

Add your IAM credentials to `C:\Users\James\.aws\credentials`, or set
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` directly in `.env`.

### 4. Configure `.env`

```powershell
copy .env.example .env
```

Fill in `FROM_ADDRESS` and `TO_ADDRESSES` (comma-separated). The database
defaults use Windows Authentication, so the account running the script needs
read access to `usr_processors` and `usr_agents`.

---

## Running Manually

```powershell
.venv\Scripts\python.exe nmi_report.py
```

Useful flags:

```powershell
# Save the report locally instead of emailing it (good for testing)
.venv\Scripts\python.exe nmi_report.py --preview

# Print processor ids and a redacted sample transaction per gateway login
.venv\Scripts\python.exe nmi_report.py --preview --debug

# Report one specific day - e.g. to backfill a missed run
.venv\Scripts\python.exe nmi_report.py --date 2026-10-28

# Report an inclusive date range, or the last N days
.venv\Scripts\python.exe nmi_report.py --range 2026-10-01 2026-10-28
.venv\Scripts\python.exe nmi_report.py --days 7

# Override recipients for this run
.venv\Scripts\python.exe nmi_report.py --to ops@example.com ceo@example.com

# Don't write transaction IDs to gateway_transactions
.venv\Scripts\python.exe nmi_report.py --preview --no-save
```

If `FROM_ADDRESS` or `TO_ADDRESSES` are blank, the script saves the email body
locally instead of sending.

---

## Scheduling (Windows Task Scheduler)

1. **Task Scheduler** → **Create Basic Task**, trigger **Daily**, e.g. 7:00 AM (reports the previous day)
2. Action **Start a Program**:
   - **Program:** `D:\projects\reporting\NMI\.venv\Scripts\python.exe`
   - **Arguments:** `nmi_report.py`
   - **Start in:** `D:\projects\reporting\NMI`
3. Under **General**, check **Run whether user is logged on or not**, using an
   account with access to the database (Windows Authentication runs as this account)

---

## What's reported

| Column | Source |
|---|---|
| Auths | Successful `auth` actions dated yesterday, excluding ones voided without capture |
| Pending Auths | Yesterday's auths not yet captured: Auths minus the auth amount of those already captured |
| Captures | Successful `capture` actions dated yesterday — everything captured yesterday, including auths from earlier days, and including any later voided |
| Voids | Yesterday's auths released or voided without capture, at their auth amount (left out of Auths). NMI: an auth with a void; Stripe: a charge refunded while uncaptured; SlimCD: a voided `AUTH` |
| Refunds | Successful `refund` and `credit` actions dated yesterday |

**Sales** (`sale` actions, which auth and capture in one step) have no column.
They're still counted in the volume charts, and if any occur a footnote gives
their count and total.

### Summary bullets

The bullets above the table come from `summary_bullets.txt`, so you can add,
reword or reorder them without touching the script:

- One bullet per line. Blank lines and lines starting with `#` are ignored.
- Text before the first `: ` is shown in bold.
- `{name}` is replaced with the period's total:

| Placeholder | Value |
|---|---|
| `{auths}` `{auths_amt}` | Auths count / amount |
| `{captured}` `{captured_amt}` | Auths since captured / the amount actually captured |
| `{pending}` `{pending_amt}` | Pending Auths |
| `{released}` `{released_amt}` | Voids |
| `{captures}` `{captures_amt}` `{captures_voided}` | Captures, and how many were later voided |
| `{sales}` `{sales_amt}` `{sales_voided}` | Sales |
| `{refunds}` `{refunds_amt}` | Refunds |
| `{capture_rate}` | captured ÷ auths, e.g. `99.6%` |
| `{paid_out}` `{fees}` `{disputes}` `{disputed}` | Stripe-only payouts, fees, dispute count / amount |
| `{period}` | the report date or range |

If the file is missing, a built-in default list is used. A line with an unknown
placeholder is shown as written, and the console names the placeholder. Set
`SUMMARY_FILE` in `.env` to use a different file.

**Table:** a Total row, then website > provider - merchant, with subtotals on the
website rows. Providers are the `usr_processors` rows with `cc_gateway='N'` (NMI) or
`'S'` (Stripe).

**Stripe row:** uses the same definitions as the Stripe report. Auths are the
day's succeeded charges, excluding ones released without capture. Captures and
refunds come from the day's balance transactions, so captures include auths from
earlier days. A small **Stripe only** line under the row shows payouts paid,
fees and disputes, which NMI has no equivalent for. The account uses manual
capture only, so it has no sales.

**SlimCD rows:** read from the reports web service
(`stats.slimcd.com/wswebservices/reports.asmx`): `SearchTransactions` for
records, `GetOpenAuths` for auths not yet captured. The `usr_processors` username
must be the numeric **ClientID** with its client password. Record times are US
Eastern.

| Column | SlimCD source |
|---|---|
| Auths | Approved `AUTH` records in the window, excluding voided ones (see Voids) |
| Pending Auths | Auths that `GetOpenAuths` still lists. The others count as captured (`{captured}` in the summary bullets). Their captured amount is the first later approved `FORCE` on the same `cardid`; if none is found, the auth is counted without an amount and a footnote says so |
| Captures | Approved `FORCE` records in the window, including auths from earlier days |
| Voids | Approved `AUTH` records in the window that were voided |
| Refunds | Approved `CREDIT` records |

Approved `SALE` records are handled like NMI sales (see the footnote above).

Declined `AUTH`/`SALE` records feed Decline Reasons, with the trailing response
code removed. `SMS` receipt records are ignored.

**Charts:** Volume by Website and Volume by Merchant (captures plus sales),
Card Brands, Decline Reasons. Decline reasons are grouped case-insensitively
across NMI and Stripe. Empty ones are listed in a compact "No activity in this period" note.

---

## Transaction IDs for reconciliation

Every run, including `--preview`, saves one row per counted transaction to the
`gateway_transactions` table in `DB_SERVER`/`DB_DATABASE`. The script creates
the table on first run, which needs CREATE TABLE permission. Use `--no-save` to
skip saving.

The rows match the report's columns, so `count(*)`/`sum(amount)` per type for a
day equal the email's Total row:

| txn_type | Rows |
|---|---|
| `auth` | Auths. `auth_status` is `captured` or `pending` |
| `void` | Voids (auths released without capture), at the auth's time |
| `capture` | Captures |
| `refund` | Refunds |
| `sale` | Sales (footnote only) |

| Column | Value |
|---|---|
| gateway | `nmi`, `stripe` or `slimcd` |
| processor_row_id, agentid, site_agentid, gateway_login | the `usr_processors` row the transaction was counted under |
| transaction_id | the gateway's ID, as the backend stores it in `RequestID`: NMI `transaction_id`, SlimCD `gateid`, Stripe PaymentIntent `pi_...` |
| amount | the amount the report counts (auth amount for `auth`/`void`) |
| txn_time | when the action happened, in `TIMEZONE` |
| loaded_at | when the run wrote the row |

- **One ID, several rows:** NMI's auth and capture are actions on one
  transaction, so a captured NMI auth has an `auth` row and a `capture` row with
  the same ID. The same goes for Stripe's PaymentIntent. SlimCD gives each
  record its own `gateid`.
- **Re-runs:** re-running a day replaces that day's rows for every login that
  was read, so auths captured or voided since then are updated rather than
  duplicated. An Unavailable login keeps its earlier rows.

Example check for gateway auths with no matching backend record (swap in the
backend table you reconcile against):

```sql
select g.*
from gateway_transactions g
where g.txn_type = 'auth'
  and g.txn_time >= '2026-11-02' and g.txn_time < '2026-11-03'
  and not exists (select 1 from Log_ApprovedPhoneCardCustomers b
                  where b.RequestID = g.transaction_id)
```

---

## Notes

- **Database freshness:** `DB_SERVER` should hold current `usr_processors` rows.
  The default `JAMESDESKTOP\JDESKTOPSQL` / `TELCAN` was restored from a backup on
  2025-04-05, so it can lack newer merchants and hold outdated gateway passwords.
  A login rejected as `Invalid Username/Password` may just have a newer password.
- **Dates:** NMI's `start_date`/`end_date` filter on a transaction's
  *last-modified* date, so the script fetches from the day before the report
  window through now, then filters each action by its own date (UTC, converted
  to `TIMEZONE`). Totals for a past day can move if the same day is re-run after
  later captures or voids.
- **Shared logins:** `usr_processors` rows sharing a gateway login are queried
  once. Transactions are matched to a row by `processor_id` = `gateway_id`;
  anything unmatched on a multi-row login goes to that login's first row, with a
  footnote.
- **Failures:** a login that can't be read shows as **Unavailable**, the email
  gets an "incomplete" warning and subject suffix, and it is never reported as
  zero. If every login fails, or the database can't be reached, the script exits
  non-zero and sends nothing.
- **Gateway hosts:** the database doesn't store a host. Use `NMI_URL_OVERRIDES`
  in `.env` for a merchant on a private-label host.
- **Not available for NMI:** the Query API has no payouts, balances or
  processor fees, so those appear only on the Stripe-only line.
- **Currency:** amounts are USD. Transactions in other currencies are excluded
  and counted in a footnote.
- **Sensitive data:** gateway logins stay in memory only, and `--debug` prints an
  allow-list of fields with no cardholder data.
