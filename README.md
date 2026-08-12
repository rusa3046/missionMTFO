# Career-page poller

Watches company ATS boards directly and emails you one digest every morning at
**07:00 Pacific**, via GitHub Actions. Stdlib Python only — no `pip install`, no
dependencies to break in six months.

Covers **Greenhouse, Lever, Ashby, SmartRecruiters, and Workday**. Workday matters most
here: Capital One, Amex, CVS, Disney, Nike, Deere and most of the enterprise-AI list run on
it, and it's the adapter most off-the-shelf tools skip because it needs a POST with a JSON
body rather than a simple GET.

---

## How it runs

`.github/workflows/daily.yml` polls every enabled company, diffs against `seen.db`,
and sends **one grouped email** containing only roles it has never seen before.

Three rules govern the inbox:

- **New roles → one email**, grouped by company, subject like
  `12 new roles — Anthropic, Stripe, Capital One +3`.
- **Nothing new → no email at all.** A daily "nothing today" message teaches you to
  filter the thread away, and then the morning it matters you don't look.
- **3+ companies failing → an email anyway**, subject `Job poller: 5 companies failing`.
  A poller whose tokens have rotted looks exactly like a quiet job market from the
  inbox. That's the failure worth paying attention to.

At most one email per run. When there are both new roles and widespread failures, the
digest carries a red banner at the top rather than arriving as a second message.

---

## Setup

### 1. Add the secrets

**Settings → Secrets and variables → Actions → New repository secret**
(direct link: `https://github.com/rusa3046/missionMTFO/settings/secrets/actions`)

| Secret | Required | Value |
|---|---|---|
| `SMTP_HOST` | yes | `smtp.gmail.com` |
| `SMTP_PORT` | no | `587` (STARTTLS). Use `465` for implicit TLS. Defaults to 587. |
| `SMTP_USER` | yes | your full Gmail address |
| `SMTP_PASS` | yes | the 16-character **App Password** from the next step |
| `EMAIL_TO` | yes | where the digest goes; comma-separated for several recipients |
| `EMAIL_FROM` | no | defaults to `SMTP_USER` |
| `TELEGRAM_BOT_TOKEN` | no | optional fallback, used only when SMTP is unset |
| `TELEGRAM_CHAT_ID` | no | optional |

Miss one of the four required secrets and the run does **not** fail — it logs which are
missing and falls back to the Telegram/stdout path. That's deliberate: a missing secret
should cost you a notification, not the run.

### 2. Gmail App Password

A normal Gmail account password **will always be rejected** by `smtplib`. You need an App
Password, and App Passwords only exist once 2-Step Verification is on.

1. Turn on 2-Step Verification: **myaccount.google.com → Security → 2-Step Verification**
2. Go to **myaccount.google.com/apppasswords**
3. Name it something like `job-poller`, click **Create**
4. Copy the 16-character code (Google shows it with spaces — paste it **without** them)
5. Paste it into the `SMTP_PASS` secret

If you use a Workspace account and the App Passwords page doesn't exist, your admin has
disabled them; use a personal Gmail or another SMTP provider instead.

### 3. First run — do this in order

Half the tokens in `companies.json` have never resolved (see **Known gaps**), so don't
let the schedule take the first swing. Use **Actions → daily digest → Run workflow**,
which has a **mode** dropdown:

```
verify      → tests every entry against its live endpoint, reports what's broken
fix-tokens  → runs discover.py --fix-config and commits the repaired companies.json
seed        → records everything currently posted WITHOUT emailing you
health      → exits nonzero if >30% of enabled companies are erroring
digest      → the real thing (this is what the 7am schedule runs)
```

1. Run **verify**. It exits nonzero whenever anything needs fixing, so **a red X here is
   the command working**, not the workflow breaking — read the FAIL/WARN list.
2. Run **fix-tokens** to repair what's repairable. It edits `companies.json` in place and
   commits it. Review that commit's diff before moving on.
3. Run **verify** again to confirm the count improved.
4. Run **seed** once. This is not optional. Any company you just repaired has **zero** rows
   in `seen.db`, so its first digest would dump every currently-open role at once rather
   than the handful posted overnight.
5. Run **digest** manually once to confirm the email actually lands.
6. Leave it alone. The schedule takes over at 07:00 Pacific.

### What fix-tokens can and can't repair

**Workday needs the instance right before the site matters.** A tenant lives on exactly
one instance — `wd1`, `wd5`, `wd12` — and the label isn't derivable from the company name.
Get it wrong and every site probe fails, which is why the first `fix-tokens` run repaired
none of the nine Workday entries. `discover.py` now probes each candidate instance once
and reads the status code: **200** means done, **422** means the tenant is there and only
the site segment is wrong (then it enumerates ~25 known segments), **401** means the board
is private and no token will ever fix it, **404** means try the next instance.

**404s depend on the slug being guessable from the company name.** Candidates come from
`slug_candidates()`, which only squashes and hyphenates words: `Trade Republic` yields
`traderepublic`, `trade-republic`, `trade`. It will **never** guess `notionhq` from
`Notion`, or `perplexity-ai` from `Perplexity`. For those, open the careers page, read the
real slug out of the URL, and edit `companies.json` by hand — see *Finding the right token*.

Three safety properties, each learned from a way this can go wrong:

- **A blocked network is not a finding.** Transport failures are counted separately from
  HTTP 404s. If nothing answers, the command aborts with exit 2 and writes nothing, rather
  than reporting that all 44 boards are gone.
- **Your formatting survives.** Repairs are applied as targeted edits to the raw text, so
  aligned columns and entry ordering are preserved. Only the repaired lines change.
- **A company never changes ATS silently.** `token` and `site` are fixed in place. Moving a
  company from Greenhouse to Ashby requires the **allow_ats_switch** checkbox, because a
  generic slug can match an unrelated company's board. Without it, cross-ATS matches are
  reported as suggestions and left unapplied.

### Nothing this job commits is checked by CI

Pushes made with the default `GITHUB_TOKEN` **never trigger workflows** — GitHub suppresses
that to prevent recursion. So `selftest.yml` does not run on the `chore:` commits this job
creates, no matter what the commit message says.

Validation therefore happens **in the job, before the commit**: the workflow runs
`--selftest` plus a structural check of `companies.json` and `seen.db`, and the commit step
is skipped entirely if that fails. `discover.py` independently validates its own output and
refuses to write (exit 3) if a repair would drop an entry, alter the filters, or produce
invalid JSON.

---

## Verifying it's actually running

This is the real risk with any scheduled job: it stops, and nothing tells you. On a quiet
day "no email" is also what success looks like, so **the inbox cannot be your monitor.**

**The primary signal is `runs.log`.** Every run appends one line and commits it back:

```
2026-08-12T14:00:31Z  mode=digest companies=44  ok=41  errors=3   new=18   matched=6   email=digest delivered=yes dur=94s
```

So the daily check is: **did a commit land this morning?**

```bash
git pull && tail -5 runs.log
git log --oneline -5 -- runs.log     # one commit per day = healthy
```

No line for yesterday means it never ran — which is precisely the failure no amount of
inbox-watching would have surfaced.

**Turn on failure notifications.** GitHub emails you when a workflow run fails, which
covers SMTP errors and failed pushes. Check
**github.com/settings/notifications → Actions → Send notifications for: failed workflows only**.

**The weekly health check** (`.github/workflows/health.yml`, Mondays) fails deliberately
when more than 30% of enabled companies are erroring. A failed run emails you via the
setting above.

**The 60-day trap.** GitHub **disables scheduled workflows in repositories with no
activity for 60 days**, and bot commits don't reliably reset that timer. You get a warning
email first, and re-enabling takes one click in the Actions tab — but if you ever notice
the digest went quiet for a week, this is the first thing to check.

**Also worth doing once:** send yourself a test digest, then add a Gmail filter on the
sender so it never lands in spam. Silent spam-foldering looks identical to silent breakage.

---

## The 7am Pacific / DST problem

GitHub's cron is **UTC only and does not observe DST**, so a single cron entry drifts by an
hour twice a year. 07:00 Pacific is 14:00 UTC in summer (PDT) and 15:00 UTC in winter (PST).

The workflow schedules **both**, then guards:

```yaml
- cron: '0 14 * * *'   # 07:00 PDT
- cron: '0 15 * * *'   # 07:00 PST
```

A guard step computes the real local time with `zoneinfo("America/Los_Angeles")` and exits
early unless the hour is 7. Manual `workflow_dispatch` runs skip the guard entirely.

**The tradeoff:** one extra no-op run per day — roughly fifteen seconds of Actions time,
and it shows up in the run history as a skipped run. In exchange the digest lands at 7am
year-round. The simpler alternative, a single `0 14 * * *`, costs no extra run but delivers
at 6am Pacific from November to March.

Worth knowing: GitHub's scheduler is best-effort and can fire 5–20 minutes late under load.
The guard checks the hour, not the minute, so a late start still passes.

---

## How state survives between runs

Actions runners are ephemeral, so `seen.db` is **committed back to the repo** after every
run, with `[skip ci]` so it doesn't retrigger the push-driven test workflow.

**Why commit-back and not `actions/cache`:** cache misses are silent and, here, expensive.
Cache entries are immutable per key, evict after 7 days unused, and vanish when the repo
hits its 10 GB limit. A miss doesn't error — you'd get a fresh empty database and ~4,600
postings in a single email. Commit-back fails loudly instead: a bad push turns the run red.
It's also inspectable (`sqlite3 seen.db …` on any clone) and restorable
(`git checkout HEAD~3 -- seen.db`).

**Concurrency:** the workflow uses a `concurrency` group so a manual run queues behind a
scheduled one rather than racing it. If a push still loses a race, the run does
`git pull --rebase` and retries once. `seen.db` is binary and unmergeable, so our copy
wins; `runs.log` and `matches.jsonl` union-merge via `.gitattributes`.

**If the push ultimately fails,** the email has already gone out, so the run exits nonzero
(red, and you get a notification) and today's roles may appear again in tomorrow's digest.
That ordering is deliberate throughout: `--digest` holds matching jobs out of `seen.db`
until delivery is confirmed. A failed send repeats tomorrow; recording first would drop
roles you never saw, silently and permanently. **Duplicates over loss.**

**If `seen.db` ever goes missing anyway** — bad checkout, failed push, someone deletes it —
`--digest` notices the database is empty and treats that run as a **seed**: it records
everything, emails nothing, and says so loudly in the log. The next run resumes normally.
An empty database is indistinguishable from "every posting on earth is new", and emailing
4,600 roles is the one failure this project exists to prevent.

**Repo growth:** each daily commit stores a fresh ~0.27 MB compressed copy of the DB —
call it 100 MB/year. Fine for years. If it ever bothers you, `sqlite3 seen.db 'VACUUM;'`
or squash the state commits.

---

## Commands

| Command | What it does |
|---|---|
| `--verify` | test every config entry against its live endpoint |
| `--seed` | record everything currently posted, without notifying |
| `--once` | one polling pass, notify on new matches |
| `--loop` | poll forever at `CHECK_INTERVAL` |
| `--digest` | one pass, one grouped email, exit — what the cron job runs |
| `--health` | exit 1 if >30% of enabled companies are erroring |
| `--selftest` | offline logic tests, no network, no side effects |

`--selftest` covers filtering, dedup, digest grouping, subject-line generation, the
zero-matches-means-no-email rule, HTML escaping, and the HTTP retry policy. It touches no
network and writes no files. Run it before pushing anything.

Transient failures (HTTP 5xx, 429, timeouts) are retried twice with exponential backoff
and honour `Retry-After`. A 404 or 422 is **not** retried — that means the token is wrong,
and hammering it just abuses an endpoint published as a courtesy.

---

## Running locally

```bash
set -a && source .env && set +a     # see .env.example
python3 poller.py --digest
```

Or the original always-on mode: `python3 poller.py --loop`, or cron every 10 minutes with
`--once`. Both still work exactly as before; `--digest` is additive.

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

Tune by watching two or three digests. Too much noise → tighten `title_exclude`.
Suspiciously quiet → loosen `title_include` and confirm `--verify` is still clean.

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

Or just run `python3 discover.py --fix-config`, which probes candidate slugs and site
segments and writes back what resolves.

---

## Files

| File | Purpose |
|---|---|
| `poller.py` | Everything. Adapters, filters, storage, digest, notification. |
| `companies.json` | Company list + filters. The only file you edit routinely. |
| `discover.py` | Token discovery and auto-repair for broken config entries. |
| `seen.db` | SQLite dedup state. **Committed** — it's the runner's only memory. |
| `runs.log` | One line per run. **Committed.** Your proof the job is alive. |
| `matches.jsonl` | Append-only log of every match. **Committed.** |
| `.github/workflows/daily.yml` | The 7am digest. |
| `.github/workflows/health.yml` | Weekly endpoint health check. |
| `.github/workflows/selftest.yml` | Offline tests on every push. |

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

Then run **verify**, then **seed** — new companies seed silently and existing state is
untouched. Skipping the seed means that company's first digest dumps its entire open board
on you.

Adding a whole new ATS is one function plus one line in `ADAPTERS`.

---

## Known gaps

**22 of the 44 enabled companies have never resolved.** `seen.db` was seeded on
2026-08-08 and contains 4,615 postings across only 22 companies. The rest were guesses that
didn't resolve — every entry is marked `"verified": false` for exactly this reason. Run
**verify**, then `discover.py --fix-config`, then **seed** before trusting a quiet morning.

Five companies are `disabled: true` because they expose no supported endpoint:

- **Google, Meta, Netflix, Walmart** — custom career sites or Eightfold
- **JPMorgan Chase** — Oracle Recruiting Cloud, not Workday

Track those through HiringCafe, which aggregates career pages directly.

---

## Privacy

`seen.db` and `matches.jsonl` are committed, and they contain the company names, job
titles and URLs of everything you're tracking — i.e. exactly where you're job hunting.
**Keep this repository private** while your search is live.

---

## Using this as a portfolio piece

It's small but it demonstrates things a resume usually asserts without evidence:
five-adapter abstraction over inconsistent third-party APIs, per-company error isolation so
one bad endpoint can't kill a run, idempotent dedup with a deliberate seed phase, an
unattended scheduled job that treats its own silence as a failure mode, and offline tests
for the logic that actually matters.

If you publish it: strip the company list to a generic example, drop `seen.db` and
`matches.jsonl` from history, and say in one line why you built it.
