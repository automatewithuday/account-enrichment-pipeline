-- One-time setup: paste into your Postgres project's SQL editor. enrich.py upserts into these three tables after writing the CSVs.
-- companies: one row per input domain (the intel card). people: one row per contact, FK to companies. Neither carries provider names or
-- per-provider costs (those stay in the CSVs and raw_responses; see DB_HIDE in enrich.py) so they can be shown as-is in a demo.
-- raw_responses: one row per provider call, keyed by the local cache key; the full provider JSON lives in `response`.

create table if not exists companies (
  domain text primary key,
  company_name text, linkedin_url text, website text, tagline text, description text,
  industry text, specialities text, categories text, company_type text,
  employee_count integer, employee_count_range text, headcount_growth_6m_pct numeric, headcount_growth_12m_pct numeric,
  hq text, country text, founded_year integer, revenue_estimate_low_usd numeric, revenue_estimate_high_usd numeric, linkedin_followers integer,
  company_miss_reason text, linkedin_miss_reason text,
  funding_total_usd numeric, last_round_type text, last_round_amount_usd numeric, last_round_date date, investors text, funding_miss_reason text,
  tech_stack text, tech_stale text, tech_last_detected date, tech_miss_reason text,
  openings_count integer, openings_growth_pct numeric, jobs_total integer, jobs_newest_posted date, job_titles text, jobs_miss_reason text,
  titles_at_company text, titles_miss_reason text, title_matches integer, people_miss_reason text,
  data_as_of date,                       -- oldest provider fetch behind the row
  last_enriched_at timestamptz,          -- newest live provider fetch behind the row; cache-served reruns do not move it
  run_id text,
  updated_at timestamptz not null default now()
);

create table if not exists people (
  id text primary key,                   -- LinkedIn profile URL, else domain|first|last
  domain text not null references companies(domain) on delete cascade,
  company_name text, first_name text, last_name text, title text, linkedin_url text,
  email text, email_status text, email_domain text, mx_provider text, mx_gateway text, mx_gateway_type text, email_verified_at date, email_miss_reason text,
  last_enriched_at timestamptz,          -- newest of the person's people-search page and email lookup; skip re-verifying inside your window
  run_id text,
  updated_at timestamptz not null default now()
);
create index if not exists people_domain_idx on people(domain);

create table if not exists raw_responses (
  cache_key text primary key,            -- sha256(backend|tool|payload), same key as .cache/calls.jsonl
  domain text references companies(domain) on delete cascade,  -- NULL for the two batched lookups that cover many domains in one call
  backend text not null, tool text not null,
  payload jsonb, response jsonb,
  cost numeric, fetched_at timestamptz, from_cache boolean,
  run_id text,
  updated_at timestamptz not null default now()
);
create index if not exists raw_responses_domain_tool_idx on raw_responses(domain, tool);

-- keep updated_at honest on upserts
create or replace function touch_updated_at() returns trigger language plpgsql as $$ begin new.updated_at = now(); return new; end $$;
do $$ declare t text; begin
  foreach t in array array['companies','people','raw_responses'] loop
    execute format('drop trigger if exists %I_touch on %I', t, t);
    execute format('create trigger %I_touch before update on %I for each row execute function touch_updated_at()', t, t);
  end loop;
end $$;

-- Lock the tables to the service-role key (enrich.py) only; the anon/public key gets nothing. No policies are needed.
alter table companies enable row level security;
alter table people enable row level security;
alter table raw_responses enable row level security;

-- Migration 2026-09-25 for databases created before provider columns were removed from the demo tables (idempotent, safe to re-run).
alter table companies drop column if exists company_source, drop column if exists linkedin_url_source, drop column if exists funding_source,
  drop column if exists crustdata_updated_at, drop column if exists deepline_cr, drop column if exists apify_usd, drop column if exists builtwith_cr;  -- legacy column names
alter table people drop column if exists source;
