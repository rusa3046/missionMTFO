# Career-page poller

Watches company ATS boards directly and pushes new matching roles to your phone.
Stdlib Python only — no `pip install`, no dependencies to break in six months.

Covers **Greenhouse, Lever, Ashby, SmartRecruiters, and Workday**. Workday matters most
here: Capital One, Amex, CVS, Disney, Nike, Deere and most of the enterprise-AI list run on
it, and it's the adapter most off-the-shelf tools skip because it needs a POST with a JSON
body rather than a simple GET.

---

## Read this first

**Every token in `companies.json` is an unverified guess.** They were written without
network access, so none has been tested against a live endpoint. Some will be wrong —
company board slugs don't reliably match company names. That's expected and takes about
30 seconds each to fix.

Run `--verify` before anything else and fix the failures. Don't skip this.

---

## Setup (10 minutes)

```bash
cd poller

# 1. Confirm the logic works — no network needed
python3 poller.py --selftest

# 2. Test every config entry against live endpoints
python3 poller.py --verify

# 3. Fix any FAIL/WARN entries (see below), then re-run --verify until clean

# 4. Record everything currently posted, WITHOUT notifying
python3 poller.py --seed

# 5. From now on, only genuinely new postings notify you
python3 poller.py --once
```

Step 4 is not optional. Skip it and your first run pushes several thousand notifications.

### Telegram (optional, 5 minutes)

Without this, matches print to your terminal and append to `matches.jsonl`. With it, your
phone buzzes.

1. Message [@BotFather](https://t.me/botfather) on Telegram → `/newbot` → copy the token
2. Message your new bot once (bots can't message you first)
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `result[0].message.chat.id`

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="987654321"
```

Put these in your shell profile so cron inherits them, or set them in the crontab directly.

---

## Finding the right token

**Greenhouse** — careers URL looks like `job-boards.greenhouse.io/SLUG` or
`boards.greenhouse.io/SLUG`. The `SLUG` is your token. If the company embeds Greenhouse on
its own site, open devtools → Network → look for a request to `boards-api.greenhouse.io`.
Verify directly:
```
https://boards-api.greenhouse.io/v1/boards/SLUG/jobs
```

**Lever** — `jobs.lever.co/SLUG`. Verify: `https://api.lever.co/v0/postings/SLUG?mode=json`

**Ashby** — `jobs.ashbyhq.com/SLUG`. Verify:
`https://api.ashbyhq.com/posting-api/job-board/SLUG`

**SmartRecruiters** — `jobs.smartrecruiters.com/SLUG`. Case-sensitive. Verify:
`https://api.smartrecruiters.com/v1/companies/SLUG/postings`

**Workday** — needs three fields, all visible in the careers URL:
```
https://capitalone.wd1.myworkdayjobs.com/en-US/Capital_One
        └──────── host ────────┘              └── site ──┘
tenant is usually the first label of the host ("capitalone")
```
Verify by opening the careers page with devtools → Network → find the POST to
`/wday/cxs/TENANT/SITE/jobs`. The `site` segment is the part people get wrong most often —
copy it exactly, including underscores and capitalization.

---

## Running it continuously

**cron** (simplest):
```cron
*/10 * * * * cd /path/to/poller && TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy /usr/bin/python3 poller.py --once >> poller.log 2>&1
```

**foreground** (for testing): `python3 poller.py --loop`

**launchd / systemd**: wrap `--loop` in a unit file if you want restart-on-crash.

Ten minutes is deliberate. Faster gains you nothing real and starts to look like abuse of
endpoints these companies publish as a courtesy.

---

## Tuning the filters

`companies.json` → `filters`. All values are case-insensitive regex.

- `title_include` — a job must match at least one
- `title_exclude` — any match disqualifies (checked after include)
- `location_include` — must match one **if the posting has a location string**
- `location_exclude` — any match disqualifies

Two deliberate choices worth knowing:

**Blank locations pass through.** Lots of postings leave location empty. Dropping them
silently would hide real roles, so they surface and you judge.

**`title_exclude` blocks Staff and above.** At your level those are noise. When you're
ready for them, delete `staff|principal|distinguished|fellow|architect` from that list.

Tune by running `--once` and watching what comes through for two days. Too much noise →
tighten `title_exclude`. Suspiciously quiet → loosen `title_include` and confirm `--verify`
is still clean.

---

## Files

| File | Purpose |
|---|---|
| `poller.py` | Everything. Adapters, filters, storage, notification. |
| `companies.json` | Company list + filters. The only file you edit routinely. |
| `seen.db` | SQLite dedup state. Delete it to reset (then re-seed). |
| `matches.jsonl` | Append-only log of every match. Useful for the Week 4 retro. |

Query your own history later:
```bash
sqlite3 seen.db "SELECT company, COUNT(*) FROM seen WHERE notified=1 GROUP BY company ORDER BY 2 DESC;"
```

---

## Adding a company

```json
{"tier": 2, "company": "Acme", "ats": "greenhouse", "token": "acme", "verified": false}
```

Workday:
```json
{"tier": 2, "company": "Acme", "ats": "workday",
 "host": "acme.wd1.myworkdayjobs.com", "tenant": "acme", "site": "External",
 "search_text": "software engineer", "verified": false}
```

Then `--verify`, then `--seed` (new companies seed silently; existing state is untouched).

Adding a whole new ATS is one function plus one line in `ADAPTERS`.

---

## Known gaps

Five companies are `disabled: true` because they don't expose a supported endpoint:

- **Google, Meta, Netflix, Walmart** — custom career sites or Eightfold
- **JPMorgan Chase** — Oracle Recruiting Cloud, not Workday

Track those through HiringCafe, which aggregates career pages directly. The poller covers
44 companies; these five stay manual on your Monday sweep.

---

## Using this as a portfolio piece

It's small but it demonstrates things your resume currently asserts without evidence:
five-adapter abstraction over inconsistent third-party APIs, per-company error isolation so
one bad endpoint can't kill a run, idempotent dedup with a deliberate seed phase, and
offline tests for the logic that actually matters.

If you publish it: strip your company list to a generic example, add the filter-tuning
rationale to the README, and say in one line why you built it. That's a legible small
project, not a toy.
