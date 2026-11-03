# IMT — NMI Transaction Report

Reports the previous day's **authorizations, captures, sales and refunds**, and
which of the day's auths were captured, across every NMI merchant account. Sends
one HTML email via Amazon SES with combined summary cards, a table grouped by
website, merchant and provider, and breakdown charts.

Merchants and gateway logins are read from the `TELCAN` database at run time, so
a new merchant is picked up without a config change.

---

## Files

| File | Purpose |
|---|---|
| `nmi_report.py` | Main script — loads accounts, queries NMI, builds charts, sends email |
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

### 2. Configure AWS credentials (for SES)

Add your IAM credentials to `C:\Users\James\.aws\credentials`, or set
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` directly in `.env`.

### 3. Configure `.env`

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

| Card | Source |
|---|---|
| Auths | Successful `auth` actions dated yesterday, excluding ones voided without capture. Detail line: count, captured (amount actually captured, including captures made since), pending capture, released |
| Captures | Successful `capture` actions dated yesterday — everything captured yesterday, including auths from earlier days |
| Sales | Successful `sale` actions dated yesterday. These auth and capture in one step and are **not** counted in Auths or Captures |
| Refunds | Successful `refund` and `credit` actions dated yesterday |

Captures and Sales include any that were later voided; the detail line shows how
many.

**Table:** website > merchant > provider, with subtotals on website and merchant
rows. Providers are the `usr_processors` rows with `cc_gateway='N'`.

**Charts:** Volume by Website and Volume by Merchant (captures plus sales),
Card Brands, Decline Reasons. Empty ones are listed in a compact "No activity in this period" note.

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
- **Not available:** the Query API has no payouts, balances or processor fees,
  so those Stripe report cards have no equivalent here.
- **Currency:** amounts are USD. Transactions in other currencies are excluded
  and counted in a footnote.
- **Sensitive data:** gateway logins stay in memory only, and `--debug` prints an
  allow-list of fields with no cardholder data.
