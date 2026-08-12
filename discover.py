#!/usr/bin/env python3
"""
Token discovery: given a company name, try candidate slugs against every supported
ATS until something resolves. Beats opening devtools 22 times.

Usage:
    python3 discover.py "Capital One"                 # try everything
    python3 discover.py "Notion" --ats ashby          # narrow to one ATS
    python3 discover.py --fix-config                  # auto-repair every FAIL in companies.json
    python3 discover.py --fix-config --dry-run        # report the repairs, write nothing
    python3 discover.py --fix-config --allow-ats-switch   # let a company move between ATSes
    python3 discover.py --workday capitalone.wd1.myworkdayjobs.com capitalone

Politeness: ~0.4s between probes. A full multi-ATS sweep for one company is ~40 requests.

Three deliberate safety properties, learned the hard way:

  A blocked network is not a finding. Transport failures (DNS, TLS, proxy,
  timeout) are counted separately from HTTP 404s, and --fix-config aborts
  without writing if nothing resolved and the network looked broken. Reporting
  "no such board" when you simply had no connectivity is worse than crashing.

  Your formatting survives. Repairs are applied as targeted edits to the raw
  text of companies.json, so alignment, comments-by-convention and entry
  ordering are preserved. Re-serialising the whole file would reformat all 88
  entries to prove a two-character change.

  A company never changes ATS silently. `token` and `site` are repaired in
  place; moving a company from Greenhouse to Ashby needs --allow-ats-switch,
  because a generic slug can match an unrelated company's board.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "companies.json"
UA = "job-poller-discovery/1.0 (personal job search)"
DELAY = 0.4
TIMEOUT = 15


class ProbeError(Exception):
    """
    Transport-level failure: DNS, TLS, proxy refusal, timeout.

    Explicitly NOT "this board does not exist" — that arrives as an HTTPError
    with a status code. Conflating the two is what makes a discovery tool
    confidently tell you every company is unreachable when it's your wifi.
    """


class NetStats:
    """Counts real HTTP answers vs transport failures across a whole sweep."""

    def __init__(self):
        self.answered = 0     # server replied, any status
        self.failed = 0       # never got a reply

    def looks_offline(self):
        return self.answered == 0 and self.failed > 0

    def __str__(self):
        return "%d answered, %d transport failures" % (self.answered, self.failed)


NET = NetStats()

# Workday instance labels. A tenant lives on exactly one of these, and the label
# is not derivable from the company — capitalone is wd1 or wd12 depending on when
# they were provisioned. Guessing wrong makes every site probe fail, which is why
# --fix-config found nothing for nine Workday tenants before this existed.
WORKDAY_INSTANCES = ["wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd101", "wd103"]

# Site-path segments that Workday tenants overwhelmingly use, most common first.
WORKDAY_SITES = [
    "External", "external", "EXTERNAL_CAREERS", "External_Career_Site",
    "careers", "Careers", "CAREERS", "search", "Search",
    "ExternalCareerSite", "External_Careers", "externalcareers",
    "{slug}", "{Slug}", "{slug}careers", "{Slug}_Careers", "{Slug}Careers",
    "{slug}_External", "Global_Careers", "GlobalCareers", "jobs", "Jobs",
    "External_Site", "PrimaryCareerSite", "Professional", "US_External",
]


def probe(url, method="GET", body=None):
    """
    One request. Raises HTTPError for a real HTTP status, ProbeError if we never
    reached the server at all. Callers must treat those two very differently.
    """
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError:
        NET.answered += 1      # a 404 is still the server talking to us
        raise
    except Exception as e:     # noqa: BLE001 — URLError, socket.timeout, ssl, DNS
        NET.failed += 1
        raise ProbeError("%s: %s" % (type(e).__name__, e))
    NET.answered += 1
    return payload


def slug_candidates(name):
    """Generate plausible slugs from a company name, most likely first."""
    base = name.lower()
    base = re.sub(r"[/&]", " ", base)
    base = re.sub(r"\b(inc|corp|corporation|co|ltd|llc|plc|group|global|tech|health|the)\b", " ", base)
    words = [w for w in re.split(r"[^a-z0-9]+", base) if w]

    cands = []
    if words:
        cands.append("".join(words))          # capitalone
        cands.append("-".join(words))         # capital-one
        cands.append(words[0])                # capital
        if len(words) > 1:
            cands.append("".join(words[:2]))
            cands.append(words[0] + words[-1])
    # original name, squashed
    cands.append(re.sub(r"[^a-z0-9]", "", name.lower()))
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def try_greenhouse(slug):
    d = probe(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return len(d.get("jobs", []))


def try_lever(slug):
    d = probe(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return len(d) if isinstance(d, list) else 0


def try_ashby(slug):
    d = probe(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return len(d.get("jobs", []))


def try_smartrecruiters(slug):
    d = probe(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=10")
    return len(d.get("content", []))


SIMPLE = {
    "greenhouse": try_greenhouse,
    "lever": try_lever,
    "ashby": try_ashby,
    "smartrecruiters": try_smartrecruiters,
}


def discover_simple(name, only_ats=None, extra_slugs=()):
    """Try each candidate slug against each simple ATS. Returns list of hits."""
    hits = []
    slugs = list(extra_slugs) + slug_candidates(name)
    atss = [only_ats] if only_ats else list(SIMPLE)

    for ats in atss:
        fn = SIMPLE.get(ats)
        if not fn:
            continue
        for slug in slugs:
            variants = [slug]
            if ats == "smartrecruiters":
                variants = [slug, slug.capitalize(), slug.title().replace("-", ""),
                            slug.replace("-", "").capitalize()]
            for v in dict.fromkeys(variants):
                try:
                    n = fn(v)
                    if n > 0:
                        print(f"  HIT   {ats:<16} token={v!r}  ({n} postings)")
                        hits.append({"ats": ats, "token": v, "count": n})
                    else:
                        print(f"  empty {ats:<16} token={v!r}")
                except urllib.error.HTTPError as e:
                    if e.code not in (404, 400):
                        print(f"  ?     {ats:<16} token={v!r} HTTP {e.code}")
                except ProbeError as e:
                    # Never silent. This is the difference between "no such
                    # board" and "we never got out of the building".
                    print(f"  NET   {ats:<16} token={v!r} unreachable ({e})")
                time.sleep(DELAY)
    return hits


def discover_workday(host, tenant, sites=None):
    """Given a known host+tenant, find the site segment. This is the 422 fix."""
    slug = tenant
    candidates = sites or [
        s.replace("{slug}", slug).replace("{Slug}", slug.capitalize())
        for s in WORKDAY_SITES
    ]
    hits = []
    for site in dict.fromkeys(candidates):
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
        try:
            d = probe(url, method="POST", body=body)
            total = d.get("total", 0)
            n = len(d.get("jobPostings", []))
            if n > 0:
                print(f"  HIT   site={site!r}  ({n} shown, {total} total)")
                hits.append({"site": site, "count": total})
                break                      # first hit is almost always correct
            else:
                print(f"  empty site={site!r}")
        except urllib.error.HTTPError as e:
            if e.code == 422:
                pass                       # wrong site, keep going — this is the common case
            elif e.code == 404:
                pass
            else:
                print(f"  ?     site={site!r} HTTP {e.code}")
        except ProbeError as e:
            print(f"  NET   site={site!r} unreachable ({e})")
        time.sleep(DELAY)
    return hits


# ─────────────────────────────────────────────────────────────────────────────
# Surgical edits to companies.json
#
# The config is hand-formatted: aligned columns, one entry per line, a curated
# ordering. json.dump() would flatten all of that to prove a two-character
# change, so repairs are applied to the raw text instead.
# ─────────────────────────────────────────────────────────────────────────────

def _scan_object(text, start):
    """Index just past the `}` closing the object that opens at `start`."""
    depth, i, in_string, escaped = 0, start, False, False
    while i < len(text):
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _entry_span(text, company):
    """(start, end) of the JSON object describing `company`, or None."""
    m = re.search(r'"company"\s*:\s*"%s"' % re.escape(company), text)
    if not m:
        return None
    start = text.rfind("{", 0, m.start())
    if start == -1:
        return None
    end = _scan_object(text, start)
    return None if end is None else (start, end)


_VALUE = r'"(?:[^"\\]|\\.)*"|true|false|null|-?\d+(?:\.\d+)?'


def _set_field(obj_text, key, value):
    """Set one key inside a single JSON object's raw text, adding it if absent."""
    literal = json.dumps(value)
    pattern = re.compile(r'("%s"\s*:\s*)(%s)' % (re.escape(key), _VALUE))
    if pattern.search(obj_text):
        return pattern.sub(lambda m: m.group(1) + literal, obj_text, count=1)
    body = obj_text[:obj_text.rfind("}")].rstrip().rstrip(",")
    return '%s, "%s": %s}' % (body, key, literal)


def apply_edits(text, edits):
    """
    edits: [(company, {key: value}), ...]. Applied back-to-front so earlier
    spans stay valid as the text length changes.
    """
    spans = []
    for company, fields in edits:
        span = _entry_span(text, company)
        if span is None:
            print("  [warn] could not locate %r in the raw config — skipped" % company)
            continue
        spans.append((span, fields))

    for (start, end), fields in sorted(spans, key=lambda s: s[0][0], reverse=True):
        obj = text[start:end]
        for key, value in fields.items():
            obj = _set_field(obj, key, value)
        text = text[:start] + obj + text[end:]
    return text


def validate_config(text, before):
    """
    Structural check on repaired config text. Returns a problem string, or None.

    Runs before anything is written. Relying on a downstream CI workflow to catch
    a broken config does not work here: pushes made with the default GITHUB_TOKEN
    never trigger workflows, so the validation has to happen in this process.
    """
    try:
        after = json.loads(text)
    except ValueError as e:
        return "result is not valid JSON: %s" % e

    old_names = [e["company"] for e in before["companies"]]
    new_names = [e["company"] for e in after.get("companies", [])]
    if new_names != old_names:
        return ("company list changed (%d -> %d entries)"
                % (len(old_names), len(new_names)))
    if after.get("filters") != before.get("filters"):
        return "filters block was modified"

    for entry in after["companies"]:
        if entry.get("disabled"):
            continue
        if entry["ats"] == "workday":
            missing = [k for k in ("host", "tenant", "site") if not entry.get(k)]
            if missing:
                return "%s is missing %s" % (entry["company"], ", ".join(missing))
        elif not entry.get("token"):
            return "%s has no token" % entry["company"]
    return None


def _entry_works(entry):
    """True/False if we got an answer, None if we never reached the endpoint."""
    try:
        if entry["ats"] == "workday":
            url = ("https://%s/wday/cxs/%s/%s/jobs"
                   % (entry["host"], entry["tenant"], entry["site"]))
            payload = probe(url, "POST", {"appliedFacets": {}, "limit": 5,
                                          "offset": 0, "searchText": ""})
            return bool(payload.get("jobPostings"))
        fn = SIMPLE.get(entry["ats"])
        return fn(entry["token"]) > 0 if fn else False
    except urllib.error.HTTPError:
        return False
    except ProbeError:
        return None
    except Exception:                                            # noqa: BLE001
        return False


def workday_host_candidates(host, tenant):
    """The configured host first, then every plausible instance for this tenant."""
    cands = [host] if host else []
    for inst in WORKDAY_INSTANCES:
        cands.append("%s.%s.myworkdayjobs.com" % (tenant, inst))
    return list(dict.fromkeys(c for c in cands if c))


def discover_workday_host(host, tenant):
    """
    Find which instance actually hosts this tenant, using one cheap probe each.

    The status code is the whole signal, so read it carefully:
        200  this host AND this site are correct — done
        422  tenant exists here, site segment is wrong — enumerate sites next
        401  tenant exists but the board is private — no amount of guessing helps
        404  wrong tenant for this host — try the next instance
        (unreachable)  host does not exist — try the next instance

    Returns (host, site_or_None, status) where status is "ok", "site-unknown",
    "auth", or "none".
    """
    for candidate in workday_host_candidates(host, tenant):
        url = "https://%s/wday/cxs/%s/External/jobs" % (candidate, tenant)
        body = {"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": ""}
        try:
            payload = probe(url, method="POST", body=body)
            if payload.get("jobPostings"):
                print("  HOST  %s (site 'External' works)" % candidate)
                return candidate, "External", "ok"
            print("  host  %s reachable but 'External' is empty" % candidate)
            return candidate, None, "site-unknown"
        except urllib.error.HTTPError as e:
            if e.code == 422:
                print("  HOST  %s (tenant exists, site segment wrong)" % candidate)
                return candidate, None, "site-unknown"
            if e.code == 401:
                print("  AUTH  %s requires authentication — not publicly pollable"
                      % candidate)
                return candidate, None, "auth"
            # 404 and friends: wrong tenant for this instance.
        except ProbeError:
            pass                      # instance does not exist; that is expected
        time.sleep(DELAY)
    return None, None, "none"


def cmd_fix_config(dry_run=False, allow_ats_switch=False):
    """Walk companies.json, retry every entry, and repair what resolves."""
    original = CONFIG_PATH.read_text()
    cfg = json.loads(original)
    edits, unresolved, suggestions, inconclusive, private = [], [], [], [], []

    for entry in cfg["companies"]:
        if entry.get("disabled"):
            continue
        name, ats = entry["company"], entry["ats"]

        state = _entry_works(entry)
        if state is True:
            continue
        if state is None:
            inconclusive.append(name)
            print("  [skip] %s — could not reach the endpoint to test it" % name)
            continue

        print("\n=== %s (%s) ===" % (name, ats))

        if ats == "workday":
            host, site, status = discover_workday_host(entry.get("host"), entry["tenant"])
            if status == "auth":
                private.append(name)
                continue
            if status == "none":
                unresolved.append(name)
                continue

            if site is None:
                hits = discover_workday(host, entry["tenant"])
                site = hits[0]["site"] if hits else None
            if site is None:
                unresolved.append(name)
                continue

            fields = {"site": site, "verified": True}
            if host != entry.get("host"):
                fields["host"] = host
                print("  FIX   host -> %r, site -> %r" % (host, site))
            else:
                print("  FIX   site -> %r" % site)
            edits.append((name, fields))
            continue

        # Same-ATS repair first: this only ever rewrites `token`.
        hits = discover_simple(name, only_ats=ats)
        if hits:
            best = max(hits, key=lambda h: h["count"])
            edits.append((name, {"token": best["token"], "verified": True}))
            print("  FIX   token -> %r (%d postings)" % (best["token"], best["count"]))
            continue

        # Nothing on its own ATS. Look elsewhere, but do not act without consent:
        # a generic slug can easily match an unrelated company's board.
        others = [a for a in SIMPLE if a != ats]
        cross = []
        for other in others:
            cross.extend(discover_simple(name, only_ats=other))
        if not cross:
            unresolved.append(name)
            continue

        best = max(cross, key=lambda h: h["count"])
        if allow_ats_switch:
            edits.append((name, {"ats": best["ats"], "token": best["token"],
                                 "verified": True}))
            print("  FIX   %s/%s -> %s/%s"
                  % (ats, entry.get("token"), best["ats"], best["token"]))
        else:
            suggestions.append((name, ats, best))
            print("  NOTE  found on %s as %r (%d postings) — not applied"
                  % (best["ats"], best["token"], best["count"]))

    # A sweep that reached nothing proves nothing. Never write on that basis.
    if NET.looks_offline():
        print("\n" + "=" * 70)
        print("ABORTED — no endpoint answered (%s)." % NET)
        print("This looks like a network/proxy problem, not 44 dead job boards.")
        print("Nothing was written. Re-run where the ATS hosts are reachable.")
        print("=" * 70)
        return 2

    if edits and not dry_run:
        repaired = apply_edits(original, edits)
        problem = validate_config(repaired, cfg)
        if problem:
            print("\n" + "=" * 70)
            print("REFUSED TO WRITE — the repaired config failed validation:")
            print("  %s" % problem)
            print("companies.json is unchanged.")
            print("=" * 70)
            return 3
        CONFIG_PATH.with_suffix(".json.bak").write_text(original)
        CONFIG_PATH.write_text(repaired)
        print("\nWrote %d repair(s) to companies.json, validated "
              "(backup: companies.json.bak)" % len(edits))
    elif edits:
        print("\n--dry-run: %d repair(s) NOT written:" % len(edits))
    else:
        print("\nNo repairs to apply.")
    for name, fields in edits:
        print("  %s: %s" % (name, ", ".join("%s=%r" % kv for kv in fields.items())))

    if suggestions:
        print("\n%d compan%s resolved on a DIFFERENT ATS. Review, then re-run with "
              "--allow-ats-switch to accept:" % (len(suggestions),
                                                 "y" if len(suggestions) == 1 else "ies"))
        for name, was, best in suggestions:
            print("  %-22s %s -> %s/%s (%d postings)"
                  % (name, was, best["ats"], best["token"], best["count"]))

    if private:
        phrase = ("company exists but requires" if len(private) == 1
                  else "companies exist but require")
        print("\n%d %s authentication (HTTP 401). No token will ever fix that — "
              'mark them "disabled": true:' % (len(private), phrase))
        for name in private:
            print("  %s" % name)

    if inconclusive:
        print("\n%d compan%s untested (endpoint unreachable): %s"
              % (len(inconclusive), "y" if len(inconclusive) == 1 else "ies",
                 ", ".join(inconclusive)))

    if unresolved:
        print("\n%d still unresolved — these likely use an unsupported ATS:"
              % len(unresolved))
        for name in unresolved:
            print("  %s" % name)
        print('Mark them "disabled": true and track via HiringCafe.')

    print("\nnetwork: %s" % NET)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("company", nargs="?", help="company name to search for")
    ap.add_argument("--ats", choices=list(SIMPLE), help="narrow to one ATS")
    ap.add_argument("--slug", action="append", default=[], help="extra slug to try")
    ap.add_argument("--workday", nargs=2, metavar=("HOST", "TENANT"),
                    help="find the site segment for a known Workday host+tenant")
    ap.add_argument("--fix-config", action="store_true",
                    help="auto-repair every broken entry in companies.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --fix-config: report the repairs, write nothing")
    ap.add_argument("--allow-ats-switch", action="store_true",
                    help="with --fix-config: permit moving a company between ATSes "
                         "(off by default — a generic slug can match the wrong company)")
    a = ap.parse_args()

    if a.fix_config:
        return cmd_fix_config(dry_run=a.dry_run, allow_ats_switch=a.allow_ats_switch)
    if a.workday:
        host, tenant = a.workday
        print(f"Probing Workday sites for {tenant} @ {host}...\n")
        hits = discover_workday(host, tenant)
        if hits:
            print(f"\nUse: \"site\": \"{hits[0]['site']}\"")
        else:
            print("\nNo site resolved. Check host/tenant, or the tenant may be private.")
        return 0
    if not a.company:
        ap.error("give a company name, --workday, or --fix-config")

    print(f"Searching for {a.company}...\n")
    hits = discover_simple(a.company, a.ats, a.slug)
    if hits:
        best = max(hits, key=lambda h: h["count"])
        print(f"\nBest: \"ats\": \"{best['ats']}\", \"token\": \"{best['token']}\"")
    else:
        print("\nNothing resolved. Likely a custom or unsupported ATS — disable and use HiringCafe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
