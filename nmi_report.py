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
from zoneinfo import ZoneInfo
import email.mime.image
import email.mime.multipart
import email.mime.text

import boto3
import pyodbc
import requests
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
    select p.agentid, m.FirstName merchant, p.site_agentid, s.FirstName website,
           p.company_name, p.gateway_id, p.cc_gateway_username, p.cc_gateway_password
    from usr_processors p with (nolock)
    left join usr_agents m with (nolock) on p.agentid = m.AgentId
    left join usr_agents s with (nolock) on p.site_agentid = s.AgentId
    where p.cc_gateway = 'N'
"""


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

def load_logins():
    """One entry per distinct gateway login, each with its usr_processors rows.
    Rows can share a login (660's Flex row does), so querying per row would
    double-count that login's transactions."""
    if DB_TRUSTED_CONNECTION:
        auth = "Trusted_Connection=yes"
    else:
        auth = f"UID={DB_USERNAME};PWD={DB_PASSWORD}"
    conn = pyodbc.connect(f"DRIVER={DB_DRIVER};SERVER={DB_SERVER};DATABASE={DB_DATABASE};{auth}", timeout=15)
    try:
        rows = conn.cursor().execute(ACCOUNTS_SQL).fetchall()
    finally:
        conn.close()

    logins = {}
    for r in rows:
        agentid, site_agentid = (r.agentid or "").strip(), (r.site_agentid or "").strip()
        key = (r.cc_gateway_username, r.cc_gateway_password)
        login = logins.setdefault(key, {
            "username": r.cc_gateway_username, "password": r.cc_gateway_password,
            "url": NMI_URL_OVERRIDES.get(agentid, NMI_QUERY_URL), "rows": [],
        })
        login["rows"].append({
            "website": (r.website or site_agentid or "Unknown").strip(),
            "merchant": (r.merchant or agentid or "Unknown").strip(),
            "provider": (r.company_name or "Unknown").strip(),
            "gateway_id": (r.gateway_id or "").strip(),
            "stats": Counter(),
            "unavailable": False,
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
        elif kind == "capture":
            stats["captures"] += 1
            stats["captures_amt"] += amt
            stats["captures_voided"] += voided
            breakdowns["website_volume"][row["website"]] += amt
            breakdowns["volume"][row["merchant"]] += amt
        elif kind == "sale":
            stats["sales"] += 1
            stats["sales_amt"] += amt
            stats["sales_voided"] += voided
            breakdowns["website_volume"][row["website"]] += amt
            breakdowns["volume"][row["merchant"]] += amt
            breakdowns["brands"][tx["cc_type"] or None] += 1
        elif kind in ("refund", "credit"):
            stats["refunds"] += 1
            stats["refunds_amt"] += abs(amt)

    for a in tx["actions"]:
        if not a["success"] and a["type"] in ("auth", "sale") and in_window(a):
            reason = re.sub(r"\s*REFID:\S+", "", a["response_text"]).strip()
            breakdowns["declines"][reason or "Unknown"] += 1


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
    ("Auths", "auths", "auths_amt"), ("Captured", "captured", "captured_amt"), ("Pending", "pending", "pending_amt"),
    ("Captures", "captures", "captures_amt"), ("Sales", "sales", "sales_amt"), ("Refunds", "refunds", "refunds_amt"),
]


def add_stats(rows):
    total = Counter()
    for row in rows:
        total.update(row["stats"])
    return total


def table_row(label, stats, indent=0, bg="#fff", weight=400):
    td = f'padding:6px 5px;border-bottom:1px solid #eee;font-size:11px;font-weight:{weight};'
    cells = [f'<td style="{td}padding-left:{5 + indent}px;">{label}</td>']
    for _, count, amount in COLUMNS:
        if stats[count]:
            cells.append(f'<td align="right" style="{td}">{fmt_money(stats[amount])}'
                         f'<div style="font-size:9px;color:#999;font-weight:400;">{fmt_int(stats[count])}</div></td>')
        else:
            cells.append(f'<td align="right" style="{td}color:#ccc;">&mdash;</td>')
    return f'<tr style="background:{bg};">' + "".join(cells) + "</tr>"


def unavailable_row(label, indent):
    td = 'padding:6px 5px;border-bottom:1px solid #eee;font-size:11px;'
    return (f'<tr style="background:#fff5f5;"><td style="{td}padding-left:{5 + indent}px;">{label}</td>'
            f'<td colspan="{len(COLUMNS)}" align="center" style="{td}color:#c0392b;font-weight:600;">Unavailable</td></tr>')


def build_table(logins):
    """Website > merchant > provider, with subtotals on the website and merchant rows."""
    tree = defaultdict(lambda: defaultdict(list))
    for login in logins:
        for row in login["rows"]:
            tree[row["website"]][row["merchant"]].append(row)

    head = "".join(f'<th align="right" style="padding:6px 5px;">{c[0]}</th>' for c in COLUMNS)
    parts = ['<table width="100%" cellpadding="0" cellspacing="0" border="0" '
             'style="border-collapse:collapse;margin-bottom:24px;color:#333;">',
             '<tr style="background:#4B45C6;color:#fff;font-size:11px;">'
             '<th align="left" style="padding:6px 5px;">Website / Merchant / Provider</th>' + head + '</tr>']
    for website in sorted(tree):
        merchants = tree[website]
        parts.append(table_row(html.escape(website), add_stats(r for ms in merchants.values() for r in ms),
                               bg="#E4E4FB", weight=700))
        for merchant in sorted(merchants):
            parts.append(table_row(html.escape(merchant), add_stats(merchants[merchant]), 10, "#F5F5FD", 600))
            for row in sorted(merchants[merchant], key=lambda r: (r["provider"], r["gateway_id"])):
                label = (html.escape(row["provider"]) + '<div style="font-size:9px;color:#999;">'
                         + html.escape(row["gateway_id"]) + '</div>')
                parts.append(unavailable_row(label, 20) if row["unavailable"]
                             else table_row(label, row["stats"], 20))
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
                   f"({fmt_money(totals['captured_amt'])}) &bull; {fmt_int(totals['pending'])} pending "
                   f"({fmt_money(totals['pending_amt'])})")
    if totals["released"]:
        auth_detail += f" &bull; {fmt_int(totals['released'])} released"

    def with_voids(detail, voided):
        return detail + (f" &bull; {fmt_int(voided)} since voided" if voided else "")

    return [
        ("Auths", fmt_money(totals["auths_amt"]), auth_detail),
        ("Captures", fmt_money(totals["captures_amt"]),
         with_voids(plural(totals["captures"], "capture"), totals["captures_voided"])),
        ("Sales", fmt_money(totals["sales_amt"]),
         with_voids(plural(totals["sales"], "sale"), totals["sales_voided"])),
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
    breakdowns = {"website_volume": Counter(), "volume": Counter(), "brands": Counter(), "declines": Counter()}
    failed = collect(logins, start_dt, end_dt, breakdowns, notes)
    if len(failed) == len(logins):
        raise SystemExit("Every gateway login failed - not sending a report.")

    print("Building charts ...")
    specs = [
        bar_spec("Volume by Website", breakdowns["website_volume"], "money", labeler=lambda k: k),
        bar_spec("Volume by Merchant", breakdowns["volume"], "money", labeler=lambda k: k),
        bar_spec("Card Brands", breakdowns["brands"]),
        bar_spec("Decline Reasons", breakdowns["declines"], labeler=lambda k: k),
    ]
    pngs = {s["id"]: make_bar_chart(s) for s in specs if s["rows"]}
    email_images = {chart_cid(s): pngs[s["id"]][0] for s in specs if s["id"] in pngs}

    totals = add_stats(r for login in logins for r in login["rows"])
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
