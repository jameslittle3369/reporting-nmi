#!/usr/bin/env python3
"""
nmi_report.py
-------------
Reports the previous day's NMI gateway activity - authorizations, captures,
sales and refunds, and which of the day's auths were captured - across every
NMI merchant account, and sends one HTML email via Amazon SES.

The merchant list and gateway logins come from the TELCAN database
(usr_processors rows with cc_gateway='N'), so a new merchant is picked up
without a config change. NMI has no reporting endpoint, so everything is
derived from the Query API's per-transaction action history. An account that
can't be read is flagged as Unavailable rather than reported as zero.

Setup:
  1. copy .env.example .env   and fill in the database and AWS SES / recipient
     settings.
  2. .venv\\Scripts\\pip.exe install -r requirements.txt
  3. .venv\\Scripts\\python.exe nmi_report.py --preview --debug

Usage:
  python nmi_report.py                  # build and send the report
  python nmi_report.py --preview        # save locally, don't send
  python nmi_report.py --days 7         # override the lookback window
  python nmi_report.py --date 2026-10-28                # one specific day
  python nmi_report.py --range 2026-10-01 2026-10-28    # inclusive date range
  python nmi_report.py --to a@b.com     # override recipients for this run
  python nmi_report.py --debug          # print a redacted sample per login
  python nmi_report.py --no-save        # don't write IDs to gateway_transactions
"""

import argparse
import base64
import html
import io
import json
import os
import re
import struct
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import email.mime.image
import email.mime.multipart
import email.mime.text

import boto3
import pyodbc
import requests
import stripe
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# -- CONFIG (loaded from .env) ------------------------------------------------
DB_SERVER   = os.getenv("DB_SERVER", r"JAMESDESKTOP\JDESKTOPSQL")
DB_DATABASE = os.getenv("DB_DATABASE", "TELCAN")
DB_DRIVER   = os.getenv("DB_DRIVER", "{ODBC Driver 17 for SQL Server}")
DB_TRUSTED_CONNECTION = os.getenv("DB_TRUSTED_CONNECTION", "true").lower() == "true"
DB_USERNAME = os.getenv("DB_USERNAME", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

NMI_QUERY_URL = os.getenv("NMI_QUERY_URL", "https://secure.networkmerchants.com/api/query.php")
# Comma-separated agentid:url pairs, for merchants on a private-label host.
NMI_URL_OVERRIDES = dict(p.strip().split(":", 1) for p in os.getenv("NMI_URL_OVERRIDES", "").split(",") if p.strip())

# Used for usr_processors rows with cc_gateway='S'.
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")

AWS_REGION            = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")

FROM_ADDRESS = os.getenv("FROM_ADDRESS", "")
FROM_NAME    = os.getenv("FROM_NAME", "James Little")
TO_ADDRESSES = [a.strip() for a in os.getenv("TO_ADDRESSES", "").split(",") if a.strip()]

REPORT_DAYS = int(os.getenv("REPORT_DAYS", "1"))
TIMEZONE    = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

DEBUG = False
# -----------------------------------------------------------------------------

ses_kwargs = {"region_name": AWS_REGION}
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    ses_kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
    ses_kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
ses = boto3.client("sesv2", **ses_kwargs)

CURRENCY = "usd"
RESULT_LIMIT = 1000
ACCOUNTS_SQL = """
    select p.id row_id, p.agentid, m.FirstName merchant, p.site_agentid, s.FirstName website,
           p.company_name, p.gateway_id, p.cc_gateway, p.cc_gateway_username, p.cc_gateway_password
    from usr_processors p with (nolock)
    left join usr_agents m with (nolock) on p.agentid = m.AgentId
    left join usr_agents s with (nolock) on p.site_agentid = s.AgentId
    where p.cc_gateway in ('N', 'S', 'D')
"""
# SlimCD reports web service (Transaction Gateway Manual, "Reports"). Its
# transaction dates are US Eastern; UserName is the numeric ClientID.
SLIMCD_URL = "https://stats.slimcd.com/wswebservices/reports.asmx/"
SLIMCD_TZ = ZoneInfo("America/New_York")
SLIMCD_MAX_RECORDS = 50000
SLIMCD_BRANDS = {"V": "visa", "M": "mastercard", "A": "amex", "D": "discover"}
# Stripe balance transaction types (see Reporting\Stripe\stripe_report.py).
STRIPE_PAYOUT_TYPES = {"payout", "payout_cancel", "payout_failure", "payout_minimum_balance_hold",
                       "payout_minimum_balance_release"}
STRIPE_CHARGE_TYPES = {"charge", "payment"}
STRIPE_REFUND_TYPES = {"refund", "payment_refund"}


def safe_id(s: str) -> str:
    """Turn a string into a safe CSS/JS identifier (must not start with a digit)."""
    clean = re.sub(r"[^a-zA-Z0-9]", "_", s)
    if clean and clean[0].isdigit():
        clean = "n" + clean
    return clean


def fmt_int(v) -> str:
    try:
        return f"{int(round(v)):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_money(v) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def fmt_value(v, fmt):
    return fmt_money(v) if fmt == "money" else fmt_int(v)


def nice_label(s) -> str:
    """'mastercard' / 'do_not_honor' -> 'Mastercard' / 'Do Not Honor'."""
    return str(s).replace("_", " ").title() if s else "Unknown"


def plural(n, word) -> str:
    return f"{n:,.0f} {word}" + ("" if n == 1 else "s")


# ---------------------------------------------------------------------------
# Merchant accounts (TELCAN database)
# ---------------------------------------------------------------------------

def db_connect():
    if DB_TRUSTED_CONNECTION:
        auth = "Trusted_Connection=yes"
    else:
        auth = f"UID={DB_USERNAME};PWD={DB_PASSWORD}"
    return pyodbc.connect(f"DRIVER={DB_DRIVER};SERVER={DB_SERVER};DATABASE={DB_DATABASE};{auth}", timeout=15)


def load_logins():
    """One entry per distinct gateway login, each with its usr_processors rows.
    Rows can share a login, so querying per row would double-count that
    login's transactions. Stripe rows ('S') share the one STRIPE_API_KEY."""
    conn = db_connect()
    try:
        rows = conn.cursor().execute(ACCOUNTS_SQL).fetchall()
    finally:
        conn.close()

    logins = {}
    for r in rows:
        agentid, site_agentid = (r.agentid or "").strip(), (r.site_agentid or "").strip()
        gateway = {"S": "stripe", "D": "slimcd"}.get(r.cc_gateway, "nmi")
        key = (gateway,) if gateway == "stripe" else (gateway, r.cc_gateway_username, r.cc_gateway_password)
        login = logins.setdefault(key, {
            "gateway": gateway,
            "username": r.cc_gateway_username or "", "password": r.cc_gateway_password or "",
            "url": NMI_URL_OVERRIDES.get(agentid, NMI_QUERY_URL), "rows": [],
        })
        login["rows"].append({
            "row_id": str(r.row_id or "").strip(), "agentid": agentid, "site_agentid": site_agentid,
            "website": (r.website or site_agentid or "Unknown").strip(),
            "merchant": (r.merchant or agentid or "Unknown").strip(),
            "provider": (r.company_name or "Unknown").strip(),
            "gateway_id": (r.gateway_id or "").strip(),
            "stats": Counter(),
            "extras": None,  # Stripe-only payouts / fees / disputes
            "unavailable": False,
            "txns": [],  # gateway IDs saved to TXN_TABLE for backend reconciliation
        })
    return list(logins.values())


# ---------------------------------------------------------------------------
# NMI Query API
# ---------------------------------------------------------------------------

class GatewayError(Exception):
    pass


def new_session():
    # query.php is read-only, so POST is safe to retry.
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=None)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def utc_stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def scrub(text, login):
    """Gateway error text goes to the console, so keep the password out of it."""
    return text.replace(login["password"], "***") if login["password"] else text


def parse_action(el):
    raw = el.findtext("date") or ""
    try:
        when = datetime.strptime(raw[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        when = None
    try:
        amount = float(el.findtext("amount") or 0)
    except ValueError:
        amount = 0.0
    return {
        "type": (el.findtext("action_type") or "").lower(),
        "amount": amount,
        "when": when,
        "success": el.findtext("success") == "1",
        "response_text": el.findtext("response_text") or "",
        "raw_date": raw,
    }


def parse_transaction(el):
    return {
        "id": el.findtext("transaction_id"),
        "condition": el.findtext("condition"),
        "processor_id": (el.findtext("processor_id") or "").strip(),
        "cc_type": (el.findtext("cc_type") or "").strip().lower(),
        "currency": (el.findtext("currency") or CURRENCY).strip().lower(),
        "actions": [parse_action(a) for a in el.findall("action")],
    }


def fetch_transactions(session, login, start_dt, end_dt):
    """Every transaction modified from the day before the window through now.
    start_date/end_date filter on LAST-MODIFIED date (any action updates it) and
    the docs don't state their timezone, so this over-fetches on purpose:
    tally() then filters each action by its own date. Running through now also
    picks up captures made after the window on the window's auths."""
    params = {
        "username": login["username"], "password": login["password"],
        "start_date": utc_stamp(start_dt - timedelta(days=1)),
        "end_date": utc_stamp(max(end_dt, datetime.now(TIMEZONE)) + timedelta(days=1)),
        "result_limit": RESULT_LIMIT,
    }
    txs = {}
    page = 0
    while True:
        try:
            resp = session.post(login["url"], data={**params, "page_number": page}, timeout=120)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except requests.RequestException as e:
            raise GatewayError(type(e).__name__) from None
        except ET.ParseError:
            raise GatewayError(scrub("non-XML response: " + resp.text[:120], login)) from None
        err = root.findtext("error_response")
        if err:
            raise GatewayError(scrub(err[:200], login))
        batch = root.findall("transaction")
        before = len(txs)
        for el in batch:
            tx = parse_transaction(el)
            txs[tx["id"]] = tx
        if len(batch) < RESULT_LIMIT or len(txs) == before:
            return list(txs.values())
        page += 1


def debug_sample(tx):
    """Allow-listed fields only - no cardholder data reaches the console."""
    return {
        **{k: tx[k] for k in ("id", "condition", "processor_id", "cc_type", "currency")},
        "actions": [{k: a[k] for k in ("type", "amount", "raw_date", "success", "response_text")}
                    for a in tx["actions"]],
    }


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------

def tally(tx, stats, start_dt, end_dt, breakdowns, row):
    """Adds one transaction's in-window actions to stats. A capture counts on
    its own date, so it can belong to an auth from an earlier day; an auth's
    captured/pending status uses the transaction's full history, so a capture
    made after the window still marks it captured."""
    done = [a for a in tx["actions"] if a["success"]]
    voided = any(a["type"] == "void" for a in done)
    captures = [a for a in done if a["type"] == "capture"]
    captured_amt = sum(a["amount"] for a in captures)

    def in_window(a):
        return a["when"] is not None and start_dt <= a["when"] < end_dt

    for a in filter(in_window, done):
        kind, amt = a["type"], a["amount"]
        if kind == "auth":
            if voided:  # released without capture; left out of the total, matching the Stripe report
                stats["released"] += 1
                stats["released_amt"] += amt
                record(row, "void", tx["id"], amt, a["when"])
                continue
            stats["auths"] += 1
            stats["auths_amt"] += amt
            breakdowns["brands"][tx["cc_type"] or None] += 1
            if captures:
                stats["captured"] += 1
                stats["captured_amt"] += captured_amt
            else:
                stats["pending"] += 1
                stats["pending_amt"] += amt
            record(row, "auth", tx["id"], amt, a["when"], "captured" if captures else "pending")
        elif kind == "capture":
            stats["captures"] += 1
            stats["captures_amt"] += amt
            stats["captures_voided"] += voided
            breakdowns["website_volume"][row["website"]] += amt
            breakdowns["volume"][row["merchant"]] += amt
            record(row, "capture", tx["id"], amt, a["when"])
        elif kind == "sale":
            stats["sales"] += 1
            stats["sales_amt"] += amt
            stats["sales_voided"] += voided
            breakdowns["website_volume"][row["website"]] += amt
            breakdowns["volume"][row["merchant"]] += amt
            breakdowns["brands"][tx["cc_type"] or None] += 1
            record(row, "sale", tx["id"], amt, a["when"])
        elif kind in ("refund", "credit"):
            stats["refunds"] += 1
            stats["refunds_amt"] += abs(amt)
            record(row, "refund", tx["id"], abs(amt), a["when"])

    for a in tx["actions"]:
        if not a["success"] and a["type"] in ("auth", "sale") and in_window(a):
            add_decline(breakdowns, re.sub(r"\s*REFID:\S+", "", a["response_text"]).strip())


def record(row, kind, txn_id, amount, when, status=None):
    """Keeps a counted transaction's gateway ID for save_transactions()."""
    row["txns"].append((kind, txn_id or "", amount, when, status))


def add_decline(breakdowns, reason):
    """Counts decline reasons case-insensitively, so NMI's 'Insufficient funds'
    and Stripe's insufficient_funds share one bar (labelled as first seen)."""
    reason = reason or "Unknown"
    breakdowns["declines"][reason.lower()] += 1
    breakdowns["decline_labels"].setdefault(reason.lower(), reason)


# ---------------------------------------------------------------------------
# Stripe (usr_processors rows with cc_gateway='S')
# ---------------------------------------------------------------------------

def fetch_stripe(row, start_dt, end_dt, breakdowns, other_currency):
    """Fills one row from Stripe, using the Stripe report's definitions: auths
    are the window's succeeded charges (released ones excluded), captures and
    refunds are the window's balance transactions, so a capture counts on its
    own day like NMI's. Payouts, fees and disputes have no NMI equivalent and go
    in row["extras"]. Returns a short summary for the console."""
    if not STRIPE_API_KEY:
        raise GatewayError("STRIPE_API_KEY not set in .env")
    client = stripe.StripeClient(STRIPE_API_KEY, max_network_retries=3)
    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
    created = {"created": {"gte": start_ts, "lt": end_ts}}

    def fetch(service, params):
        try:
            items = [o.to_dict() for o in service.list({**params, "limit": 100}).auto_paging_iter()]
        except stripe.StripeError as e:
            raise GatewayError(f"{type(e).__name__}: {e.user_message or e}") from None
        usd = [o for o in items if (o.get("currency") or CURRENCY).lower() == CURRENCY]
        for o in items:
            if o not in usd:
                other_currency[o["currency"].upper()] += 1
        return usd

    def major(cents):
        return (cents or 0) / 100  # USD only - other currencies are excluded above

    def stamp(ts):
        return datetime.fromtimestamp(ts, timezone.utc)

    def intent_id(obj):  # the backend stores the PaymentIntent (pi_...) as RequestID
        pi = obj.get("payment_intent")
        return (pi.get("id") if isinstance(pi, dict) else pi) or obj["id"]

    def source_id(b):  # PaymentIntent of the charge or refund behind a balance transaction
        src = b.get("source")
        return intent_id(src) if isinstance(src, dict) else (src or b["id"])

    charges = fetch(client.v1.charges, created)
    bts = fetch(client.v1.balance_transactions, {**created, "expand": ["data.source"]})
    payouts = fetch(client.v1.payouts, {"arrival_date": {"gte": start_ts, "lt": end_ts}})
    disputes = fetch(client.v1.disputes, created)

    stats = row["stats"]
    for c in charges:
        if c["status"] == "failed":
            add_decline(breakdowns, nice_label((c.get("outcome") or {}).get("reason") or c.get("failure_code")))
        elif c["status"] == "succeeded":
            if c.get("refunded") and not c.get("captured"):  # released without capture
                stats["released"] += 1
                stats["released_amt"] += major(c["amount"])
                record(row, "void", intent_id(c), major(c["amount"]), stamp(c["created"]))
                continue
            amt = major(c["amount"])
            stats["auths"] += 1
            stats["auths_amt"] += amt
            pmd = c.get("payment_method_details") or {}
            breakdowns["brands"][(pmd.get("card") or pmd.get("card_present") or {}).get("brand")] += 1
            if c.get("captured"):
                stats["captured"] += 1
                stats["captured_amt"] += major(c.get("amount_captured"))
            else:
                stats["pending"] += 1
                stats["pending_amt"] += amt
            record(row, "auth", intent_id(c), amt, stamp(c["created"]), "captured" if c.get("captured") else "pending")

    for b in bts:
        if b["type"] in STRIPE_CHARGE_TYPES:
            amt = major(b["amount"])
            stats["captures"] += 1
            stats["captures_amt"] += amt
            breakdowns["website_volume"][row["website"]] += amt
            breakdowns["volume"][row["merchant"]] += amt
            record(row, "capture", source_id(b), amt, stamp(b["created"]))
        elif b["type"] in STRIPE_REFUND_TYPES:
            stats["refunds"] += 1
            stats["refunds_amt"] += -major(b["amount"])
            record(row, "refund", source_id(b), -major(b["amount"]), stamp(b["created"]))

    row["extras"] = {
        "paid_out": sum(major(p["amount"]) for p in payouts if p["status"] == "paid"),
        "fees": sum(major(b["fee"]) for b in bts if b["type"] not in STRIPE_PAYOUT_TYPES),
        "disputes": len(disputes),
        "disputed": sum(major(d["amount"]) for d in disputes),
    }
    return (f"{len(charges):,} charges, {len(bts):,} balance transactions, "
            f"{len(payouts):,} payouts, {len(disputes):,} disputes")


# ---------------------------------------------------------------------------
# SlimCD (usr_processors rows with cc_gateway='D')
# ---------------------------------------------------------------------------

def slimcd_call(session, login, op, **params):
    """POSTs one reports.asmx operation and returns its <reply> element."""
    resp = None
    try:
        resp = session.post(SLIMCD_URL + op, timeout=180,
                            data={"UserName": login["username"], "Password": login["password"], **params})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except requests.RequestException as e:
        raise GatewayError(type(e).__name__) from None
    except ET.ParseError:
        raise GatewayError(scrub("non-XML response: " + resp.text[:120], login)) from None
    if root.findtext("response") != "Success":
        raise GatewayError(scrub(root.findtext("description") or "unknown error", login)[:200])
    return root


def parse_slimcd(el):
    def text(key):
        return (el.findtext(key) or "").strip()

    try:
        when = datetime.strptime(text("transactiondate"), "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=SLIMCD_TZ)
    except ValueError:
        when = None
    try:
        amount = float(text("amount") or 0)
    except ValueError:
        amount = 0.0
    card = text("cardtype").upper()
    return {
        "gateid": text("gateid"), "when": when, "type": text("transtype").upper(), "approved": text("approved"),
        "amount": amount, "brand": SLIMCD_BRANDS.get(card, card.lower() or None), "cardid": text("cardid"),
        "siteid": text("siteid"), "voided": text("voided") not in ("", "0"), "proc_response": text("proc_response"),
    }


def fetch_slimcd(session, login, row, start_dt, end_dt, breakdowns, notes):
    """Fills one row from SlimCD. A capture (FORCE) is its own record, so it
    counts on its own day like NMI's; an auth is captured when GetOpenAuths no
    longer lists it, and its captured amount comes from the first later FORCE
    on the same cardid. Records are fetched through today so captures and voids
    made after the window are visible. Returns a short summary for the console."""
    dates = {"StartDate": start_dt.astimezone(SLIMCD_TZ).strftime("%m/%d/%Y"),
             "EndDate": datetime.now(SLIMCD_TZ).strftime("%m/%d/%Y")}  # EndDate is inclusive
    root = slimcd_call(session, login, "SearchTransactions", RecordCountOnly="false",
                       MaxRecords=SLIMCD_MAX_RECORDS, SearchXML="<search></search>", **dates)
    recs = [parse_slimcd(el) for el in root.iter("Transaction")]
    if len(recs) >= SLIMCD_MAX_RECORDS:
        raise GatewayError(f"result truncated at {SLIMCD_MAX_RECORDS:,} records")
    open_auths = set()
    for site in sorted({r["siteid"] for r in recs if r["siteid"]}):
        reply = slimcd_call(session, login, "GetOpenAuths", SiteID=site, **dates)
        open_auths |= {(el.findtext("gateid") or "").strip() for el in reply.iter("Openauth")}

    in_window = [r for r in recs if r["when"] is not None and start_dt <= r["when"] < end_dt]
    forces = sorted((r for r in recs if r["type"] == "FORCE" and r["approved"] == "Y" and r["cardid"]),
                    key=lambda r: r["when"] or start_dt)
    used_forces = set()
    unlinked = 0
    stats = row["stats"]
    for r in in_window:
        kind, amt = r["type"], r["amount"]
        if r["approved"] == "N":
            if kind in ("AUTH", "SALE"):
                add_decline(breakdowns, re.sub(r"\s+\d+$", "", r["proc_response"]))
            continue
        if r["approved"] != "Y":
            continue
        if kind == "AUTH":
            if r["voided"]:  # released without capture; left out of the total, matching NMI and Stripe
                stats["released"] += 1
                stats["released_amt"] += amt
                record(row, "void", r["gateid"], amt, r["when"])
                continue
            stats["auths"] += 1
            stats["auths_amt"] += amt
            breakdowns["brands"][r["brand"]] += 1
            if r["gateid"] in open_auths:
                stats["pending"] += 1
                stats["pending_amt"] += amt
                record(row, "auth", r["gateid"], amt, r["when"], "pending")
                continue
            stats["captured"] += 1
            record(row, "auth", r["gateid"], amt, r["when"], "captured")
            force = next((f for f in forces if f["gateid"] not in used_forces and f["cardid"] == r["cardid"]
                          and f["when"] and f["when"] >= r["when"]), None)
            if force:
                used_forces.add(force["gateid"])
                stats["captured_amt"] += force["amount"]
            else:
                unlinked += 1
        elif kind == "FORCE":
            stats["captures"] += 1
            stats["captures_amt"] += amt
            stats["captures_voided"] += r["voided"]
            breakdowns["website_volume"][row["website"]] += amt
            breakdowns["volume"][row["merchant"]] += amt
            record(row, "capture", r["gateid"], amt, r["when"])
        elif kind == "SALE":
            stats["sales"] += 1
            stats["sales_amt"] += amt
            stats["sales_voided"] += r["voided"]
            breakdowns["website_volume"][row["website"]] += amt
            breakdowns["volume"][row["merchant"]] += amt
            breakdowns["brands"][r["brand"]] += 1
            record(row, "sale", r["gateid"], amt, r["when"])
        elif kind == "CREDIT":
            stats["refunds"] += 1
            stats["refunds_amt"] += abs(amt)
            record(row, "refund", r["gateid"], abs(amt), r["when"])

    if unlinked:
        notes.append(f"{unlinked:,} captured SlimCD auth(s) for {html.escape(row['merchant'])} had no matching "
                     f"FORCE record, so their captured amount isn't included.")
    if DEBUG:
        print(f"    in window by type/approved: {dict(Counter((r['type'], r['approved']) for r in in_window))}"
              f" | open auths: {len(open_auths):,}")
    return f"{len(recs):,} records ({len(in_window):,} in window)"


def collect(logins, start_dt, end_dt, breakdowns, notes):
    """Fills each row's stats. Returns the labels of logins that couldn't be read."""
    session = new_session()
    failed = []
    other_currency = Counter()
    unmatched = Counter()
    for login in logins:
        rows = login["rows"]
        label = f"{rows[0]['merchant']} / {rows[0]['provider']} ({rows[0]['gateway_id']})"
        try:
            if login["gateway"] == "stripe":
                print(f"  {label}: {fetch_stripe(rows[0], start_dt, end_dt, breakdowns, other_currency)}")
                continue
            if login["gateway"] == "slimcd":
                print(f"  {label}: {fetch_slimcd(session, login, rows[0], start_dt, end_dt, breakdowns, notes)}")
                continue
            txs = fetch_transactions(session, login, start_dt, end_dt)
        except GatewayError as e:
            print(f"  {label}: UNAVAILABLE ({e})")
            failed.append(label)
            for row in rows:
                row["unavailable"] = True
            continue
        print(f"  {label}: {len(txs):,} transactions")

        by_gateway = {r["gateway_id"].lower(): r for r in rows}
        if DEBUG:
            ids = Counter(t["processor_id"] for t in txs)
            print(f"    processor_ids: {dict(ids)} | gateway_ids: {sorted(by_gateway)}")
            if txs:
                print("    " + json.dumps(debug_sample(txs[-1]), default=str)[:1500])

        for tx in txs:
            if tx["currency"] != CURRENCY:
                other_currency[tx["currency"].upper()] += 1
                continue
            row = by_gateway.get(tx["processor_id"].lower())
            if row is None:
                row = rows[0]
                if len(rows) > 1:
                    unmatched[(tx["processor_id"] or "(blank)", label)] += 1
            tally(tx, row["stats"], start_dt, end_dt, breakdowns, row)

    if other_currency:
        notes.append(f"Amounts are in {CURRENCY.upper()}. Excluded "
                     + ", ".join(f"{n:,} {c} transaction(s)" for c, n in other_currency.most_common()) + ".")
    for (pid, label), n in unmatched.most_common():
        notes.append(f"{n:,} transaction(s) with processor_id {html.escape(pid)} matched no usr_processors row "
                     f"and were counted under {html.escape(label)}.")
    return failed


# ---------------------------------------------------------------------------
# Transaction IDs for backend reconciliation
# ---------------------------------------------------------------------------

TXN_TABLE = "gateway_transactions"
TXN_TABLE_SQL = f"""
if object_id('{TXN_TABLE}') is null
begin
    create table {TXN_TABLE} (
        id int identity primary key,
        gateway varchar(10) not null,
        processor_row_id varchar(50) not null,
        agentid varchar(50) null,
        site_agentid varchar(50) null,
        gateway_login varchar(50) null,
        txn_type varchar(10) not null,
        transaction_id varchar(64) not null,
        auth_status varchar(10) null,
        amount decimal(12, 2) not null,
        txn_time datetime not null,
        loaded_at datetime not null default getdate()
    );
    create index ix_{TXN_TABLE}_type_id on {TXN_TABLE} (txn_type, transaction_id);
    create index ix_{TXN_TABLE}_row_time on {TXN_TABLE} (processor_row_id, txn_time);
end
"""


def save_transactions(logins, start_dt, end_dt):
    """Replaces the window's rows for every login that was read, in one DB
    transaction, so re-running a day corrects auths captured or voided since
    instead of duplicating them. Unavailable logins keep their earlier rows.
    Times are stored in TIMEZONE. Returns the number of rows written."""
    def local(dt):
        return dt.astimezone(TIMEZONE).replace(tzinfo=None)

    window = (local(start_dt), local(end_dt))
    rows = [(login["gateway"], row) for login in logins for row in login["rows"] if not row["unavailable"]]
    values = [(gateway, row["row_id"], row["agentid"], row["site_agentid"], row["gateway_id"],
               kind, txn_id, status, Decimal(f"{amount:.2f}"), local(when))
              for gateway, row in rows for kind, txn_id, amount, when, status in row["txns"]]
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(TXN_TABLE_SQL)
        cur.executemany(f"delete from {TXN_TABLE} where processor_row_id = ? and txn_time >= ? and txn_time < ?",
                        [(row["row_id"], *window) for _, row in rows])
        if values:
            cur.fast_executemany = True
            cur.executemany(f"insert into {TXN_TABLE} (gateway, processor_row_id, agentid, site_agentid, "
                            "gateway_login, txn_type, transaction_id, auth_status, amount, txn_time) "
                            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
        conn.commit()
    finally:
        conn.close()
    return len(values)


# ---------------------------------------------------------------------------
# Chart generation (matplotlib, Agg backend, CID-embedded PNG images)
# ---------------------------------------------------------------------------

PALETTE = ["#4A90D9", "#E8704A", "#50C878", "#9B59B6", "#F1C40F", "#1ABC9C", "#F39C12", "#EC7063"]


def chart_cid(spec) -> str:
    return "chart_" + spec["id"]


def chart_to_png(fig):
    """Renders a matplotlib figure to raw PNG bytes (not base64) - these get
    attached as inline MIMEImage/Content-ID parts rather than embedded as a
    data: URI, since Outlook desktop's Word-based rendering engine mishandles
    large base64 image data (crops it, shows a broken linked-image icon)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    data = buf.read()
    plt.close(fig)
    native_w = struct.unpack(">I", data[16:20])[0]
    native_h = struct.unpack(">I", data[20:24])[0]
    return data, native_w, native_h


def axis_formatter(fmt):
    def money(x, _):
        a = abs(x)
        s = f"{a / 1e6:.1f}M" if a >= 1e6 else f"{a / 1e3:.1f}k" if a >= 1e4 else f"{a:,.0f}"
        return ("-" if x < 0 else "") + "$" + s

    return money if fmt == "money" else (lambda x, _: f"{int(x):,}")


def bar_spec(title, counter, fmt="int", top=12, labeler=nice_label):
    rows = [(labeler(k), v) for k, v in sorted(counter.items(), key=lambda kv: kv[1], reverse=True) if v]
    return {"id": safe_id(title), "title": title, "rows": rows[:top], "fmt": fmt}


def make_bar_chart(spec):
    """Horizontal bar chart of (label, value) rows."""
    labels = [r[0] for r in spec["rows"]]
    values = [r[1] for r in spec["rows"]]
    maxv = max(values) if values else 1

    fig, ax = plt.subplots(figsize=(7, max(2.5, len(labels) * 0.4 + 1)))
    fig.patch.set_facecolor("#F8F9FA")
    ax.set_facecolor("#F8F9FA")
    bars = ax.barh(labels, values, color=PALETTE[0], height=0.6)
    for bar, v in zip(bars, values):
        ax.text(bar.get_width() + maxv * 0.015, bar.get_y() + bar.get_height() / 2,
                fmt_value(v, spec["fmt"]), va="center", fontsize=8, color="#333")
    ax.set_ylim(max(len(labels), 3) - 0.5, -0.5)  # top-down order; keeps 1-2 bars from filling the plot
    ax.set_xlim(0, maxv * 1.18)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(axis_formatter(spec["fmt"])))
    ax.set_title(spec["title"], fontsize=13, fontweight="bold", pad=10)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", labelsize=8)
    ax.tick_params(axis="x", labelsize=8)
    fig.tight_layout()
    return chart_to_png(fig)


def img_tag(src, native_w, native_h, alt="chart", display_w=600):
    """Outlook-safe image tag wrapped in a 1x1 presentation table - explicit
    width/height attributes, not CSS, are required because Outlook's
    Word-based rendering engine mishandles CSS image sizing. `src` is a full
    src attribute value, e.g. "cid:chart_xxx" for the real email (resolved
    against an inline MIMEImage part) or a data: URI for local preview."""
    display_h = round(native_h * display_w / native_w)
    dw, dh = str(display_w), str(display_h)
    return (
        '<table width="' + dw + '" cellpadding="0" cellspacing="0" border="0" role="presentation" '
        'style="border:0;mso-table-lspace:0;mso-table-rspace:0;">'
        '<tr><td>'
        '<img src="' + src + '" alt="' + alt + '" '
        'width="' + dw + '" height="' + dh + '" '
        'style="display:block;border-radius:6px;">'
        '</td></tr></table>'
    )


def inline_images_for_preview(html_body, images):
    """Substitutes cid: references back to self-contained data: URIs for the
    local --preview save path, since cid: only resolves inside a real MIME
    container (an opened email) - a plain saved .html file needs the image
    bytes inline to be viewable standalone in a browser."""
    for cid, png_bytes in images.items():
        b64 = base64.b64encode(png_bytes).decode()
        html_body = html_body.replace(
            'src="cid:' + cid + '"',
            'src="data:image/png;base64,' + b64 + '"',
        )
    return html_body


def stat_box(label, value, color="#4A90D9", detail=None):
    return (
        '<div style="flex:1;min-width:140px;background:#fff;border-radius:8px;'
        'padding:16px 12px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08);">'
        '<div style="font-size:22px;font-weight:700;color:' + color + ';">' + str(value) + '</div>'
        '<div style="font-size:11px;color:#666;margin-top:4px;">' + label + '</div>'
        + ('<div style="font-size:10px;color:#999;margin-top:2px;">' + detail + '</div>' if detail else '')
        + '</div>'
    )


# ---------------------------------------------------------------------------
# Email body (light theme, CID-embedded PNG charts - see img_tag/send_email)
# ---------------------------------------------------------------------------

# Table columns: (heading, count key, amount key).
COLUMNS = [
    ("Auths", "auths", "auths_amt"), ("Pending Auths", "pending", "pending_amt"),
    ("Captures", "captures", "captures_amt"), ("Voids", "released", "released_amt"),
    ("Refunds", "refunds", "refunds_amt"),
]


def add_stats(rows):
    total = Counter()
    for row in rows:
        total.update(row["stats"])
    return total


def table_row(label, stats, indent=0, bg="#fff", weight=400):
    td = f'padding:6px 5px;border-bottom:1px solid #eee;font-size:12px;font-weight:{weight};'
    cells = [f'<td style="{td}padding-left:{5 + indent}px;">{label}</td>']
    for _, count, amount in COLUMNS:
        if stats[count]:
            cells.append(f'<td align="right" style="{td}">{fmt_money(stats[amount])}'
                         f'<div style="font-size:10px;color:#999;font-weight:400;">{fmt_int(stats[count])}</div></td>')
        else:
            cells.append(f'<td align="right" style="{td}color:#ccc;">&mdash;</td>')
    return f'<tr style="background:{bg};">' + "".join(cells) + "</tr>"


def unavailable_row(label, indent):
    td = 'padding:6px 5px;border-bottom:1px solid #eee;font-size:12px;'
    return (f'<tr style="background:#fff5f5;"><td style="{td}padding-left:{5 + indent}px;">{label}</td>'
            f'<td colspan="{len(COLUMNS)}" align="center" style="{td}color:#c0392b;font-weight:600;">Unavailable</td></tr>')


def extras_row(extras, indent):
    """Stripe-only figures under the Stripe provider row."""
    text = (f"Stripe only: Paid out {fmt_money(extras['paid_out'])} &middot; Fees {fmt_money(extras['fees'])}"
            f" &middot; Disputes {fmt_int(extras['disputes'])} ({fmt_money(extras['disputed'])})")
    return (f'<tr><td colspan="{len(COLUMNS) + 1}" style="padding:0 5px 6px {5 + indent}px;'
            f'border-bottom:1px solid #eee;font-size:11px;color:#888;">{text}</td></tr>')


def build_table(logins):
    """Total, then website > provider - merchant, with subtotals on the website rows."""
    tree = defaultdict(list)
    for login in logins:
        for row in login["rows"]:
            tree[row["website"]].append(row)

    head = "".join(f'<th align="right" style="padding:6px 5px;">{c[0]}</th>' for c in COLUMNS)
    parts = ['<table width="100%" cellpadding="0" cellspacing="0" border="0" '
             'style="border-collapse:collapse;margin-bottom:24px;color:#333;">',
             '<tr style="background:#4B45C6;color:#fff;font-size:12px;">'
             '<th align="left" style="padding:6px 5px;">Website / Provider - Merchant</th>' + head + '</tr>',
             table_row("Total", add_stats(r for rows in tree.values() for r in rows), bg="#D8D8F5", weight=700)]
    for website in sorted(tree):
        parts.append(table_row(html.escape(website), add_stats(tree[website]), bg="#E4E4FB", weight=700))
        for row in sorted(tree[website], key=lambda r: (r["provider"], r["merchant"], r["gateway_id"])):
            label = (html.escape(f"{row['provider']} - {row['merchant']}") + '<div style="font-size:10px;color:#999;">'
                     + html.escape(row["gateway_id"]) + '</div>')
            parts.append(unavailable_row(label, 10) if row["unavailable"]
                         else table_row(label, row["stats"], 10))
            if row["extras"] and not row["unavailable"]:
                parts.append(extras_row(row["extras"], 10))
    parts.append("</table>")
    return "\n".join(parts)


def build_charts_html(specs, pngs):
    parts = ['<h2 style="margin:28px 0 10px;font-size:16px;color:#333;'
             'border-top:2px solid #f0f0f0;padding-top:20px;">Breakdown</h2>']
    no_activity = []
    for spec in specs:
        if spec["id"] in pngs:
            png, w, h = pngs[spec["id"]]
            parts.append('<div style="margin-bottom:14px;">'
                         + img_tag("cid:" + chart_cid(spec), w, h, spec["title"], display_w=560) + '</div>')
        else:
            no_activity.append(spec["title"])
    if no_activity:
        parts.append('<p style="font-size:11px;color:#aaa;margin:6px 0 0;">No activity in this period: '
                     + ", ".join(no_activity) + '</p>')
    return "\n".join(parts)


def build_email_html(report_date, days, boxes, table_html, charts_html, notes, failed):
    ts_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
    notes_html = "".join(f'<p style="margin:16px 0 0;font-size:11px;color:#888;">{n}</p>' for n in notes)
    period = "Daily report" if days == 1 else f"{days} days"
    warning = ""
    if failed:
        warning = ('<div style="background:#fdecea;border:1px solid #f5c6c2;border-radius:8px;padding:12px 14px;'
                   'margin-bottom:20px;font-size:12px;color:#a12a1f;"><b>Totals are incomplete.</b> '
                   'These gateway logins could not be read: ' + html.escape(", ".join(failed)) + '.</div>')

    lines = [
        "<!DOCTYPE html>",
        '<html xmlns:v="urn:schemas-microsoft-com:vml">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>IMT NMI Report | " + report_date + "</title>",
        "</head>",
        '<body style="margin:0;padding:0;background:#F0F2F5;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;">',
        '<div style="max-width:700px;margin:32px auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);">',
        '<!--[if gte mso 9]><v:rect xmlns:v="urn:schemas-microsoft-com:vml" fill="true" stroke="false" style="mso-width-percent:1000;"><v:fill type="gradient" color="#8B8CF6" color2="#4B45C6" angle="135"/><v:textbox style="mso-fit-shape-to-text:true" inset="0,0,0,0"><![endif]-->',
        '<div style="background-color:#8B8CF6;background:linear-gradient(135deg,#8B8CF6 0%,#4B45C6 100%);padding:28px 32px;">',
        '<div style="color:#fff;font-size:20px;font-weight:700;">IMT NMI Report | ' + report_date + '</div>',
        "</div>",
        '<!--[if gte mso 9]></v:textbox></v:rect><![endif]-->',
        '<div style="padding:24px 32px;">',
        warning,
        '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px;">',
        boxes,
        "</div>",
        table_html,
        charts_html,
        notes_html,
        '<p style="margin:32px 0 0;font-size:11px;color:#aaa;text-align:center;">',
        f"Generated {ts_str} &bull; IMT NMI Report &bull; {period}",
        "</p>",
        "</div>",
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(l for l in lines if l)


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(subject, html_body, to_addresses, images=None):
    """images: optional {cid: png_bytes} dict - each is attached as an inline
    MIMEImage part with a matching Content-ID, nested inside a multipart/related
    part alongside the HTML (mixed -> alternative -> related), so <img src="cid:...">
    references resolve reliably in every mail client including Windows desktop
    Outlook (which mishandles base64 data: URIs embedded directly in <img src>)."""
    sender = f"{FROM_NAME} <{FROM_ADDRESS}>"
    msg = email.mime.multipart.MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_addresses)

    body_part = email.mime.multipart.MIMEMultipart("alternative")
    if images:
        related = email.mime.multipart.MIMEMultipart("related")
        related.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
        for cid, png_bytes in images.items():
            img = email.mime.image.MIMEImage(png_bytes, "png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            related.attach(img)
        body_part.attach(related)
    else:
        body_part.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
    msg.attach(body_part)

    ses.send_email(
        FromEmailAddress=sender,
        Destination={"ToAddresses": to_addresses},
        Content={"Raw": {"Data": msg.as_bytes()}},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    """Also resolves the report period into args.start_dt (midnight, TIMEZONE)
    and an exclusive args.end_dt."""
    p = argparse.ArgumentParser(description="NMI transaction report emailed via AWS SES")
    period = p.add_mutually_exclusive_group()
    period.add_argument("--days", type=int, default=None,
                        help="Last N full days ending yesterday (overrides REPORT_DAYS)")
    period.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                        help="Report a single day")
    period.add_argument("--range", nargs=2, metavar=("START", "END"), default=None,
                        help="Report an inclusive date range")
    p.add_argument("--preview", action="store_true", help="Save the report locally instead of emailing")
    p.add_argument("--to", nargs="+", default=None, help="Override recipients for this run")
    p.add_argument("--debug", action="store_true", help="Print a redacted sample transaction per gateway login")
    p.add_argument("--no-save", action="store_true",
                   help="Don't write transaction IDs to the gateway_transactions table")
    args = p.parse_args()

    def midnight(s):
        try:
            d = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            p.error(f"invalid date {s!r} - use YYYY-MM-DD")
        return datetime(d.year, d.month, d.day, tzinfo=TIMEZONE)

    today = datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.date or args.range:
        start_s, end_s = (args.date, args.date) if args.date else args.range
        args.start_dt, args.end_dt = midnight(start_s), midnight(end_s) + timedelta(days=1)
        if args.end_dt <= args.start_dt:
            p.error("--range END is before START")
        if args.end_dt > today:
            p.error("report dates must be before today")
    else:
        days = args.days or REPORT_DAYS
        if days < 1:
            p.error("--days must be at least 1")
        args.end_dt = today
        args.start_dt = today - timedelta(days=days)
    return args


def build_cards(totals):
    """(label, value, detail) per summary card."""
    auth_detail = (f"{plural(totals['auths'], 'auth')} &bull; {fmt_int(totals['captured'])} captured "
                   f"({fmt_money(totals['captured_amt'])})")

    def with_voids(detail, voided):
        return detail + (f" &bull; {fmt_int(voided)} since voided" if voided else "")

    return [
        ("Auths", fmt_money(totals["auths_amt"]), auth_detail),
        ("Pending Auths", fmt_money(totals["pending_amt"]), plural(totals["pending"], "pending auth")),
        ("Captures", fmt_money(totals["captures_amt"]),
         with_voids(plural(totals["captures"], "capture"), totals["captures_voided"])),
        ("Voids", fmt_money(totals["released_amt"]), plural(totals["released"], "released auth")),
        ("Refunds", fmt_money(totals["refunds_amt"]), plural(totals["refunds"], "refund")),
    ]


def main():
    global DEBUG
    args = parse_args()
    DEBUG = args.debug

    to_addresses = args.to or TO_ADDRESSES

    start_dt, end_dt = args.start_dt, args.end_dt
    days = (end_dt.date() - start_dt.date()).days
    if days == 1:
        report_date = start_dt.strftime("%b %d, %Y")
    else:
        report_date = start_dt.strftime("%b %d") + " - " + (end_dt - timedelta(days=1)).strftime("%b %d, %Y")

    print("Loading NMI accounts from the database ...")
    try:
        logins = load_logins()
    except pyodbc.Error as e:
        raise SystemExit(f"Could not read usr_processors from {DB_SERVER}/{DB_DATABASE}: {e.args[-1]}")
    if not logins:
        raise SystemExit("No usr_processors rows with cc_gateway='N' - nothing to report.")
    print(f"{len(logins)} gateway logins")

    print(f"Fetching NMI transactions ({days}-day window) ...")
    notes = []
    breakdowns = {"website_volume": Counter(), "volume": Counter(), "brands": Counter(), "declines": Counter(),
                  "decline_labels": {}}
    failed = collect(logins, start_dt, end_dt, breakdowns, notes)
    if len(failed) == len(logins):
        raise SystemExit("Every gateway login failed - not sending a report.")

    if not args.no_save:
        try:
            print(f"Saved {save_transactions(logins, start_dt, end_dt):,} transaction IDs to {TXN_TABLE}")
        except pyodbc.Error as e:
            print(f"Could not save transaction IDs to {TXN_TABLE}: {e.args[-1]}")
            notes.append(f"Transaction IDs could not be saved to {TXN_TABLE} for reconciliation.")

    print("Building charts ...")
    specs = [
        bar_spec("Volume by Website", breakdowns["website_volume"], "money", labeler=lambda k: k),
        bar_spec("Volume by Merchant", breakdowns["volume"], "money", labeler=lambda k: k),
        bar_spec("Card Brands", breakdowns["brands"]),
        bar_spec("Decline Reasons", breakdowns["declines"], labeler=breakdowns["decline_labels"].get),
    ]
    pngs = {s["id"]: make_bar_chart(s) for s in specs if s["rows"]}
    email_images = {chart_cid(s): pngs[s["id"]][0] for s in specs if s["id"] in pngs}

    totals = add_stats(r for login in logins for r in login["rows"])
    if totals["sales"]:  # no Sales column, so don't let any go unseen
        notes.append(f"Not shown in the table: {plural(totals['sales'], 'sale')} totalling "
                     f"{fmt_money(totals['sales_amt'])} (auth and capture in one step), included in the volume charts.")
    boxes = "".join(stat_box(label, value, PALETTE[i % len(PALETTE)], detail)
                    for i, (label, value, detail) in enumerate(build_cards(totals)))

    print("Composing email ...")
    html_body = build_email_html(report_date, days, boxes, build_table(logins),
                                 build_charts_html(specs, pngs), notes, failed)
    subject = "IMT NMI Report | " + report_date + (" (incomplete)" if failed else "")
    file_name = "IMT_NMI_" + (end_dt - timedelta(days=1)).strftime("%Y%m%d") + "_email.html"

    if args.preview or not FROM_ADDRESS or not to_addresses:
        if not (FROM_ADDRESS and to_addresses) and not args.preview:
            print("FROM_ADDRESS or TO_ADDRESSES not set in .env - saving locally instead of sending.")
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(inline_images_for_preview(html_body, email_images))
        print(f"Saved email body preview to {file_name}")
    else:
        print(f"Sending to {', '.join(to_addresses)} ...")
        send_email(subject, html_body, to_addresses, images=email_images)
        print("Done! Report sent successfully.")


if __name__ == "__main__":
    main()
