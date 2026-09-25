# Account Enrichment Pipeline

Give it a CSV of company domains and the job titles you sell to. It returns one row of company intel per domain, including which email security gateway sits in front of the inbox, and one row per matching decision-maker with a verified work email, at a few cents per account. It never pays twice for the same data, and can mirror everything into a Postgres database.

```
domains.csv + "VP Sales,Chief Revenue,Founder,CEO"
        │
        ▼
   accounts.csv   one row per company: identity, size & growth, revenue, funding, tech stack, hiring, titles
   people.csv     one row per contact: name, title, profile URL, verified email, mail provider
   (optional)     the same rows plus every raw provider response, upserted into a Postgres database
```

## What you get

**Per company** (accounts.csv, one row per input domain)

| Group | Columns |
|---|---|
| Identity | company_name, website, linkedin_url, tagline, description, industry, specialities, categories, company_type, hq, country, founded_year |
| Mail routing | seg_vendor (security gateway in front of the inbox: proofpoint, mimecast, barracuda, cisco, ... or none), mailbox_provider (google, microsoft, zoho, other), mx_hosts |
| Size & growth | employee_count, employee_count_range, headcount_growth_6m_pct, headcount_growth_12m_pct, linkedin_followers |
| Revenue & funding | revenue_estimate_low_usd, revenue_estimate_high_usd, funding_total_usd, last_round_type, last_round_amount_usd, last_round_date, investors |
| Tech stack | tech_stack (current), tech_stale (not seen in 180 days), tech_last_detected |
| Hiring | jobs_total (open postings, last 30 days), job_titles (newest first), jobs_newest_posted |
| People | titles_at_company (every title found, your targets first), title_matches |
| Freshness & cost | data_as_of (oldest fetch behind the row), per-step *_miss_reason, per-provider cost columns |

**Per contact** (people.csv, up to `--max-people` per company)

| Group | Columns |
|---|---|
| Person | first_name, last_name, title, linkedin_url, source |
| Email | email, email_status (verified live), email_domain, email_verified_at |
| Mail routing | mx_provider, mx_security_gateway (true when a gateway sits in front), mx_gateway_type |
| Mobile (`--mobile` only) | mobile, mobile_cc, mobile_status, do_not_contact (provider opt-out flag, honour it) |

Rows are never dropped. Every blank data column has a sibling `*_miss_reason` that says why it is blank (`no_jobs`, `no_email`, `domain_mismatch:bit.ly`, `http_403`, ...).

## Example output

Illustrative values for a fictional company.

accounts.csv

| column | value |
|---|---|
| domain | acme.io |
| company_name | Acme |
| industry | Software Development |
| employee_count / range | 240 / 201-500 |
| headcount_growth_6m_pct / 12m | 3.1 / 12.4 |
| revenue_estimate_low_usd / high | 20000000 / 50000000 |
| funding_total_usd | 31500000 |
| last_round_type / amount / date | Series B / 22000000 / 2025-03-14 |
| seg_vendor / mailbox_provider | proofpoint / microsoft |
| tech_stack | React;Next.js;Amazon CloudFront;Google Tag Manager;HubSpot;... |
| jobs_total | 14 |
| job_titles | Enterprise Account Executive;SDR Manager;Head of Demand Generation;... |
| jobs_newest_posted | 2026-09-23 |
| titles_at_company | Founder & CEO;VP Sales;Chief Revenue Officer;Account Executive;... |
| title_matches | 3 |
| data_as_of | 2026-09-24 |

people.csv

| first_name | last_name | title | email | email_status | mx_provider | mx_security_gateway |
|---|---|---|---|---|---|---|
| Jane | Doe | VP Sales | jane@acme.io | valid | microsoft 365 | true |
| Raj | Patel | Chief Revenue Officer | raj@acme.io | valid | microsoft 365 | true |

## The pipeline

Ten steps per domain, cheapest first. Each later step overwrites what an earlier one guessed.

```
 1. baseline lookup          free   coarse name / industry / size for all domains in one call
 2. mail routing (DNS)       free   MX records -> security gateway vendor + mailbox provider, before any paid step
 3. LinkedIn URL resolution  free   homepage footer link > batched identify > company record > baseline
 4. company record           paid   funding, revenue estimate, headcount growth, categories, investors
    └─ funding fallback      paid   funding rounds by website, only when step 4 has no funding block
 5. company profile scrape   paid   name, industry, headcount, HQ, founded, description, followers (source of truth)
 6. tech stack               paid   technologies with last-detected dates; stale ones split out
 7. job postings             paid   open postings, titles, newest date; postings for other companies are filtered out
 8. titles at company        free   every title held at the company, your targets first
 9. people search            free   contacts matching your titles; tries legacy / alias domains when the input domain is empty
10. email finder             paid   verified work email + per-person gateway flag, billed only on a hit
11. mobile finder             paid   mobile number per contact, only with --mobile, billed only on a hit
```

Every step runs against a provider you configure in `.env`. A missing provider key does not stop the run; the affected columns carry a miss reason.

## Cost

Measured on real accounts. Reruns from cache are free.

| Scenario | Per account |
|---|---|
| Full fresh intel card, two verified contacts | about $0.13 |
| Same, with a mobile number for both contacts (`--mobile`) | about $1.64 |
| Card only, no contacts | about $0.06 |
| Card without tech stack | about $0.03 |
| Each additional verified email | $0.034, nothing on a miss |
| Each mobile number (`--mobile`) | $0.755 |

Every provider response is cached in `.cache/calls.jsonl`, keyed by the exact request. Re-running a list costs nothing. `--max-age DAYS` re-fetches only responses older than DAYS, which is the knob for a periodic refresh. `--dry-run` prints every call that would be billed before you spend anything.

## Quick start

```
cp .env.example .env            # fill in your provider keys
uv sync
uv run python test_enrich.py    # offline self-check, prints "ok"
uv run python enrich.py --input sample.csv --titles "VP Sales,Chief Revenue" --dry-run
uv run python enrich.py --input sample.csv --titles "VP Sales,Chief Revenue"
```

Options: `--limit N` (first N domains), `--max-people N` (contacts per company, default 5), `--mobile` (mobile number per contact, off by default), `--max-age DAYS`, `--out-dir DIR`, `--no-db`. `DEBUG=1` logs every call to stderr.

## Optional database

With `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in `.env`, every run also upserts three linked tables after writing the CSVs. Run `schema.sql` once in your project's SQL editor.

| Table | Key | Holds |
|---|---|---|
| companies | domain | the intel card, without provider names or cost columns, plus last_enriched_at |
| people | profile URL | the contact rows with email, mail routing and optional mobile, foreign key to companies |
| raw_responses | request hash | one row per provider call with the full JSON response, fetch time and cost; re-fetching a step overwrites its row |

`last_enriched_at` is the time a provider was actually fetched, not the time the CLI ran, so a cached rerun does not fake freshness. The database is a mirror: CSVs are written first and a database error never fails a run.

## Design rules

- Never pay twice: every response is cached, errors are never cached, and changing a request re-bills only that request.
- Never drop a row: blanks always carry a reason.
- Cheapest source first, most trusted source last.
- Single module, no framework, three dependencies (`httpx`, `python-dotenv`, `dnspython`). Python via `uv`.
- The gateway map (`gateway-map.json`) is data, not code: add an MX suffix there when a new gateway shows up as `unknown`.
- Secrets live in `.env` only.
