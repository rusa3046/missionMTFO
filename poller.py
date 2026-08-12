#!/usr/bin/env python3
"""
Career-page poller: watches company ATS endpoints and pushes new matching roles.

Stdlib only. No pip install required.

Usage:
    python3 poller.py --verify          # test every config entry, report which resolve
    python3 poller.py --seed            # record current jobs WITHOUT notifying (run this first)
    python3 poller.py --once            # one polling pass, notify on new matches
    python3 poller.py --loop            # poll forever at CHECK_INTERVAL
    python3 poller.py --selftest        # run offline logic tests, no network needed

Environment:
    TELEGRAM_BOT_TOKEN    optional; if unset, notifications go to stdout + matches.jsonl
    TELEGRAM_CHAT_ID      optional
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "companies.json"
DB_PATH = HERE / "seen.db"
MATCHES_LOG = HERE / "matches.jsonl"

USER_AGENT = "job-poller/1.0 (personal job search; contact via github)"
TIMEOUT = 20
POLITE_DELAY = 1.0      # seconds between companies; be a good citizen
CHECK_INTERVAL = 600    # 10 minutes


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

def http_json(url, method="GET", body=None, extra_headers=None):
    """Fetch JSON. Raises urllib.error.* on failure; caller decides how loud to be."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


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

def notify(jobs):
    """Push to Telegram if configured; always append to matches.jsonl and print."""
    if not jobs:
        return

    with MATCHES_LOG.open("a") as fh:
        for j in jobs:
            fh.write(json.dumps(asdict(j)) + "\n")

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
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def load_config():
    with CONFIG_PATH.open() as fh:
        return json.load(fh)


def fetch_all(cfg, quiet=False):
    """Returns (jobs, errors). One bad company never kills the run."""
    jobs, errors = [], []
    for entry in cfg["companies"]:
        if entry.get("disabled"):
            continue
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


def _report_errors(errors):
    if errors:
        print(f"\n  {len(errors)} company error(s):")
        for name, why in errors:
            print(f"    {name}: {why}")


# ─────────────────────────────────────────────────────────────────────────────
# Offline self-test — proves filtering + dedup work with zero network
# ─────────────────────────────────────────────────────────────────────────────

def cmd_selftest():
    print("Running offline self-test (no network)...\n")
    cfg = {"filters": {
        "title_include": [r"\b(software|backend|platform|infrastructure|ai|ml)\b.*engineer",
                          r"engineer.*\b(backend|platform|infrastructure)\b"],
        "title_exclude": [r"\b(intern|principal|staff|director|manager|vp)\b",
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
        (Job("Acme", "greenhouse", "9", "Senior Software Engineer", "Berlin", "u"), True,
         "senior is allowed"),
    ]

    failed = 0
    for job, expected, desc in cases:
        got = f.matches(job)
        mark = "ok  " if got == expected else "FAIL"
        if got != expected:
            failed += 1
        print(f"  {mark} {desc:<45} expected={expected} got={got}")

    # dedup
    global DB_PATH
    original = DB_PATH
    DB_PATH = HERE / "_selftest.db"
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = db_connect()
    j = Job("Acme", "greenhouse", "42", "Backend Engineer", "Seattle", "u")
    a = is_new(conn, j.key)
    record(conn, j, notified=True)
    conn.commit()
    b = is_new(conn, j.key)
    ok = (a is True and b is False)
    print(f"  {'ok  ' if ok else 'FAIL'} dedup: first sighting new, second sighting not")
    if not ok:
        failed += 1
    conn.close()
    DB_PATH.unlink(missing_ok=True)
    DB_PATH = original

    print(f"\n{'ALL PASSED' if failed == 0 else str(failed) + ' FAILED'}")
    return 0 if failed == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="Poll company ATS boards for new roles.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verify", action="store_true", help="test config entries against live endpoints")
    g.add_argument("--seed", action="store_true", help="record current jobs without notifying")
    g.add_argument("--once", action="store_true", help="single polling pass")
    g.add_argument("--loop", action="store_true", help="poll forever")
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


if __name__ == "__main__":
    sys.exit(main())
