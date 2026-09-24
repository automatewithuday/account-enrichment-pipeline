"""Lean account enrichment: CSV of domains + target titles -> accounts.csv + people.csv.

Nine enrichment steps per domain (firmographics, funding, company profile, tech stack, job postings, titles, people, verified email),
each behind a credentialed provider configured in .env. Every network call is cached in .cache/calls.jsonl keyed by
(backend, id, payload); reruns cost nothing. With database keys in .env every run also upserts companies / people / raw_responses (see schema.sql).
"""
import argparse
import csv
import re
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
HOST = os.getenv("DEEPLINE_HOST_URL", "").rstrip("/")
DL_KEY = os.getenv("DEEPLINE_API_KEY")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
DEBUG = os.getenv("DEBUG") == "1"
SB_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ACTOR = "harvestapi~linkedin-job-search"
GATEWAY_MAP = json.loads((Path(__file__).parent / "gateway-map.json").read_text())  # MX suffixes -> security gateway / mailbox provider
LI_COMPANY = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([A-Za-z0-9._%-]+)", re.I)
PRICE_DL = {"crustdata_v3_company_search": 0.02, "leadmagic_email_finder": 0.34}  # credits per returned result; everything else free
CACHE_PATH = Path(".cache/calls.jsonl")
CACHE: dict[str, dict] = {}
SPENT: list[tuple[str, float, bool, float]] = []  # (backend, amount, from_cache, fetched_at epoch)
RAW: list[dict] = []  # every non-error response this run, in raw_responses row shape; cache hits included so a rerun backfills the DB
CUR_DOMAIN = None  # ponytail: single-threaded global so cached() can stamp RAW rows with the domain being enriched
MAX_AGE_DAYS = None  # --max-age: cached responses older than this are re-fetched (and re-billed)
JOBS_MAX = 25  # LinkedIn postings per company, $0.001 each; count shows "25+" when capped
STALE_TECH_DAYS = 180  # ponytail: a technology last detected before this goes to tech_stale; tune once scoring says what "current" means
DRY = False
PLANNED: list[str] = []

ACCOUNT_COLS = [
    "domain", "company_source", "company_name", "linkedin_url", "linkedin_url_source", "website", "tagline", "description", "industry", "specialities", "categories", "company_type",
    "employee_count", "employee_count_range", "headcount_growth_6m_pct", "headcount_growth_12m_pct",
    "hq", "country", "founded_year", "revenue_estimate_low_usd", "revenue_estimate_high_usd", "linkedin_followers", "company_miss_reason", "linkedin_miss_reason",
    "seg_vendor", "mailbox_provider", "mx_hosts", "seg_miss_reason",
    "funding_total_usd", "last_round_type", "last_round_amount_usd", "last_round_date", "investors", "funding_source", "funding_miss_reason",
    "tech_stack", "tech_stale", "tech_last_detected", "tech_miss_reason", "openings_count", "openings_growth_pct", "jobs_total", "jobs_newest_posted", "job_titles", "jobs_miss_reason",
    "titles_at_company", "titles_miss_reason", "title_matches", "people_miss_reason", "crustdata_updated_at", "data_as_of", "deepline_cr", "apify_usd", "builtwith_cr",
]
PEOPLE_COLS = ["domain", "company_name", "first_name", "last_name", "title", "linkedin_url", "source",
               "email", "email_status", "email_domain", "mx_provider", "mx_security_gateway", "mx_gateway_type", "email_verified_at", "email_miss_reason"]


def log(*a):
    if DEBUG:
        print(*a, file=sys.stderr)


def norm_domain(s):
    s = (s or "").strip().lower()
    for p in ("https://", "http://"):
        s = s.removeprefix(p)
    return s.removeprefix("www.").split("/")[0].split("?")[0]


def match_titles(title, wanted):
    t = (title or "").lower()
    return any(w.lower() in t for w in wanted)


def get(obj, *paths):
    """First non-empty value at any dotted path."""
    for path in paths:
        cur = obj
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur not in (None, "", [], {}):
            return cur
    return None


def rows_of(raw, *keys):
    """Find the list of rows in a provider payload of unknown nesting."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in keys + ("rows", "companies", "leads", "titles", "data", "output", "results", "items", "result"):
            v = raw.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and (inner := rows_of(v, *keys)):
                return inner
    return []


def load_cache():
    if CACHE_PATH.exists():
        for line in CACHE_PATH.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                CACHE[rec["key"]] = rec


def cached(backend, ident, payload, fn):
    key = hashlib.sha256(f"{backend}|{ident}|{json.dumps(payload, sort_keys=True, separators=(',', ':'))}".encode()).hexdigest()
    rec = CACHE.get(key)
    if rec and (MAX_AGE_DAYS is None or time.time() - rec.get("ts", 0) < MAX_AGE_DAYS * 86400):
        SPENT.append((backend, rec["cost"], True, rec.get("ts", 0)))
        RAW.append({"cache_key": key, "domain": CUR_DOMAIN, "backend": backend, "tool": ident, "payload": payload, "response": rec["resp"],
                    "cost": rec["cost"], "fetched_at": iso(rec.get("ts", 0)), "from_cache": True})
        return rec["resp"]
    if DRY:
        PLANNED.append(f"{backend}:{ident}")
        return {"error": "dry_run"}
    resp = fn(key)
    cost = float(resp.pop("_cost", 0.0))
    SPENT.append((backend, cost, False, time.time()))
    if "error" not in resp:  # errors are never cached, so a fixed key/quota just reruns
        rec = {"key": key, "backend": backend, "id": ident, "resp": resp, "cost": cost, "ts": time.time()}
        CACHE_PATH.parent.mkdir(exist_ok=True)
        with CACHE_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        CACHE[key] = rec
        RAW.append({"cache_key": key, "domain": CUR_DOMAIN, "backend": backend, "tool": ident, "payload": payload, "response": resp,
                    "cost": cost, "fetched_at": iso(rec["ts"]), "from_cache": False})
    return resp


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def db_upsert(table, rows, on_conflict):
    """Postgres REST upsert in chunks of 500; never raises, returns rows written. No-op without keys."""
    if not (SB_URL and SB_KEY and rows):
        return 0
    n = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = request(f"db:{table}", "POST", f"{SB_URL}/rest/v1/{table}?on_conflict={on_conflict}", json=chunk, timeout=60,
                    headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}", "Prefer": "resolution=merge-duplicates,return=minimal"})
        if r is None or r.status_code >= 400:
            print(f"db: {table} upsert failed after {n} rows: {'no response' if r is None else f'http_{r.status_code} {r.text[:200]}'}", file=sys.stderr)
            return n
        n += len(chunk)
    return n


DB_HIDE = {"company_source", "linkedin_url_source", "funding_source", "crustdata_updated_at", "deepline_cr", "apify_usd", "builtwith_cr", "source"}  # provider names stay in the CSVs and raw_responses, never on the demo tables


def db_write(accounts, people, run_id):
    """companies first (people/raw carry its FK), then people, then raw responses deduped by cache key. Blank strings become NULL."""
    clean = lambda row: {k: (None if v == "" else v) for k, v in row.items() if k not in DB_HIDE}
    nc = db_upsert("companies", [clean({k: a.get(k) for k in ACCOUNT_COLS + ["last_enriched_at"]} | {"run_id": run_id}) for a in accounts], "domain")
    np_ = db_upsert("people", [clean({k: p.get(k) for k in PEOPLE_COLS + ["id", "last_enriched_at"]} | {"run_id": run_id}) for p in people], "id")
    nr = db_upsert("raw_responses", [r | {"run_id": run_id} for r in {r["cache_key"]: r for r in RAW}.values()], "cache_key")
    return f"companies {nc}, people {np_}, raw {nr}"


def request(label, method, url, **kw):
    """4 attempts on 429/409/5xx/transport errors; returns Response or None when exhausted."""
    for i in range(4):
        try:
            r = httpx.request(method, url, **kw)
            if r.status_code not in (429, 409) and r.status_code < 500:
                return r
            err = f"http_{r.status_code}"
        except httpx.TransportError as e:
            err = f"transport:{type(e).__name__}"
        log("retry", label, err)
        time.sleep(2 ** i)
    return None


def deepline(tool, payload, backend="deepline"):
    def fn(key):
        if not DL_KEY:
            return {"error": "no_deepline_key"}
        r = request(tool, "POST", f"{HOST}/api/v2/integrations/{tool}/execute", json={"payload": payload},
                    headers={"Authorization": f"Bearer {DL_KEY}", "Idempotency-Key": key}, timeout=90)
        if r is None:
            return {"error": "http_5xx_exhausted"}
        if r.status_code >= 400:
            return {"error": f"http_{r.status_code}", "detail": r.text[:200]}
        body = r.json()
        raw = get(body, "toolResponse.raw", "result")
        billing = body.get("billing")
        log(tool, r.status_code, billing, json.dumps(raw)[:700])
        cost = get(billing or {}, "credits", "creditsCharged", "credits_charged", "amount")
        if not isinstance(cost, (int, float)):
            cost = PRICE_DL.get(tool, 0.0) * (1 if rows_of(raw) else 0)
        return {"raw": raw, "billing": billing, "_cost": cost}
    return cached(backend, tool, payload, fn)


def apify(actor, payload, per_item_usd, start_usd=0.001, **params):
    def fn(key):
        if not APIFY_TOKEN:
            return {"error": "no_apify_token"}
        r = request("apify", "POST", f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                    params={"token": APIFY_TOKEN, "clean": "true", **params}, json=payload, timeout=320)  # the platform hard-fails sync runs at 300s
        if r is None:
            return {"error": "http_5xx_exhausted"}
        if r.status_code == 408:
            return {"error": "apify_408_timeout"}
        if r.status_code == 403 and "not-approved" in r.text:  # one-time approval in the platform console
            return {"error": "apify_actor_not_approved:" + str(get(r.json(), "error.data.approvalUrl"))}
        if r.status_code >= 400:
            return {"error": f"http_{r.status_code}", "detail": r.text[:200]}
        items = r.json()
        log("apify", actor, r.status_code, len(items))
        return {"items": items, "_cost": start_usd + per_item_usd * len(items)}
    return cached("apify", actor, payload, fn)


def apify_jobs(linkedin_url, max_items=JOBS_MAX):
    return apify(ACTOR, {"company": [linkedin_url], "maxItems": max_items, "postedLimit": "month", "sortBy": "date"},
                 per_item_usd=0.001, maxItems=max_items, maxTotalChargeUsd=0.05)


def apify_company(linkedin_url):
    """LinkedIn company page scrape: the source of truth for name/industry/size/HQ. $0.004 per company."""
    return apify("harvestapi~linkedin-company", {"companies": [linkedin_url]}, per_item_usd=0.004, start_usd=0.00005, maxTotalChargeUsd=0.02)


def builtwith(domain):
    """Tech names: 0.14 cr per domain, so one call per domain keeps the cache per domain."""
    resp = deepline("builtwith_domain_lookup", {"domains": [domain], "live_only": True, "hide_text": True,
                                                "no_meta": True, "no_attr": True, "no_pii": True}, backend="builtwith")
    if resp.get("error"):
        return resp
    raw = resp.get("raw") or {}
    errs = get(raw, "Errors", "data.Errors")
    if errs:
        e = errs[0] if isinstance(errs[0], dict) else {"Message": str(errs[0])}
        return {"error": f"builtwith_{e.get('Code')}_{str(e.get('Message', ''))[:40]}"}
    tech = {}
    for res in get(raw, "Results", "data.Results") or []:
        names = {}  # name -> LastDetected epoch ms (None when the provider gives none)
        for path in (res.get("Result") or {}).get("Paths", []):
            for t in path.get("Technologies", []):
                s_ = t.get("Name", "") + (f" ({t['Tag']})" if t.get("Tag") else "")
                if s_:
                    names[s_] = max(names.get(s_) or 0, t.get("LastDetected") or 0) or None
        tech[norm_domain(res.get("Lookup"))] = names
    return {"tech": tech}


def dns_query(domain, rtype):
    """MX -> [(pref, host)], TXT -> [str]. Raises dns.resolver errors; caller maps them to statuses."""
    import dns.resolver  # ponytail: lazy import keeps the module importable without the DNS dependency for the offline self-check
    r = dns.resolver.Resolver()
    r.lifetime = 4.0
    ans = r.resolve(domain, rtype)
    if rtype == "MX":
        return sorted((a.preference, str(a.exchange).rstrip(".").lower()) for a in ans)
    return [b"".join(a.strings).decode(errors="ignore").lower() for a in ans]


def suffix_match(host, table, key):
    best, best_len = None, 0
    for name, entry in table.items():
        for suf in entry[key]:
            if (host == suf or host.endswith("." + suf)) and len(suf) > best_len:
                best, best_len = name, len(suf)
    return best


def classify_mx(hosts, spf):
    """Gateway = first MX host (preference order) matching a gateway suffix; mailbox from MX when direct, else from SPF include."""
    seg = next((v for h in hosts if (v := suffix_match(h, GATEWAY_MAP["gateways"], "suffixes"))), None)
    prov = None if seg else next((p for h in hosts if (p := suffix_match(h, GATEWAY_MAP["mailbox_providers"], "mx_suffixes"))), None)
    if not prov:
        prov = next((p for p, e in GATEWAY_MAP["mailbox_providers"].items() if any(f"include:{i}" in spf for i in e["spf_includes"])), None)
    return {"seg_vendor": seg or ("none" if prov else "unknown"), "mailbox_provider": prov or "other", "mx_hosts": "|".join(hosts)}


def mail_gateway(d):
    """Free: what sits in front of the domain's inbox. nxdomain/no_mx/null_mx are definitive and cached; timeouts are errors and retried next run."""
    def fn(key):
        import dns.exception
        import dns.resolver
        try:
            hosts = [h for _, h in dns_query(d, "MX")]
        except dns.resolver.NXDOMAIN:
            return {"raw": {"status": "nxdomain"}, "_cost": 0.0}
        except dns.resolver.NoAnswer:
            return {"raw": {"status": "no_mx"}, "_cost": 0.0}
        except (dns.exception.Timeout, dns.resolver.NoNameservers, dns.resolver.LifetimeTimeout) as e:
            return {"error": f"dns_{type(e).__name__.lower()}"}
        if hosts in ([""], ["."]):
            return {"raw": {"status": "null_mx", "mx_hosts": ""}, "_cost": 0.0}
        try:
            spf = next((t for t in dns_query(d, "TXT") if t.startswith("v=spf1")), "")
        except Exception:  # SPF is a fallback signal only; any TXT failure just means "no SPF"
            spf = ""
        return {"raw": {"status": "ok", "spf": spf} | classify_mx(hosts, spf), "_cost": 0.0}
    return cached("dns", "mx_lookup", {"domain": d}, fn)


def linkedin_from_site(d):
    """Free and precise: the company's own homepage usually links its LinkedIn page."""
    def fn(key):
        try:
            r = httpx.get(f"https://{d}", follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        except httpx.HTTPError as e:
            return {"error": f"site_{type(e).__name__}"}
        slugs = [m.group(1).rstrip("/").lower() for m in LI_COMPANY.finditer(r.text)] if r.status_code < 400 else []
        return {"slug": slugs[0] if slugs else None, "status": r.status_code}
    return cached("web", "homepage_linkedin", {"domain": d}, fn)


def identify(domains):
    """Free identify lookup: domain -> LinkedIn URL with a confidence score, batched."""
    out = {}
    for i in range(0, len(domains), 100):
        raw = deepline("crustdata_v3_company_identify", {"domains": domains[i:i + 100]}).get("raw")
        for entry in rows_of(raw):
            m = (entry.get("matches") or [None])[0]
            url = m and get(m, "company_data.basic_info.professional_network_url", "professional_network_url")
            if url:
                out[norm_domain(entry.get("matched_on"))] = (url, get(m, "confidence_score", "confidence"))
    return out


def baseline(domains):
    out = {}
    for i in range(0, len(domains), 100):
        chunk = domains[i:i + 100]
        sql = "SELECT * FROM companies WHERE normalized_domain IN (%s) LIMIT 5000" % ",".join("'%s'" % d.replace("'", "") for d in chunk)
        for row in rows_of(deepline("free_simple_company_search", {"sql": sql}).get("raw"), "rows"):
            row = {k.lower(): v for k, v in row.items()}
            d = norm_domain(row.get("normalized_domain") or row.get("domain"))
            prev = out.get(d)
            row["_shared"] = (prev["_shared"] if prev else 0) + 1  # hosted-page domains (notion.so, github.io) have many
            out[d] = row if not prev or (row.get("employee_count") or 0) > (prev.get("employee_count") or 0) else prev | {"_shared": row["_shared"]}
    return out


def enrich_domain(d, titles, base, ident, max_people):
    global CUR_DOMAIN
    CUR_DOMAIN = d
    start = len(SPENT)
    b = base.get(d) or {}
    a = {"domain": d, "company_name": b.get("company_name"), "industry": b.get("industry"), "hq": b.get("location"),
         "employee_count": b.get("employee_count"), "founded_year": b.get("year_founded"), "linkedin_url": b.get("linkedin_url")}
    a["linkedin_url_source"] = "free_table" if a["linkedin_url"] else None
    if a["linkedin_url"] and not a["linkedin_url"].startswith("http"):
        a["linkedin_url"] = "https://www." + a["linkedin_url"].removeprefix("www.")

    mx = mail_gateway(d)  # free DNS, before any paid step: gateway vendor + mailbox provider per company
    if mx.get("error") or (mx.get("raw") or {}).get("status") != "ok":
        a["seg_miss_reason"] = mx.get("error") or (mx.get("raw") or {}).get("status") or "no_mx"
    else:
        a.update({k: mx["raw"][k] for k in ("seg_vendor", "mailbox_provider", "mx_hosts")})

    resp = deepline("crustdata_v3_company_search", {"filters": {"field": "basic_info.primary_domain", "type": "=", "value": d}, "limit": 1})
    rows = rows_of(resp.get("raw"), "companies")
    c = {}
    if resp.get("error") or not rows:
        a["company_miss_reason"] = a["funding_miss_reason"] = resp.get("error") or "no_crustdata_match"
    else:
        c = rows[0]
        pd = norm_domain(get(c, "basic_info.primary_domain"))
        if pd != d:
            a["company_miss_reason"] = f"domain_mismatch:{pd}"
        elif b.get("_shared", 0) > 5:
            a["company_miss_reason"] = f"shared_domain:{b['_shared']}_companies"  # data is for *some* company on this host
        pct = lambda g: round(g, 1) if isinstance(g, (int, float)) else None
        a.update({k: v for k, v in {
            "company_name": get(c, "basic_info.name"), "linkedin_url": get(c, "basic_info.professional_network_url"),
            "linkedin_url_source": "crustdata" if get(c, "basic_info.professional_network_url") else None,
            "website": get(c, "basic_info.website"), "description": get(c, "basic_info.description"),
            "industry": get(c, "taxonomy.professional_network_industry") or ", ".join(get(c, "basic_info.industries") or []) or None,
            "categories": ";".join(get(c, "taxonomy.categories") or []) or None, "company_type": get(c, "basic_info.company_type"),
            "employee_count": get(c, "headcount.total"), "employee_count_range": get(c, "basic_info.employee_count_range"),
            "headcount_growth_12m_pct": pct(get(c, "headcount.growth_percent.12m")),
            "hq": get(c, "locations.headquarters"), "country": get(c, "locations.country"), "founded_year": get(c, "basic_info.year_founded"),
            "revenue_estimate_low_usd": get(c, "revenue.estimated.lower_bound_usd"), "revenue_estimate_high_usd": get(c, "revenue.estimated.upper_bound_usd"),
            "linkedin_followers": get(c, "followers.count"), "investors": ";".join(get(c, "funding.investors") or []) or None,
            "funding_total_usd": get(c, "funding.total_investment_usd"), "last_round_type": get(c, "funding.last_round_type"),
            "last_round_amount_usd": get(c, "funding.last_round_amount_usd"), "last_round_date": get(c, "funding.last_fundraise_date"),
            "openings_count": get(c, "hiring.openings_count"), "headcount_growth_6m_pct": pct(get(c, "headcount.growth_percent.6m")),
            "openings_growth_pct": get(c, "hiring.openings_growth_percent.6m", "hiring.openings_growth_percent.3m"),
            "crustdata_updated_at": (get(c, "metadata.updated_at", "metadata.indexed_at") or "")[:10] or None,
        }.items() if v is not None})
        if a.get("openings_count") is None:
            a["openings_count"] = get(c, "hiring.openings_count")  # keep 0 if it is really 0
    a["company_source"] = "crustdata" if rows else ("free_table" if b else None)

    # funding: the company-search row (free with it, freshest) > funding-rounds lookup by website (0.14 cr, rounds only; its investor attribution is unreliable). A 0 from the first source is a real zero.
    if a.get("funding_total_usd") is not None:
        a["funding_source"] = "crustdata"
    else:
        resp = deepline("aviato_get_company_funding_rounds", {"website": d, "perPage": 20, "page": 0})
        rounds = [r for r in rows_of(resp.get("raw"), "fundingRounds") if r.get("announcedOn")]
        if rounds:
            last = max(rounds, key=lambda r: r["announcedOn"])
            a.update({"funding_source": "aviato", "funding_total_usd": sum(r.get("moneyRaised") or 0 for r in rounds), "last_round_type": last.get("stage"),
                      "last_round_amount_usd": last.get("moneyRaised"), "last_round_date": last["announcedOn"][:10]})
            a.pop("funding_miss_reason", None)
        else:
            a["funding_miss_reason"] = a.get("funding_miss_reason") or resp.get("error") or "not_available"

    aliases = {norm_domain(x) for x in [a.get("website"), *(get(c, "basic_info.all_domains") or [])]}

    # LinkedIn URL resolution, all free: homepage footer link (company's own claim) > batched identify lookup > paid company-search row > free table
    if d in ident:
        a["linkedin_url"], a["linkedin_url_source"] = ident[d][0], f"identify:{ident[d][1]}"
    site = linkedin_from_site(d)
    if site.get("slug"):
        a["linkedin_url"], a["linkedin_url_source"] = f"https://www.linkedin.com/company/{site['slug']}", "homepage"

    # LinkedIn page scrape is the truth for identity/industry/size; the company-search row & free table only fill what it lacks
    if not a.get("linkedin_url"):
        a["linkedin_miss_reason"] = "no_linkedin_url"
    else:
        resp = apify_company(a["linkedin_url"])
        li = (resp.get("items") or [None])[0]
        if resp.get("error") or not li:
            a["linkedin_miss_reason"] = resp.get("error") or "no_linkedin_page"
        else:
            locs = li.get("locations") or []
            hq = next((l for l in locs if l.get("headquarter")), locs[0] if locs else {})
            a.update({k: v for k, v in {
                "company_source": "linkedin", "company_name": li.get("name"), "website": li.get("website"), "tagline": li.get("tagline"),
                "description": li.get("description"), "company_type": li.get("companyType"),
                "industry": "; ".join(i.get("name", "") for i in li.get("industries") or []) or None,
                "specialities": ";".join(li.get("specialities") or []) or None,
                "employee_count": li.get("employeeCount"),
                "employee_count_range": f"{get(li, 'employeeCountRange.start')}-{get(li, 'employeeCountRange.end')}" if get(li, "employeeCountRange.start") else None,
                "hq": ", ".join(x for x in (hq.get("city"), hq.get("geographicArea"), hq.get("country")) if x) or None,
                "country": hq.get("country"), "founded_year": get(li, "foundedOn.year"), "linkedin_followers": li.get("followerCount"),
            }.items() if v is not None})

    resp = builtwith(d)
    t = resp["error"] if resp.get("error") else resp["tech"].get(d, {})
    if isinstance(t, dict) and t:
        cut = (time.time() - STALE_TECH_DAYS * 86400) * 1000
        a["tech_stack"] = ";".join(n for n, ld in t.items() if not ld or ld >= cut) or None
        a["tech_stale"] = ";".join(n for n, ld in t.items() if ld and ld < cut) or None
        seen = [ld for ld in t.values() if ld]
        a["tech_last_detected"] = time.strftime("%Y-%m-%d", time.gmtime(max(seen) / 1000)) if seen else None
    else:
        a["tech_miss_reason"] = t if isinstance(t, str) else ("not_available" if t == {} else "no_builtwith_result")

    if a.get("openings_count") == 0:
        a["jobs_miss_reason"] = "no_openings"
    elif not a.get("linkedin_url"):
        a["jobs_miss_reason"] = "no_linkedin_url"
    else:
        resp = apify_jobs(a["linkedin_url"])
        if resp.get("error"):
            a["jobs_miss_reason"] = resp["error"]
        else:
            want = a["linkedin_url"].lower().rstrip("/")  # the company's LinkedIn URL is a stronger identity than its website (bit.ly links, legacy domains)
            items = [j for j in resp["items"] if (get(j, "company.linkedinUrl") or "").lower().rstrip("/") in ("", want)]  # drop wrong-company hits
            aliases.update(norm_domain(get(j, "company.website")) for j in items)
            total = get(resp["items"][0], "_meta.pagination.totalElements") if resp["items"] else None  # true 30-day count, all postings
            a["jobs_total"] = total if isinstance(total, int) else (f"{len(items)}+" if resp.get("capped", len(resp["items"]) >= JOBS_MAX) else len(items))
            a["jobs_newest_posted"] = max(((j.get("postedDate") or "")[:10] for j in items), default=None) or None
            a["job_titles"] = ";".join(dict.fromkeys(j.get("title") for j in items if j.get("title"))) or None  # newest first, up to JOBS_MAX
            if not items:
                a["jobs_miss_reason"] = "company_mismatch" if resp["items"] else "no_jobs"

    resp = deepline("company_titles", {"domain": d})
    names = [x if isinstance(x, str) else (get(x, "title", "name") or "") for x in rows_of(resp.get("raw"), "titles")]
    names = sorted({n for n in names if n}, key=lambda n: (not match_titles(n, titles), n))  # target matches first
    a["titles_at_company"] = ";".join(names[:50]) or None
    if not names:
        a["titles_miss_reason"] = resp.get("error") or "no_titles"

    # ponytail: link shorteners would pull people from the shortener's own company; extend the tuple when a new one shows up
    aliases = sorted(x for x in aliases | {norm_domain(a.get("website"))} if x and x != d and x not in ("bit.ly", "lnkd.in", "linktr.ee", "t.co"))
    people, scanned, pstart = [], 0, len(SPENT)
    for dom in [d] + aliases:  # alias domains (rebrands, legacy sites) only when nobody is indexed under the input domain; the people search is free
        for page in (1, 2):
            resp = deepline("dropleads_search_people", {"filters": {"companyDomains": [dom], "jobTitles": titles}, "pagination": {"page": page, "limit": 50}})
            leads = rows_of(resp.get("raw"), "leads")
            scanned += len(leads)
            people += [p for p in leads if match_titles(p.get("title"), titles)]
            if resp.get("error") or len(leads) < 50 or len(people) >= max_people:
                break
        if scanned or resp.get("error"):
            break
    uniq = {}
    for p in people:
        uniq.setdefault(p.get("linkedinUrl") or p.get("fullName"), p)  # the people index holds duplicate records per person; keep the first
    people = list(uniq.values())[:max_people]
    a["title_matches"] = len(people)
    if resp.get("error") and scanned == 0:
        a["people_miss_reason"] = resp["error"]
    elif scanned == 0:
        a["people_miss_reason"] = "no_people"
    elif not people:
        a["people_miss_reason"] = "no_title_match"
    rows = [{"domain": d, "company_name": a.get("company_name"), "first_name": p.get("firstName"), "last_name": p.get("lastName"),
             "title": p.get("title"), "linkedin_url": p.get("linkedinUrl"), "source": "dropleads" if dom == d else f"dropleads:{dom}",
             "id": p.get("linkedinUrl") or f"{d}|{p.get('firstName')}|{p.get('lastName')}"} for p in people]
    found_ts = [ts for _, _, _, ts in SPENT[pstart:] if ts]
    for r in rows:  # email finder: 0.34 cr on a verified hit, free on a miss; keyed by the domain the person was found under (rebrands miss on the input domain)
        n = len(SPENT)
        resp = deepline("leadmagic_email_finder", {"first_name": r["first_name"], "last_name": r["last_name"], "domain": dom})
        own_ts = [SPENT[-1][3]] if len(SPENT) > n and SPENT[-1][3] else []
        r["last_enriched_at"] = iso(max(found_ts + own_ts)) if found_ts + own_ts else None  # newest fetch behind this person: their people-search page or email lookup
        em = get(resp, "raw.data") or {}
        if em.get("email"):
            r.update(email=em["email"], email_status=em.get("status"), email_domain=dom, mx_provider=em.get("mx_provider"),
                     mx_security_gateway=em.get("mx_security_gateway"), mx_gateway_type=em.get("mx_gateway_type"), email_verified_at=(em.get("processed_at") or "")[:10])
        else:
            r["email_miss_reason"] = resp.get("error") or "no_email"

    for backend, col in (("deepline", "deepline_cr"), ("apify", "apify_usd"), ("builtwith", "builtwith_cr")):
        a[col] = round(sum(x for bk, x, _, _ in SPENT[start:] if bk == backend), 4)
    fetched = [ts for _, _, _, ts in SPENT[start:] if ts]
    a["data_as_of"] = time.strftime("%Y-%m-%d", time.gmtime(min(fetched))) if fetched else None  # oldest response behind this row
    a["last_enriched_at"] = iso(max(fetched)) if fetched else None  # newest live fetch behind this row; cache-served reruns leave it unchanged
    CUR_DOMAIN = None
    return a, rows


def write_csvs(accounts, people, out_dir):
    for name, cols, data in (("accounts.csv", ACCOUNT_COLS, accounts), ("people.csv", PEOPLE_COLS, people)):
        with open(Path(out_dir) / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, restval="", extrasaction="ignore")
            w.writeheader()
            w.writerows(data)


def main():
    global DRY, MAX_AGE_DAYS
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True, help="CSV with a 'domain' column")
    ap.add_argument("--titles", required=True, help='comma-separated title substrings, e.g. "VP Sales,Head of RevOps"')
    ap.add_argument("--limit", type=int, help="only the first N domains")
    ap.add_argument("--max-people", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true", help="plan calls, touch no network")
    ap.add_argument("--max-age", type=int, metavar="DAYS", help="re-fetch cached responses older than DAYS (re-bills only those); default: cache never expires")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--no-db", action="store_true", help="skip the Supabase upsert even when keys are set")
    args = ap.parse_args()
    DRY, MAX_AGE_DAYS = args.dry_run, args.max_age
    titles = [t.strip() for t in args.titles.split(",") if t.strip()]
    with open(args.input, newline="") as f:
        domains = [norm_domain(r.get("domain")) for r in csv.DictReader(f)]
    domains = list(dict.fromkeys(d for d in domains if d))[: args.limit]
    load_cache()

    base = baseline(domains)
    ident = identify(domains)

    accounts, people = [], []
    for d in domains:
        a, ps = enrich_domain(d, titles, base, ident, args.max_people)
        accounts.append(a)
        people += ps
        print(f"{d}: {a.get('company_name') or '-'} | openings={a.get('openings_count')} | jobs={a.get('jobs_total')} | people={a['title_matches']}", file=sys.stderr)

    if DRY:
        print(f"DRY RUN: {len(domains)} domains, {len(PLANNED)} network calls planned (cached ones excluded):", file=sys.stderr)
        for p in sorted(set(PLANNED)):
            print(f"  {p} x{PLANNED.count(p)}", file=sys.stderr)
        print(f"Estimate per domain: <=0.02 Deepline cr (+0.14 aviato only when crustdata has no funding) + <=${0.001 * (JOBS_MAX + 1):.3f} Apify ({JOBS_MAX} jobs) + ~1 BuiltWith credit + 0.34 cr per verified email (<= --max-people)", file=sys.stderr)
        return
    write_csvs(accounts, people, args.out_dir)
    dl = sum(x for bk, x, c, _ in SPENT if bk == "deepline" and not c)
    ap_usd = sum(x for bk, x, c, _ in SPENT if bk == "apify" and not c)
    bw = sum(x for bk, x, c, _ in SPENT if bk == "builtwith" and not c)
    db = "off" if args.no_db or not (SB_URL and SB_KEY) else db_write(accounts, people, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    print(f"Wrote accounts.csv ({len(accounts)}) and people.csv ({len(people)}). "
          f"Run cost: Deepline {dl:.2f} cr | Apify ${ap_usd:.3f} | BuiltWith {bw:.2f} cr | {sum(1 for _, _, c, _ in SPENT if c)} cached calls | db: {db}", file=sys.stderr)


if __name__ == "__main__":
    main()
