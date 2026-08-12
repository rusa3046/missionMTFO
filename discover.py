#!/usr/bin/env python3
"""
Token discovery: given a company name, try candidate slugs against every supported
ATS until something resolves. Beats opening devtools 22 times.

Usage:
    python3 discover.py "Capital One"                 # try everything
    python3 discover.py "Notion" --ats ashby          # narrow to one ATS
    python3 discover.py --fix-config                  # auto-repair every FAIL in companies.json
    python3 discover.py --workday capitalone.wd1.myworkdayjobs.com capitalone

Politeness: ~0.4s between probes. A full multi-ATS sweep for one company is ~40 requests.
"""

import argparse
import itertools
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
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


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
                except Exception:                                # noqa: BLE001
                    pass
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
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(DELAY)
    return hits


def cmd_fix_config():
    """Walk companies.json, retry every entry, and write back what resolves."""
    cfg = json.loads(CONFIG_PATH.read_text())
    changed, unresolved = [], []

    for entry in cfg["companies"]:
        if entry.get("disabled"):
            continue
        name, ats = entry["company"], entry["ats"]

        # Does it already work?
        try:
            if ats == "workday":
                url = f"https://{entry['host']}/wday/cxs/{entry['tenant']}/{entry['site']}/jobs"
                d = probe(url, "POST", {"appliedFacets": {}, "limit": 5,
                                        "offset": 0, "searchText": ""})
                if d.get("jobPostings"):
                    continue
            else:
                if SIMPLE[ats](entry["token"]) > 0:
                    continue
        except Exception:                                        # noqa: BLE001
            pass

        print(f"\n=== {name} ({ats}) ===")
        if ats == "workday":
            hits = discover_workday(entry["host"], entry["tenant"])
            if hits:
                entry["site"] = hits[0]["site"]
                entry["verified"] = True
                changed.append((name, f"site -> {hits[0]['site']}"))
            else:
                unresolved.append(name)
        else:
            hits = discover_simple(name)
            if hits:
                best = max(hits, key=lambda h: h["count"])
                entry["ats"] = best["ats"]
                entry["token"] = best["token"]
                entry.pop("host", None)
                entry.pop("tenant", None)
                entry.pop("site", None)
                entry["verified"] = True
                changed.append((name, f"{best['ats']}/{best['token']}"))
            else:
                unresolved.append(name)

    if changed:
        backup = CONFIG_PATH.with_suffix(".json.bak")
        backup.write_text(CONFIG_PATH.read_text())
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"\nWrote {len(changed)} fixes to companies.json (backup: {backup.name})")
        for n, w in changed:
            print(f"  {n}: {w}")

    if unresolved:
        print(f"\n{len(unresolved)} still unresolved — these likely use an unsupported ATS:")
        for n in unresolved:
            print(f"  {n}")
        print("Mark them \"disabled\": true and track via HiringCafe.")
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
    a = ap.parse_args()

    if a.fix_config:
        return cmd_fix_config()
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
