#!/usr/bin/env python3
"""
Career-page poller: watches company ATS endpoints and pushes new matching roles.

Stdlib only. No pip install required.

Usage:
    python3 poller.py --verify          # test every config entry, report which resolve
    python3 poller.py --seed            # record current jobs WITHOUT notifying (run this first)
    python3 poller.py --once            # one polling pass, notify on new matches
    python3 poller.py --loop            # poll forever at CHECK_INTERVAL
    python3 poller.py --digest          # one pass, email a single grouped digest, exit
    python3 poller.py --health          # exit 1 if >30% of enabled companies are erroring
    python3 poller.py --selftest        # run offline logic tests, no network needed

Environment:
    SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO
                          email delivery for --digest. All four required together.
    SMTP_PORT             optional, defaults to 587 (587 = STARTTLS, 465 = implicit TLS)
    EMAIL_FROM            optional, defaults to SMTP_USER
    TELEGRAM_BOT_TOKEN    optional fallback, used when SMTP is not configured
    TELEGRAM_CHAT_ID      optional

With nothing configured, matches print to stdout and append to matches.jsonl.
--digest degrades to that rather than crashing, so a missing secret costs you a
notification, not the run.
"""

import argparse
import html
import json
import os
import re
import smtplib
import socket
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "companies.json"
DB_PATH = HERE / "seen.db"
MATCHES_LOG = HERE / "matches.jsonl"
RUNS_LOG = HERE / "runs.log"

USER_AGENT = "job-poller/1.0 (personal job search; contact via github)"
TIMEOUT = 20
POLITE_DELAY = 1.0      # seconds between companies; be a good citizen
CHECK_INTERVAL = 600    # 10 minutes

HTTP_RETRIES = 2        # retries *after* the first attempt
HTTP_BACKOFF = 1.5      # seconds before the first retry; doubles each time
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

SMTP_TIMEOUT = 30       # a hung SMTP server must not eat the Actions budget
SMTP_RETRY_DELAY = 5

ERROR_ALERT_THRESHOLD = 3     # this many failing companies earns an email of its own
HEALTH_MAX_ERROR_RATE = 0.30  # --health exits nonzero above this
SUBJECT_MAX_COMPANIES = 3     # names in the subject line before "+N"


# ─────────────────────────────────────────────────────────────────────────────
# Model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    company: str
    ats: str
    ats_id: str
    title: str
    location: str
    url: str
    posted_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.company}:{self.ats}:{self.ats_id}"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

def _open_json(req):
    """One HTTP attempt. Split out so the retry loop stays readable and stubbable."""
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _retry_after(err):
    """Honour a numeric Retry-After, capped — we're inside a 20-minute run budget."""
    headers = getattr(err, "headers", None)
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return min(float(value), 30.0)
    except (TypeError, ValueError):
        return None          # HTTP-date form; not worth parsing here


def http_json(url, method="GET", body=None, extra_headers=None, retries=None):
    """
    Fetch JSON. Raises urllib.error.* on final failure; caller decides how loud to be.

    Retries 5xx, 429 and connection/read timeouts — the failures that clear on
    their own while nobody is watching. A 404 or 422 means the token is wrong, and
    retrying that just hammers an endpoint published as a courtesy.
    """
    if retries is None:
        retries = HTTP_RETRIES

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    attempt = 0
    while True:
        try:
            return _open_json(req)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt >= retries:
                raise
            delay = _retry_after(e) or HTTP_BACKOFF * (2 ** attempt)
        except (urllib.error.URLError, socket.timeout, TimeoutError):
            # HTTPError subclasses URLError, so it has to be caught above this.
            if attempt >= retries:
                raise
            delay = HTTP_BACKOFF * (2 ** attempt)
        attempt += 1
        time.sleep(delay)


# ─────────────────────────────────────────────────────────────────────────────
# ATS adapters
#
# Each adapter takes the config entry and returns list[Job].
# Adding a new ATS = write one function + register it in ADAPTERS.
# ─────────────────────────────────────────────────────────────────────────────

def fetch_greenhouse(entry):
    """Greenhouse public board API. `token` is the board slug in the careers URL."""
    token = entry["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    payload = http_json(url)
    jobs = []
    for j in payload.get("jobs", []):
        jobs.append(Job(
            company=entry["company"],
            ats="greenhouse",
            ats_id=str(j.get("id", "")),
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            posted_at=j.get("updated_at", "") or "",
        ))
    return jobs


def fetch_lever(entry):
    """Lever public postings API."""
    token = entry["token"]
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    payload = http_json(url)
    jobs = []
    for j in payload:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        jobs.append(Job(
            company=entry["company"],
            ats="lever",
            ats_id=str(j.get("id", "")),
            title=j.get("text", ""),
            location=cats.get("location", "") or "",
            url=j.get("hostedUrl", ""),
            posted_at=_ms_to_iso(created),
        ))
    return jobs


def fetch_ashby(entry):
    """Ashby public job board API."""
    token = entry["token"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    payload = http_json(url)
    jobs = []
    for j in payload.get("jobs", []):
        jobs.append(Job(
            company=entry["company"],
            ats="ashby",
            ats_id=str(j.get("id", "")),
            title=j.get("title", ""),
            location=j.get("location", "") or "",
            url=j.get("jobUrl", ""),
            posted_at=j.get("publishedAt", "") or "",
        ))
    return jobs


def fetch_smartrecruiters(entry):
    """SmartRecruiters public postings API. `token` is the company identifier."""
    token = entry["token"]
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    payload = http_json(url)
    jobs = []
    for j in payload.get("content", []):
        loc = j.get("location") or {}
        loc_str = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        jobs.append(Job(
            company=entry["company"],
            ats="smartrecruiters",
            ats_id=str(j.get("id", "")),
            title=j.get("name", ""),
            location=loc_str,
            url=f"https://jobs.smartrecruiters.com/{token}/{j.get('id','')}",
            posted_at=j.get("releasedDate", "") or "",
        ))
    return jobs


def fetch_workday(entry):
    """
    Workday CXS API. This is the one most pollers skip.

    Config needs:
        host:   e.g. "capitalone.wd1.myworkdayjobs.com"
        tenant: e.g. "capitalone"
        site:   e.g. "Capital_One"   (the path segment in the careers URL)

    Workday paginates hard, so we walk offsets until we run dry or hit max_pages.
    """
    host, tenant, site = entry["host"], entry["tenant"], entry["site"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    search = entry.get("search_text", "")
    max_pages = int(entry.get("max_pages", 5))

    jobs, offset, limit = [], 0, 20
    for _ in range(max_pages):
        body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search}
        payload = http_json(url, method="POST", body=body)
        postings = payload.get("jobPostings", [])
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath", "")
            jobs.append(Job(
                company=entry["company"],
                ats="workday",
                ats_id=str(j.get("bulletFields", [path])[0] if j.get("bulletFields") else path),
                title=j.get("title", ""),
                location=j.get("locationsText", "") or "",
                url=f"https://{host}{path}" if path.startswith("/") else path,
                posted_at=j.get("postedOn", "") or "",
            ))
        offset += limit
        if offset >= payload.get("total", 0):
            break
        time.sleep(0.3)
    return jobs


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def _ms_to_iso(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Filtering
# ─────────────────────────────────────────────────────────────────────────────

class Filters:
    def __init__(self, cfg):
        f = cfg.get("filters", {})
        self.title_include = _compile(f.get("title_include", []))
        self.title_exclude = _compile(f.get("title_exclude", []))
        self.location_include = _compile(f.get("location_include", []))
        self.location_exclude = _compile(f.get("location_exclude", []))

    def matches(self, job: Job) -> bool:
        title, loc = job.title or "", job.location or ""

        if self.title_include and not _any_match(self.title_include, title):
            return False
        if _any_match(self.title_exclude, title):
            return False
        if self.location_exclude and _any_match(self.location_exclude, loc):
            return False
        # An empty location field is common and usually means "see posting".
        # Don't silently drop those — surface them and let the human judge.
        if self.location_include and loc.strip():
            if not _any_match(self.location_include, loc):
                return False
        return True


def _compile(patterns):
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _any_match(patterns, text):
    return any(p.search(text) for p in patterns)


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            key        TEXT PRIMARY KEY,
            company    TEXT,
            title      TEXT,
            location   TEXT,
            url        TEXT,
            first_seen TEXT,
            notified   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def is_new(conn, key) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone() is None


def db_is_seeded(conn) -> bool:
    """
    Has this database ever been seeded?

    db_connect() happily creates an empty database, so "no rows" is
    indistinguishable from "every posting on earth is new". Unattended, that
    difference is thousands of emails, so --digest checks before it sends.
    """
    return conn.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is not None


def record(conn, job: Job, notified: bool):
    conn.execute(
        "INSERT OR IGNORE INTO seen (key, company, title, location, url, first_seen, notified) "
        "VALUES (?,?,?,?,?,?,?)",
        (job.key, job.company, job.title, job.location, job.url,
         datetime.now(timezone.utc).isoformat(), 1 if notified else 0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Notification
# ─────────────────────────────────────────────────────────────────────────────

def log_matches(jobs):
    """Append every match to matches.jsonl. Durable regardless of delivery channel."""
    if not jobs:
        return
    with MATCHES_LOG.open("a") as fh:
        for j in jobs:
            fh.write(json.dumps(asdict(j)) + "\n")


def notify(jobs):
    """Push to Telegram if configured; always append to matches.jsonl and print."""
    if not jobs:
        return

    log_matches(jobs)

    for j in jobs:
        print(f"  NEW  {j.company:<22} {j.title[:60]:<60} {j.location[:30]}")
        print(f"       {j.url}")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return

    # Batch into chunks so one message per ~10 jobs, not per job.
    for chunk in _chunks(jobs, 10):
        lines = []
        for j in chunk:
            loc = f" — {j.location}" if j.location else ""
            lines.append(f"<b>{_esc(j.company)}</b>: {_esc(j.title)}{_esc(loc)}\n{j.url}")
        text = "\n\n".join(lines)
        try:
            http_json(
                f"https://api.telegram.org/bot{token}/sendMessage",
                method="POST",
                body={"chat_id": chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
            )
        except Exception as e:                                   # noqa: BLE001
            print(f"  [warn] telegram send failed: {e}", file=sys.stderr)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─────────────────────────────────────────────────────────────────────────────
# Email digest
#
# Everything here is pure except send_email() and the SMTP conversation it wraps,
# so --selftest can exercise grouping, subjects, escaping and the send/no-send
# decision without a network or an inbox.
# ─────────────────────────────────────────────────────────────────────────────

def _utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def group_by_company(jobs):
    """Group jobs by company: biggest group first, titles sorted within a group."""
    groups = {}
    for j in jobs:
        groups.setdefault(j.company, []).append(j)
    for items in groups.values():
        items.sort(key=lambda j: ((j.title or "").lower(), (j.location or "").lower()))
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())))


def digest_subject(jobs, max_companies=SUBJECT_MAX_COMPANIES):
    """
    "12 new roles — Anthropic, Stripe, Capital One +3"

    On a phone the subject line is the entire notification, so it leads with the
    count and names the companies that contributed most.
    """
    n = len(jobs)
    if not n:
        return "Job poller: no new roles"
    names = list(group_by_company(jobs))
    shown = names[:max_companies]
    hidden = len(names) - len(shown)
    tail = ", ".join(shown)
    if hidden > 0:
        tail += " +%d" % hidden
    return "%d new role%s — %s" % (n, "" if n == 1 else "s", tail)


def error_subject(errors):
    n = len(errors)
    return "Job poller: %s failing" % _companies(n)


def decide_email(n_matches, n_errors, threshold=ERROR_ALERT_THRESHOLD):
    """
    What to send, if anything: "digest", "errors", or "none".

    Two deliberate rules:

    Zero matches sends nothing. A daily "nothing today" message teaches you to
    filter the whole thread away, and then the morning it matters you don't look.

    Widespread failure sends something even with zero matches, because a poller
    whose tokens have rotted looks exactly like a quiet job market from the inbox.

    At most one email per run: when there are matches *and* failures, the digest
    carries the failure banner rather than arriving as a second message.
    """
    if n_matches > 0:
        return "digest"
    if n_errors >= threshold:
        return "errors"
    return "none"


def _safe_url(url):
    """Only http(s) becomes a clickable link — these URLs come from third parties."""
    u = (url or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def _plural(n, word, suffix="s"):
    return "%d %s%s" % (n, word, "" if n == 1 else suffix)


def _companies(n):
    return "%d compan%s" % (n, "y" if n == 1 else "ies")


# Inline styles only: Gmail strips <style> blocks, and an external stylesheet or
# image would break the moment the network it points at does.
_S_WRAP = ("font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,"
           "Arial,sans-serif;font-size:16px;line-height:1.45;color:#1a1a1a;"
           "max-width:640px;margin:0 auto;padding:16px;")
_S_H1 = "font-size:18px;font-weight:600;margin:0 0 4px;"
_S_SUB = "font-size:13px;color:#666;margin:0 0 20px;"
_S_ALERT = ("background:#fff4f4;border-left:4px solid #cc3333;padding:10px 12px;"
            "margin:0 0 20px;font-size:14px;color:#7a1a1a;")
_S_CO = ("font-size:15px;font-weight:600;margin:24px 0 4px;padding-bottom:6px;"
         "border-bottom:1px solid #e0e0e0;")
_S_ROW = "padding:10px 0;border-bottom:1px solid #f2f2f2;"
_S_LINK = "color:#1155cc;text-decoration:none;font-weight:500;"
_S_LOC = "font-size:13px;color:#666;margin-top:2px;"
_S_FOOT = ("font-size:12px;color:#888;margin-top:28px;border-top:1px solid #e0e0e0;"
           "padding-top:12px;")


def render_text(jobs, errors, polled):
    """Plain-text part. Deliberately not escaped — this is text/plain."""
    groups = group_by_company(jobs)
    n_co = len(groups)
    out = ["%s across %d compan%s" % (_plural(len(jobs), "new role"),
                                      n_co, "y" if n_co == 1 else "ies"),
           _utc_stamp(), ""]
    if len(errors) >= ERROR_ALERT_THRESHOLD:
        out += ["!! %s failed to poll — listed at the bottom." % _companies(len(errors)), ""]
    for company, items in groups.items():
        out.append("%s (%d)" % (company.upper(), len(items)))
        for j in items:
            out.append("  %s" % j.title)
            if j.location:
                out.append("  %s" % j.location)
            out.append("  %s" % j.url)
            out.append("")
    out.append("-" * 56)
    out.append("Polled %d companies, %s." % (polled, _plural(len(errors), "error")))
    for name, why in errors:
        out.append("  %s: %s" % (name, why))
    return "\n".join(out) + "\n"


def render_html(jobs, errors, polled):
    """HTML part: grouped by company, one tappable row per role."""
    esc = html.escape
    groups = group_by_company(jobs)
    n_co = len(groups)
    parts = ['<div style="%s">' % _S_WRAP]
    parts.append('<p style="%s">%s across %d compan%s</p>'
                 % (_S_H1, _plural(len(jobs), "new role"), n_co, "y" if n_co == 1 else "ies"))
    parts.append('<p style="%s">%s</p>' % (_S_SUB, esc(_utc_stamp())))

    if len(errors) >= ERROR_ALERT_THRESHOLD:
        parts.append('<p style="%s"><strong>%s failed to poll.</strong> '
                     'Listed at the bottom — a wrong token stays wrong until you fix it.</p>'
                     % (_S_ALERT, _companies(len(errors))))

    for company, items in groups.items():
        parts.append('<div style="%s">%s <span style="font-weight:400;color:#888;">(%d)</span></div>'
                     % (_S_CO, esc(company), len(items)))
        for j in items:
            parts.append('<div style="%s">' % _S_ROW)
            url = _safe_url(j.url)
            if url:
                parts.append('<a href="%s" style="%s">%s</a>'
                             % (esc(url), _S_LINK, esc(j.title or "(untitled)")))
            else:
                parts.append('<span style="%s">%s</span>'
                             % (_S_LINK, esc(j.title or "(untitled)")))
            if j.location:
                parts.append('<div style="%s">%s</div>' % (_S_LOC, esc(j.location)))
            parts.append('</div>')

    parts.append('<div style="%s">Polled %d companies &middot; %s'
                 % (_S_FOOT, polled, _plural(len(errors), "error")))
    if errors:
        parts.append('<br>' + '<br>'.join('%s: %s' % (esc(n), esc(w)) for n, w in errors))
    parts.append('</div></div>')
    return "\n".join(parts)


def render_error_text(errors, polled):
    out = ["%s failed to poll out of %d." % (_companies(len(errors)), polled),
           _utc_stamp(), "",
           "No new roles were found this run, but that result is not trustworthy",
           "while this many endpoints are down.", ""]
    for name, why in errors:
        out.append("  %s: %s" % (name, why))
    out += ["", "Fix tokens with:  python3 discover.py --fix-config",
            "Then re-check with: python3 poller.py --health"]
    return "\n".join(out) + "\n"


def render_error_html(errors, polled):
    esc = html.escape
    parts = ['<div style="%s">' % _S_WRAP,
             '<p style="%s">%s failed to poll out of %d</p>'
             % (_S_H1, _companies(len(errors)), polled),
             '<p style="%s">%s</p>' % (_S_SUB, esc(_utc_stamp())),
             '<p style="%s">No new roles were found this run, but that result is not '
             'trustworthy while this many endpoints are down.</p>' % _S_ALERT]
    for name, why in errors:
        parts.append('<div style="%s"><strong>%s</strong><div style="%s">%s</div></div>'
                     % (_S_ROW, esc(name), _S_LOC, esc(why)))
    parts.append('<div style="%s">Fix tokens with <code>python3 discover.py --fix-config</code>, '
                 'then re-check with <code>python3 poller.py --health</code>.</div></div>' % _S_FOOT)
    return "\n".join(parts)


def smtp_settings(env=None):
    """
    Read SMTP config from the environment.

    Returns (settings, missing). settings is None when anything required is absent
    so callers can degrade and say so — an unattended job that dies on a missing
    variable is strictly worse than one that falls back and logs why.
    """
    env = os.environ if env is None else env
    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO")
    missing = [k for k in required if not (env.get(k) or "").strip()]
    if missing:
        return None, missing

    raw_port = (env.get("SMTP_PORT") or "").strip() or "587"
    try:
        port = int(raw_port)
    except ValueError:
        return None, ["SMTP_PORT (not a number: %r)" % raw_port]

    recipients = [a.strip() for a in env["EMAIL_TO"].split(",") if a.strip()]
    if not recipients:
        return None, ["EMAIL_TO (no addresses)"]

    return {
        "host": env["SMTP_HOST"].strip(),
        "port": port,
        "user": env["SMTP_USER"].strip(),
        "password": env["SMTP_PASS"],
        "to": recipients,
        "sender": (env.get("EMAIL_FROM") or env["SMTP_USER"]).strip(),
    }, []


def build_message(settings, subject, text_body, html_body):
    """multipart/alternative: plain text first, HTML as the richer alternative."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings["sender"]
    msg["To"] = ", ".join(settings["to"])
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _smtp_reason(err):
    detail = getattr(err, "smtp_error", b"")
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", errors="replace")
    return detail or str(err)


def _smtp_send(settings, msg):
    host, port = settings["host"], settings["port"]
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    try:
        server.ehlo()
        if port != 465:
            server.starttls(context=context)
            server.ehlo()
        server.login(settings["user"], settings["password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:                                # noqa: BLE001
            pass          # a dead socket on close is not a delivery failure


def send_email(settings, subject, text_body, html_body, attempts=2):
    """
    Send one multipart message. Returns True on success, never raises.

    The caller needs a boolean, not a traceback: whether delivery happened decides
    whether these jobs get marked as notified.
    """
    msg = build_message(settings, subject, text_body, html_body)
    last = None
    for attempt in range(attempts):
        try:
            _smtp_send(settings, msg)
            print("  email sent to %s — %s" % (", ".join(settings["to"]), subject))
            return True
        except smtplib.SMTPAuthenticationError as e:
            # Retrying rejected credentials only locks the account faster.
            print("  [error] SMTP auth rejected: %s" % _smtp_reason(e), file=sys.stderr)
            print("  [error] Gmail requires an App Password here — a normal account "
                  "password always fails. See README, 'Email delivery'.", file=sys.stderr)
            return False
        except (smtplib.SMTPException, OSError) as e:
            last = e
            if attempt + 1 < attempts:
                print("  [warn] SMTP send failed (%s: %s) — retrying in %ds"
                      % (type(e).__name__, e, SMTP_RETRY_DELAY), file=sys.stderr)
                time.sleep(SMTP_RETRY_DELAY)
    print("  [error] SMTP send failed after %d attempts: %s: %s"
          % (attempts, type(last).__name__, last), file=sys.stderr)
    return False


def deliver_digest(jobs, errors, polled):
    """
    Deliver this run's single notification.

    Returns True when the operator has been told (or there was nothing to tell),
    False only when a configured email channel failed. cmd_digest uses that
    boolean to decide whether these jobs may be marked as seen.
    """
    kind = decide_email(len(jobs), len(errors))
    if kind == "none":
        print("  no new matches, %d error(s) — no email sent (by design)" % len(errors))
        return True

    if kind == "digest":
        subject = digest_subject(jobs)
        text_body = render_text(jobs, errors, polled)
        html_body = render_html(jobs, errors, polled)
    else:
        subject = error_subject(errors)
        text_body = render_error_text(errors, polled)
        html_body = render_error_html(errors, polled)

    settings, missing = smtp_settings()
    if settings is not None:
        ok = send_email(settings, subject, text_body, html_body)
        if ok and kind == "digest":
            log_matches(jobs)
        return ok

    print("  [info] SMTP not configured (missing: %s) — falling back." % ", ".join(missing))
    if kind == "errors":
        print(text_body)
        return True
    # Pre-existing path: Telegram if set, otherwise stdout + matches.jsonl.
    notify(jobs)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def load_config():
    with CONFIG_PATH.open() as fh:
        return json.load(fh)


def enabled_companies(cfg):
    return [e for e in cfg["companies"] if not e.get("disabled")]


def log_run(mode, polled, ok, errors, new, matched, emailed, delivered, seconds):
    """
    One line per run, appended to runs.log and committed alongside seen.db.

    This is the file to check when you're wondering whether the job is still
    alive. A missing line for yesterday means it never ran — which is exactly the
    failure no amount of inbox-watching would have shown you.
    """
    line = ("%s  mode=%-6s companies=%-3d ok=%-3d errors=%-3d new=%-4d matched=%-3d "
            "email=%-6s delivered=%-3s dur=%ds\n"
            % (_utc_stamp(), mode, polled, ok, errors, new, matched,
               emailed, "yes" if delivered else "NO", int(seconds)))
    try:
        with RUNS_LOG.open("a") as fh:
            fh.write(line)
    except OSError as e:
        print("  [warn] could not append to %s: %s" % (RUNS_LOG.name, e), file=sys.stderr)
    print("  " + line.strip())


def fetch_all(cfg, quiet=False):
    """Returns (jobs, errors). One bad company never kills the run."""
    jobs, errors = [], []
    for entry in enabled_companies(cfg):
        adapter = ADAPTERS.get(entry["ats"])
        if not adapter:
            errors.append((entry["company"], f"unknown ats '{entry['ats']}'"))
            continue
        try:
            found = adapter(entry)
            jobs.extend(found)
            if not quiet:
                print(f"  {entry['company']:<22} {len(found):>4} postings")
        except urllib.error.HTTPError as e:
            errors.append((entry["company"], f"HTTP {e.code}"))
        except Exception as e:                                   # noqa: BLE001
            errors.append((entry["company"], type(e).__name__ + ": " + str(e)[:80]))
        time.sleep(POLITE_DELAY)
    return jobs, errors


def cmd_verify(cfg):
    print("Verifying config entries against live endpoints...\n")
    ok, bad = [], []
    for entry in cfg["companies"]:
        if entry.get("disabled"):
            print(f"  SKIP  {entry['company']:<22} disabled ({entry.get('notes','')[:50]})")
            continue
        adapter = ADAPTERS.get(entry["ats"])
        name = entry["company"]
        if not adapter:
            bad.append((name, f"unknown ats '{entry['ats']}'"))
            continue
        try:
            found = adapter(entry)
            if found:
                ok.append((name, len(found)))
                print(f"  OK    {name:<22} {len(found):>4} postings  [{entry['ats']}]")
            else:
                bad.append((name, "resolved but returned 0 postings — wrong token?"))
                print(f"  WARN  {name:<22} 0 postings  [{entry['ats']}]")
        except urllib.error.HTTPError as e:
            bad.append((name, f"HTTP {e.code}"))
            print(f"  FAIL  {name:<22} HTTP {e.code}  [{entry['ats']}]")
        except Exception as e:                                   # noqa: BLE001
            bad.append((name, str(e)[:80]))
            print(f"  FAIL  {name:<22} {type(e).__name__}  [{entry['ats']}]")
        time.sleep(POLITE_DELAY)

    print(f"\n{len(ok)} working, {len(bad)} need fixing.")
    if bad:
        print("\nFix these before seeding — see README section 'Finding the right token'.")
        for name, why in bad:
            print(f"  - {name}: {why}")
    return 0 if not bad else 1


def cmd_seed(cfg):
    print("Seeding — recording current postings WITHOUT notifying.\n")
    conn = db_connect()
    jobs, errors = fetch_all(cfg)
    n = 0
    for j in jobs:
        if is_new(conn, j.key):
            record(conn, j, notified=False)
            n += 1
    conn.commit()
    print(f"\nSeeded {n} existing postings across {len(cfg['companies'])} companies.")
    _report_errors(errors)
    print("From now on, only postings that appear AFTER this moment will notify you.")
    return 0


def cmd_once(cfg, quiet=False):
    conn = db_connect()
    filters = Filters(cfg)
    if not quiet:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] polling...")
    jobs, errors = fetch_all(cfg, quiet=quiet)

    fresh = []
    for j in jobs:
        if not is_new(conn, j.key):
            continue
        matched = filters.matches(j)
        record(conn, j, notified=matched)
        if matched:
            fresh.append(j)
    conn.commit()

    if fresh:
        print(f"\n{len(fresh)} new matching role(s):")
        notify(fresh)
    elif not quiet:
        print("  no new matches")
    _report_errors(errors)
    return 0


def cmd_loop(cfg):
    print(f"Polling every {CHECK_INTERVAL}s. Ctrl-C to stop.\n")
    while True:
        try:
            cmd_once(cfg, quiet=True)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
        except Exception as e:                                   # noqa: BLE001
            print(f"[error] pass failed: {e}", file=sys.stderr)
        time.sleep(CHECK_INTERVAL)


def cmd_digest(cfg):
    """
    One pass, one grouped email, exit. Built for cron.

    The ordering here deliberately differs from --once: matching jobs are held out
    of seen.db until delivery is confirmed. A failed send therefore repeats
    tomorrow, which is mildly annoying. Recording first would instead drop roles
    you never saw, silently, forever. Duplicates over loss.
    """
    started = time.time()
    conn = db_connect()
    filters = Filters(cfg)
    polled = len(enabled_companies(cfg))

    print("[%s] digest run — polling %d companies" % (_utc_stamp(), polled))
    seeded = db_is_seeded(conn)
    jobs, errors = fetch_all(cfg, quiet=True)

    if not seeded:
        # An empty database means seen.db went missing — a bad checkout, a failed
        # push, a lost cache. Every open posting looks new, and emailing all of
        # them is the one failure this project exists to avoid. Seed and stay quiet.
        n = 0
        for j in jobs:
            if is_new(conn, j.key):
                record(conn, j, notified=False)
                n += 1
        conn.commit()
        print("  [warn] seen.db was empty — treating this as a seed run, NOT a digest.",
              file=sys.stderr)
        print("  [warn] recorded %d postings without notifying. The next run will "
              "email only genuinely new roles." % n, file=sys.stderr)
        _report_errors(errors)
        log_run("seed", polled=polled, ok=polled - len(errors), errors=len(errors),
                new=n, matched=0, emailed="none", delivered=True,
                seconds=time.time() - started)
        conn.close()
        return 0

    fresh, batch = [], set()
    for j in jobs:
        # `batch` guards against a board listing the same posting twice in one
        # response: without it the job is new both times and mails twice.
        if j.key in batch or not is_new(conn, j.key):
            continue
        batch.add(j.key)
        if filters.matches(j):
            fresh.append(j)                  # held back until delivery succeeds
        else:
            record(conn, j, notified=False)
    conn.commit()

    print("  %d postings fetched, %d new, %d matching, %d company error(s)"
          % (len(jobs), len(batch), len(fresh), len(errors)))

    delivered = deliver_digest(fresh, errors, polled)

    if fresh and delivered:
        for j in fresh:
            record(conn, j, notified=True)
        conn.commit()
    elif fresh:
        print("  [warn] %d matching role(s) deliberately left unrecorded — the next "
              "run will retry them." % len(fresh), file=sys.stderr)

    _report_errors(errors)
    log_run("digest", polled=polled, ok=polled - len(errors), errors=len(errors),
            new=len(batch), matched=len(fresh),
            emailed=decide_email(len(fresh), len(errors)),
            delivered=delivered, seconds=time.time() - started)
    conn.close()
    return 0 if delivered else 1


def cmd_health(cfg):
    """
    Exit nonzero when more than HEALTH_MAX_ERROR_RATE of enabled companies error.

    Worth running weekly: ATS tokens rot quietly, and a poller that polls nothing
    is indistinguishable from a quiet job market until you go looking.
    """
    started = time.time()
    entries = enabled_companies(cfg)
    print("Health check — %d enabled companies\n" % len(entries))
    jobs, errors = fetch_all(cfg, quiet=True)

    counts = {}
    for j in jobs:
        counts[j.company] = counts.get(j.company, 0) + 1
    failing = {name for name, _ in errors}
    empty = [e["company"] for e in entries
             if e["company"] not in failing and not counts.get(e["company"])]

    for name, why in errors:
        print("  FAIL  %-24s %s" % (name, why))
    for name in empty:
        print("  WARN  %-24s resolved but returned 0 postings" % name)

    rate = (len(errors) / float(len(entries))) if entries else 0.0
    healthy = rate <= HEALTH_MAX_ERROR_RATE
    print("\n%d/%d enabled companies erroring (%.0f%%), threshold %.0f%%."
          % (len(errors), len(entries), rate * 100, HEALTH_MAX_ERROR_RATE * 100))
    print("HEALTHY" if healthy
          else "UNHEALTHY — repair tokens with `python3 discover.py --fix-config`")

    log_run("health", polled=len(entries), ok=len(entries) - len(errors),
            errors=len(errors), new=0, matched=0, emailed="none",
            delivered=True, seconds=time.time() - started)
    return 0 if healthy else 1


def _report_errors(errors):
    if errors:
        print(f"\n  {len(errors)} company error(s):")
        for name, why in errors:
            print(f"    {name}: {why}")


# ─────────────────────────────────────────────────────────────────────────────
# Offline self-test — proves filtering + dedup work with zero network
# ─────────────────────────────────────────────────────────────────────────────

class _Check:
    """Tiny assertion counter so every section reports in the same shape."""

    def __init__(self):
        self.failed = 0

    def __call__(self, desc, ok, detail=""):
        if not ok:
            self.failed += 1
        suffix = ("   <- " + detail) if (detail and not ok) else ""
        print("  %s %s%s" % ("ok  " if ok else "FAIL", desc, suffix))
        return ok


def _selftest_filters(check):
    print("filters")
    cfg = {"filters": {
        "title_include": [r"\b(software|backend|platform|infrastructure|ai|ml)\b.*engineer",
                          r"engineer.*\b(backend|platform|infrastructure)\b",
                          r"\bforward.deployed\b", r"\bfde\b"],
        "title_exclude": [r"\b(intern|principal|staff|director|manager|vp)\b",
                          r"\b(senior|snr|sr|lead|distinguished|fellow|architect)\b",
                          r"\b(engineer|engineering|swe|sde|developer|programmer)"
                          r"\s*[-,]?\s*(level\s*)?(2|3|4|5|6|ii|iii|iv|v|vi)\b",
                          r"\b[lt][3-9]\b",
                          r"\bsales\b", r"\bsolutions? engineer\b"],
        "location_include": [r"seattle", r"bellevue", r"redmond", r"remote",
                             r"san francisco", r"bay area", r"new york",
                             r"london", r"berlin", r"amsterdam", r"dublin", r"zurich"],
        "location_exclude": [r"\bindia\b", r"bangalore", r"hyderabad", r"chennai"],
    }}
    f = Filters(cfg)

    cases = [
        (Job("Acme", "greenhouse", "1", "Software Engineer, Backend", "Seattle, WA", "u"), True,
         "plain backend SWE in Seattle"),
        (Job("Acme", "greenhouse", "2", "Staff Software Engineer", "Seattle, WA", "u"), False,
         "staff level excluded"),
        (Job("Acme", "greenhouse", "3", "Software Engineer Intern", "Remote", "u"), False,
         "intern excluded"),
        (Job("Acme", "greenhouse", "4", "Backend Engineer", "Bangalore, India", "u"), False,
         "excluded location"),
        (Job("Acme", "greenhouse", "5", "Solutions Engineer", "Seattle, WA", "u"), False,
         "solutions engineer is not SWE"),
        (Job("Acme", "greenhouse", "6", "AI Engineer", "London, UK", "u"), True,
         "AI engineer in London"),
        (Job("Acme", "greenhouse", "7", "Product Designer", "Seattle, WA", "u"), False,
         "wrong discipline"),
        (Job("Acme", "greenhouse", "8", "Platform Engineer", "", "u"), True,
         "blank location surfaces rather than drops"),
        # Seniority. The poller only ever sees the title, never the description,
        # so years-of-experience is invisible to it. These patterns catch the
        # explicit level markers, which is as far as title-only filtering goes.
        (Job("Acme", "greenhouse", "9", "Senior Software Engineer", "Berlin", "u"), False,
         "senior excluded"),
        (Job("Acme", "greenhouse", "10", "Sr. Software Engineer", "Seattle", "u"), False,
         "abbreviated Sr. excluded"),
        (Job("Acme", "greenhouse", "11", "Lead Backend Engineer", "Remote", "u"), False,
         "lead excluded"),
        (Job("Acme", "greenhouse", "12", "Software Engineer III", "Seattle", "u"), False,
         "roman numeral level excluded"),
        (Job("Acme", "greenhouse", "13", "Software Engineer 3", "Remote", "u"), False,
         "numeric level excluded"),
        (Job("Acme", "greenhouse", "14", "Backend Engineer, L5", "Seattle", "u"), False,
         "ladder code excluded"),
        (Job("Acme", "greenhouse", "15", "Software Engineer I", "Seattle", "u"), True,
         "level I survives — that is the level being hunted"),
        (Job("Acme", "greenhouse", "16", "Software Engineer, New Grad", "Remote", "u"), True,
         "new grad survives"),
        (Job("Acme", "greenhouse", "17", "Software Engineer", "Seattle", "u"), True,
         "unlevelled title survives — the title cannot reveal its YOE bar"),

        # Forward-deployed roles are wanted. They match none of the other
        # include patterns, so they need one of their own — removing the old
        # exclusion alone would still have dropped them.
        (Job("Acme", "greenhouse", "18", "Forward Deployed Engineer", "Seattle", "u"), True,
         "forward deployed engineer included"),
        (Job("Acme", "greenhouse", "19", "Forward-Deployed Software Engineer", "Remote", "u"), True,
         "hyphenated spelling included"),
        (Job("Acme", "greenhouse", "20", "FDE, Enterprise", "New York", "u"), True,
         "the FDE abbreviation included"),
        (Job("Acme", "greenhouse", "21", "Senior Forward Deployed Engineer", "Seattle", "u"), False,
         "seniority still wins over a wanted discipline"),
        (Job("Acme", "greenhouse", "22", "Solutions Engineer", "Remote", "u"), False,
         "sibling exclusions survive the edit — solutions still out"),
        (Job("Acme", "greenhouse", "23", "Field Engineer", "Seattle", "u"), False,
         "field engineer still out"),
    ]

    for job, expected, desc in cases:
        got = f.matches(job)
        check("%-46s expected=%s got=%s" % (desc, expected, got), got == expected)


def _selftest_dedup(check):
    print("\ndedup and digest idempotence")
    global DB_PATH
    original = DB_PATH
    DB_PATH = HERE / "_selftest.db"
    if DB_PATH.exists():
        DB_PATH.unlink()
    try:
        conn = db_connect()
        j = Job("Acme", "greenhouse", "42", "Backend Engineer", "Seattle", "u")
        first = is_new(conn, j.key)
        record(conn, j, notified=True)
        conn.commit()
        second = is_new(conn, j.key)
        check("first sighting new, second sighting not", first is True and second is False)

        # A digest pass must never re-collect something already in seen.db.
        already = Job("Acme", "greenhouse", "42", "Backend Engineer", "Seattle", "u")
        brand_new = Job("Acme", "greenhouse", "43", "Platform Engineer", "Seattle", "u")
        selected = [x for x in (already, brand_new) if is_new(conn, x.key)]
        check("digest re-run skips the job it already sent",
              [x.ats_id for x in selected] == ["43"],
              "selected=%s" % [x.ats_id for x in selected])

        # Same posting twice in one response must not mail twice.
        batch, picked = set(), []
        for x in (brand_new, brand_new):
            if x.key in batch or not is_new(conn, x.key):
                continue
            batch.add(x.key)
            picked.append(x)
        check("duplicate inside a single response collapses to one", len(picked) == 1)
        conn.close()
    finally:
        DB_PATH.unlink(missing_ok=True)
        DB_PATH = original


def _spec_example_jobs():
    """12 roles / 6 companies — the exact shape of the subject line in the README."""
    jobs = []
    for i in range(4):
        jobs.append(Job("Anthropic", "greenhouse", "a%d" % i, "Engineer %d" % i, "SF", "https://x/a"))
    for i in range(3):
        jobs.append(Job("Stripe", "greenhouse", "s%d" % i, "Engineer %d" % i, "SF", "https://x/s"))
    for i in range(2):
        jobs.append(Job("Capital One", "workday", "c%d" % i, "Engineer %d" % i, "VA", "https://x/c"))
    jobs.append(Job("Figma", "greenhouse", "f", "Engineer", "SF", "https://x/f"))
    jobs.append(Job("Ramp", "ashby", "r", "Engineer", "NYC", "https://x/r"))
    jobs.append(Job("Notion", "greenhouse", "n", "Engineer", "SF", "https://x/n"))
    return jobs


def _selftest_grouping(check):
    print("\ndigest grouping")
    jobs = [
        Job("Stripe", "greenhouse", "1", "Backend Engineer", "SF", "https://x/1"),
        Job("Anthropic", "greenhouse", "2", "Software Engineer, API", "SF", "https://x/2"),
        Job("Stripe", "greenhouse", "3", "API Engineer", "Remote", "https://x/3"),
        Job("Anthropic", "greenhouse", "4", "Platform Engineer", "Seattle", "https://x/4"),
        Job("Anthropic", "greenhouse", "5", "Backend Engineer", "NYC", "https://x/5"),
        Job("Ramp", "ashby", "6", "Infrastructure Engineer", "NYC", "https://x/6"),
    ]
    groups = group_by_company(jobs)
    check("one bucket per company", list(groups) == ["Anthropic", "Stripe", "Ramp"],
          "got=%s" % list(groups))
    check("largest company first", [len(v) for v in groups.values()] == [3, 2, 1],
          "got=%s" % [len(v) for v in groups.values()])
    check("titles sorted within a company",
          [j.title for j in groups["Anthropic"]] ==
          ["Backend Engineer", "Platform Engineer", "Software Engineer, API"],
          "got=%s" % [j.title for j in groups["Anthropic"]])
    check("no job lost in grouping", sum(len(v) for v in groups.values()) == len(jobs))
    check("empty input groups to nothing", group_by_company([]) == {})


def _selftest_subjects(check):
    print("\nsubject lines")
    got = digest_subject(_spec_example_jobs())
    check("README example reproduces exactly",
          got == "12 new roles — Anthropic, Stripe, Capital One +3", "got=%r" % got)

    one = digest_subject([Job("Anthropic", "gh", "1", "Engineer", "SF", "u")])
    check("single role is singular", one == "1 new role — Anthropic", "got=%r" % one)

    three = digest_subject([Job("A", "gh", "1", "E", "", "u"),
                            Job("B", "gh", "2", "E", "", "u"),
                            Job("C", "gh", "3", "E", "", "u")])
    check("exactly three companies gets no +N", three == "3 new roles — A, B, C",
          "got=%r" % three)

    err = error_subject([("A", "HTTP 404"), ("B", "HTTP 500"), ("C", "timeout")])
    check("failure subject names the count", err == "Job poller: 3 companies failing",
          "got=%r" % err)


def _selftest_rendering(check):
    print("\nrendering and escaping")
    nasty = Job("Ben & Jerry's", "greenhouse", "9",
                "R&D Engineer <script>alert(1)</script> — pay > $200k",
                "Seattle & Remote", "https://example.com/j?a=1&b=2")
    body = render_html([nasty], [], 1)
    check("html escapes &", "&amp;" in body)
    check("html escapes <", "&lt;script&gt;" in body)
    check("html escapes >", "&gt; $200k" in body)
    check("no raw <script> survives into the html", "<script>" not in body)
    check("ampersand in the href is escaped too",
          'href="https://example.com/j?a=1&amp;b=2"' in body)
    check("company name is escaped", "Ben &amp; Jerry" in body)
    check("no external stylesheet, image or style block",
          "<link" not in body and "<img" not in body and "<style" not in body)

    text = render_text([nasty], [], 1)
    check("plain text part is NOT html-escaped",
          "&amp;" not in text and "R&D Engineer" in text)
    check("plain text keeps the raw angle brackets", "<script>" in text and "> $200k" in text)
    check("plain text carries the url", "https://example.com/j?a=1&b=2" in text)

    hostile = Job("Acme", "gh", "1", "Engineer", "SF", "javascript:alert(1)")
    check("non-http url is never turned into a link",
          "javascript:" not in render_html([hostile], [], 1))

    with_errors = render_html(_spec_example_jobs(),
                              [("A", "HTTP 404"), ("B", "HTTP 500"), ("C", "timeout")], 44)
    check("digest banners widespread failure inline", "3 companies failed to poll" in with_errors)


def _selftest_delivery(check):
    print("\ndelivery decisions")
    check("zero matches, zero errors -> no email", decide_email(0, 0) == "none")
    check("zero matches, 2 errors -> still no email", decide_email(0, 2) == "none")
    check("zero matches, 3 errors -> failure alert", decide_email(0, 3) == "errors")
    check("matches -> digest", decide_email(5, 0) == "digest")
    check("matches plus failures -> one digest, not two mails", decide_email(5, 9) == "digest")

    fake = {"host": "smtp.example.com", "port": 587, "user": "me@example.com",
            "password": "secret", "to": ["me@example.com", "alt@example.com"],
            "sender": "me@example.com"}
    jobs = _spec_example_jobs()
    errors3 = [("A", "HTTP 404"), ("B", "HTTP 500"), ("C", "timeout")]
    sent = []

    def fake_send(settings, subject, text_body, html_body, attempts=2):
        sent.append(subject)
        return fake_send.ok
    fake_send.ok = True

    saved = {name: globals()[name]
             for name in ("send_email", "notify", "smtp_settings", "log_matches")}
    globals()["send_email"] = fake_send
    globals()["notify"] = lambda js: sent.append("fallback:%d" % len(js))
    globals()["log_matches"] = lambda js: None      # keep matches.jsonl untouched
    try:
        globals()["smtp_settings"] = lambda env=None: (fake, [])

        del sent[:]
        ok = deliver_digest([], [], 44)
        check("zero matches sends literally nothing", ok is True and sent == [],
              "returned=%s sent=%s" % (ok, sent))

        del sent[:]
        deliver_digest([], [("A", "x"), ("B", "y")], 44)
        check("two failures stay silent", sent == [], "sent=%s" % sent)

        del sent[:]
        deliver_digest([], errors3, 44)
        check("three failures send the alert",
              sent == ["Job poller: 3 companies failing"], "sent=%s" % sent)

        del sent[:]
        deliver_digest(jobs, [], 44)
        check("matches send one digest",
              sent == ["12 new roles — Anthropic, Stripe, Capital One +3"], "sent=%s" % sent)

        del sent[:]
        deliver_digest(jobs, errors3 + [("D", "z")], 44)
        check("matches plus failures still send exactly one email", len(sent) == 1,
              "sent=%s" % sent)

        del sent[:]
        fake_send.ok = False
        ok = deliver_digest(jobs, [], 44)
        check("failed send reports False so jobs stay unrecorded", ok is False)
        fake_send.ok = True

        # SMTP unconfigured: must degrade, not raise.
        globals()["smtp_settings"] = lambda env=None: (None, ["SMTP_HOST", "SMTP_PASS"])
        del sent[:]
        ok = deliver_digest(jobs, [], 44)
        check("no SMTP config falls back instead of crashing",
              ok is True and sent == ["fallback:12"], "returned=%s sent=%s" % (ok, sent))
    finally:
        globals().update(saved)

    print("\nmessage structure")
    msg = build_message(fake, "Subject here", "plain body", "<p>html body</p>")
    check("multipart/alternative", msg.get_content_type() == "multipart/alternative",
          "got=%s" % msg.get_content_type())
    types = [p.get_content_type() for p in msg.iter_parts()]
    check("carries a text part and an html part", types == ["text/plain", "text/html"],
          "got=%s" % types)
    check("both recipients on the To header", msg["To"] == "me@example.com, alt@example.com")
    check("subject preserved", msg["Subject"] == "Subject here")

    print("\nsmtp settings")
    cfg, missing = smtp_settings({"SMTP_HOST": "h", "SMTP_USER": "u",
                                  "SMTP_PASS": "p", "EMAIL_TO": "a@x.com, b@x.com"})
    check("port defaults to 587", cfg is not None and cfg["port"] == 587)
    check("EMAIL_TO splits on commas", cfg is not None and cfg["to"] == ["a@x.com", "b@x.com"])
    check("sender falls back to SMTP_USER", cfg is not None and cfg["sender"] == "u")
    _, missing = smtp_settings({"SMTP_HOST": "h"})
    check("missing vars are named, not raised",
          missing == ["SMTP_USER", "SMTP_PASS", "EMAIL_TO"], "got=%s" % missing)


def _selftest_http_retry(check):
    print("\nhttp retry")
    saved_open, saved_backoff = _open_json, HTTP_BACKOFF
    globals()["HTTP_BACKOFF"] = 0.0          # no real sleeping in tests
    calls = []
    try:
        def flaky(req):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
            return {"ok": True}
        globals()["_open_json"] = flaky
        got = http_json("https://example.invalid/a")
        check("503 retried, third attempt succeeds", got == {"ok": True} and len(calls) == 3,
              "calls=%d" % len(calls))

        del calls[:]

        def dead(req):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
        globals()["_open_json"] = dead
        try:
            http_json("https://example.invalid/b")
            check("404 raises immediately without retrying", False, "no exception raised")
        except urllib.error.HTTPError:
            check("404 raises immediately without retrying", len(calls) == 1,
                  "calls=%d" % len(calls))

        del calls[:]

        def slow(req):
            calls.append(1)
            raise urllib.error.URLError(socket.timeout("timed out"))
        globals()["_open_json"] = slow
        try:
            http_json("https://example.invalid/c")
            check("timeout retried exactly HTTP_RETRIES times", False, "no exception raised")
        except urllib.error.URLError:
            check("timeout retried exactly HTTP_RETRIES times",
                  len(calls) == 1 + HTTP_RETRIES, "calls=%d" % len(calls))
    finally:
        globals()["_open_json"] = saved_open
        globals()["HTTP_BACKOFF"] = saved_backoff


def _selftest_seed_guard(check):
    print("\nempty-database guard")
    global DB_PATH, RUNS_LOG
    orig_db, orig_log = DB_PATH, RUNS_LOG
    DB_PATH = HERE / "_selftest.db"
    RUNS_LOG = HERE / "_selftest_runs.log"
    DB_PATH.unlink(missing_ok=True)
    RUNS_LOG.unlink(missing_ok=True)

    cfg = {"filters": {"title_include": [r"engineer"]},
           "companies": [{"company": "Anthropic", "ats": "greenhouse", "token": "a"},
                         {"company": "Stripe", "ats": "greenhouse", "token": "s"}]}
    canned = [Job("Anthropic", "greenhouse", "1", "Backend Engineer", "SF", "https://x/1"),
              Job("Stripe", "greenhouse", "2", "Platform Engineer", "Remote", "https://x/2")]
    calls = []

    saved = {n: globals()[n] for n in ("fetch_all", "deliver_digest")}
    globals()["fetch_all"] = lambda c, quiet=False: (list(canned), [])
    globals()["deliver_digest"] = lambda js, errs, polled: calls.append(len(js)) or True
    try:
        rc = cmd_digest(cfg)
        check("empty seen.db seeds instead of emailing", rc == 0 and calls == [],
              "rc=%s calls=%s" % (rc, calls))

        conn = db_connect()
        rows = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        notified = conn.execute("SELECT COUNT(*) FROM seen WHERE notified=1").fetchone()[0]
        conn.close()
        check("seed run records everything and notifies nothing",
              rows == 2 and notified == 0, "rows=%d notified=%d" % (rows, notified))

        canned.append(Job("Ramp", "ashby", "3", "Infrastructure Engineer", "NYC", "https://x/3"))
        rc = cmd_digest(cfg)
        check("the run after that delivers only the genuinely new role",
              rc == 0 and calls == [1], "rc=%s calls=%s" % (rc, calls))
    finally:
        globals().update(saved)
        DB_PATH.unlink(missing_ok=True)
        RUNS_LOG.unlink(missing_ok=True)
        DB_PATH, RUNS_LOG = orig_db, orig_log


def cmd_selftest():
    print("Running offline self-test (no network)...\n")
    check = _Check()
    _selftest_filters(check)
    _selftest_dedup(check)
    _selftest_seed_guard(check)
    _selftest_grouping(check)
    _selftest_subjects(check)
    _selftest_rendering(check)
    _selftest_delivery(check)
    _selftest_http_retry(check)
    print("\n%s" % ("ALL PASSED" if check.failed == 0 else "%d FAILED" % check.failed))
    return 0 if check.failed == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="Poll company ATS boards for new roles.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verify", action="store_true", help="test config entries against live endpoints")
    g.add_argument("--seed", action="store_true", help="record current jobs without notifying")
    g.add_argument("--once", action="store_true", help="single polling pass")
    g.add_argument("--loop", action="store_true", help="poll forever")
    g.add_argument("--digest", action="store_true",
                   help="one pass, email one grouped digest, exit (for cron/Actions)")
    g.add_argument("--health", action="store_true",
                   help="exit 1 if more than 30%% of enabled companies are erroring")
    g.add_argument("--selftest", action="store_true", help="offline logic tests, no network")
    args = ap.parse_args()

    if args.selftest:
        return cmd_selftest()

    cfg = load_config()
    if args.verify:
        return cmd_verify(cfg)
    if args.seed:
        return cmd_seed(cfg)
    if args.once:
        return cmd_once(cfg)
    if args.loop:
        return cmd_loop(cfg)
    if args.digest:
        return cmd_digest(cfg)
    if args.health:
        return cmd_health(cfg)


if __name__ == "__main__":
    sys.exit(main())
