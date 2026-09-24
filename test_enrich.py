"""Offline self-check: uv run python test_enrich.py"""
import os
import tempfile
from pathlib import Path

import enrich as e

e.SB_URL, e.SB_KEY = "", None  # never touch a real database from the self-check

assert e.norm_domain("https://www.Stripe.com/about?x=1") == "stripe.com"
assert e.norm_domain(None) == "" and e.norm_domain(" acme.io/ ") == "acme.io"
assert e.match_titles("Senior VP of Sales, EMEA", ["vp sales", "VP of Sales"])
assert not e.match_titles(None, ["CEO"]) and not e.match_titles("Engineer", ["Sales"])
assert e.rows_of({"companies": [{"a": 1}]}) == [{"a": 1}] and e.rows_of([1]) == [1] and e.rows_of({"x": 1}) == []
assert e.rows_of({"result": {"data": {"rows": [1, 2]}}}) == [1, 2]
assert e.rows_of({"data": {"status": "ok", "output": {"titles": ["a"]}}}, "titles") == ["a"]
assert e.get({"a": {"b": 0}}, "a.b") == 0 and e.get({"a": {}}, "a.b", "c") is None

# mail gateway classification from MX hosts (+ SPF fallback for a mailbox behind a gateway); DNS failures are errors, never cached
assert e.classify_mx(["mx0a-00123.pphosted.com", "mx0b-00123.pphosted.com"], "v=spf1 include:spf.protection.outlook.com -all") == {"seg_vendor": "proofpoint", "mailbox_provider": "microsoft", "mx_hosts": "mx0a-00123.pphosted.com|mx0b-00123.pphosted.com"}
assert e.classify_mx(["aspmx.l.google.com"], "") == {"seg_vendor": "none", "mailbox_provider": "google", "mx_hosts": "aspmx.l.google.com"}
assert e.classify_mx(["mail.selfhosted.io"], "")["seg_vendor"] == "unknown" and e.classify_mx(["mail.selfhosted.io"], "")["mailbox_provider"] == "other"

# cache: second call with same payload never invokes fn; errors are not cached
e.CACHE_PATH = Path(tempfile.mkdtemp()) / "calls.jsonl"
calls = []
fn = lambda key: (calls.append(key), {"raw": {"rows": [1]}, "_cost": 0.02})[1]
assert e.cached("deepline", "t", {"x": 1}, fn)["raw"] == {"rows": [1]}
assert e.cached("deepline", "t", {"x": 1}, fn)["raw"] == {"rows": [1]} and len(calls) == 1
assert [x[:3] for x in e.SPENT[-2:]] == [("deepline", 0.02, False), ("deepline", 0.02, True)] and e.SPENT[-1][3] > 0
assert [r["from_cache"] for r in e.RAW[-2:]] == [False, True] and e.RAW[-1]["cache_key"] == e.RAW[-2]["cache_key"] and e.RAW[-1]["tool"] == "t" and e.RAW[-1]["fetched_at"].endswith("Z")
# --max-age: a record older than the limit is re-fetched, then served again
import time
rec = next(r for r in e.CACHE.values() if r["id"] == "t" and r["resp"]["raw"] == {"rows": [1]}); rec["ts"] = time.time() - 2 * 86400
e.MAX_AGE_DAYS = 1
e.cached("deepline", "t", {"x": 1}, fn); assert len(calls) == 2
e.cached("deepline", "t", {"x": 1}, fn); assert len(calls) == 2
e.MAX_AGE_DAYS = None
e.cached("deepline", "t", {"x": 2}, lambda k: {"error": "http_401"})
e.cached("deepline", "t", {"x": 2}, lambda k: (calls.append("again"), {"error": "http_401"})[1])
assert calls[-1] == "again"
assert not any(r["payload"] == {"x": 2} for r in e.RAW)  # errors are neither cached nor mirrored to the DB

# enrich_domain with fake backends
CRUST = {"companies": [{"metadata": {"updated_at": "2026-09-24T12:00:00Z"}, "basic_info": {"name": "Acme", "primary_domain": "acme.com", "professional_network_url": "https://www.linkedin.com/company/acme",
          "industries": ["software"], "year_founded": 2015}, "headcount": {"total": 120}, "locations": {"headquarters": "Austin, TX"},
          "funding": {"total_investment_usd": 5e6, "last_round_type": "Series A", "last_round_amount_usd": 5e6, "last_fundraise_date": "2025-01-02"},
          "hiring": {"openings_count": 3, "openings_growth_percent": {"6m": 50.0}}, "taxonomy": {"professional_network_industry": "Software", "categories": ["B2B", "SaaS"]},
          "revenue": {"estimated": {"lower_bound_usd": 1, "upper_bound_usd": 2}}, "followers": {"count": 9}}]}
LEADS = {"leads": [{"firstName": "Ann", "lastName": "Lee", "title": "VP Sales", "linkedinUrl": "li/ann"}, {"firstName": "Ann", "lastName": "Lee", "title": "Vp Sales", "linkedinUrl": "li/ann"},
                   {"firstName": "Bob", "lastName": "Ray", "title": "Engineer"}]}
BW = {"data": {"Results": [{"Lookup": "acme.com", "Result": {"Paths": [{"Technologies": [{"Name": "React", "Tag": "js"}, {"Name": "React", "Tag": "js"}, {"Name": "jQuery", "LastDetected": 1000000000000}]}]}}]}}
AVIATO = {"data": {"fundingRounds": [{"announcedOn": "2020-02-25T00:00:00.000Z", "moneyRaised": 306066, "stage": "Seed"}, {"announcedOn": "2022-05-04T00:00:00.000Z", "moneyRaised": 14300000, "stage": "Series A"}]}}
EMAIL = {"data": {"email": "ann@acme.com", "status": "valid", "mx_provider": "google workspace", "mx_security_gateway": False, "mx_gateway_type": "Cloud Mailbox Host", "processed_at": "2026-09-25T01:02:03Z"}}
def fake_deepline(tool, payload, backend="deepline"):
    return {"raw": {"crustdata_v3_company_search": CRUST, "company_titles": {"titles": ["VP Sales", "Engineer"]},
                    "dropleads_search_people": LEADS, "builtwith_domain_lookup": BW, "aviato_get_company_funding_rounds": AVIATO, "leadmagic_email_finder": EMAIL}[tool]}
e.deepline = fake_deepline
e.apify_company = lambda url: {"items": [{"name": "Acme Inc", "employeeCount": 130, "employeeCountRange": {"start": 51, "end": 200},
    "industries": [{"name": "Software Development"}], "foundedOn": {"year": 2014}, "followerCount": 10, "companyType": "Privately Held",
    "locations": [{"city": "Denver", "geographicArea": "Colorado", "country": "US"}, {"city": "Austin", "geographicArea": "Texas", "country": "US", "headquarter": True}]}]}
e.linkedin_from_site = lambda d: {"slug": "acme-inc"} if d == "acme.com" else {"slug": None}
e.mail_gateway = lambda d: {"raw": {"status": "ok", "seg_vendor": "mimecast", "mailbox_provider": "google", "mx_hosts": "us-smtp-inbound-1.mimecast.com"}}
LI = "https://www.linkedin.com/company/acme-inc"
e.apify_jobs = lambda url, max_items=10: {"items": [{"title": "AE", "postedDate": "2026-09-01T00:00:00Z", "company": {"linkedinUrl": LI + "/", "website": "https://acme-legacy.io/?utm=x"}, "_meta": {"pagination": {"totalElements": 41}}},
    {"title": "X", "company": {"linkedinUrl": "https://www.linkedin.com/company/other", "website": "https://acme.com"}}], "capped": True}
a, ps = e.enrich_domain("acme.com", ["vp sales"], {}, {"acme.com": ("https://www.linkedin.com/company/acme", 0.9)}, 5)
assert a["linkedin_url"] == "https://www.linkedin.com/company/acme-inc" and a["linkedin_url_source"] == "homepage"
assert a["company_name"] == "Acme Inc" and a["employee_count"] == 130 and a["hq"] == "Austin, Texas, US" and a["company_source"] == "linkedin"
assert a["employee_count_range"] == "51-200" and a["founded_year"] == 2014 and a["industry"] == "Software Development" and a["categories"] == "B2B;SaaS" and a["revenue_estimate_high_usd"] == 2 and a["linkedin_followers"] == 10
assert a["funding_total_usd"] == 5e6 and a["funding_source"] == "crustdata" and a["openings_count"] == 3 and a["openings_growth_pct"] == 50.0
assert a["jobs_total"] == 41 and a["job_titles"] == "AE" and a["jobs_newest_posted"] == "2026-09-01"
assert a["tech_stack"] == "React (js)" and a["tech_stale"] == "jQuery" and a["tech_last_detected"] == "2001-09-09" and a["crustdata_updated_at"] == "2026-09-24"
assert a["titles_at_company"] == "VP Sales;Engineer" and a["title_matches"] == 1
assert a["seg_vendor"] == "mimecast" and a["mailbox_provider"] == "google" and "seg_miss_reason" not in a
assert ps == [{"domain": "acme.com", "company_name": "Acme Inc", "first_name": "Ann", "last_name": "Lee", "title": "VP Sales", "linkedin_url": "li/ann", "source": "dropleads",
               "id": "li/ann", "last_enriched_at": None, "email": "ann@acme.com", "email_status": "valid", "email_domain": "acme.com", "mx_provider": "google workspace", "mx_security_gateway": False, "mx_gateway_type": "Cloud Mailbox Host", "email_verified_at": "2026-09-25"}]
assert not any(k.endswith("miss_reason") for k in a)
a2, _ = e.enrich_domain("acme.com", ["vp sales"], {"acme.com": {"_shared": 40}}, {}, 5)
assert a2["company_miss_reason"] == "shared_domain:40_companies" and a2["company_name"] == "Acme Inc"
a3, _ = e.enrich_domain("other.com", ["vp sales"], {}, {"other.com": ("https://www.linkedin.com/company/other", 0.5)}, 5)
assert a3["linkedin_url_source"] == "identify:0.5"
assert a3["company_miss_reason"] == "domain_mismatch:acme.com" and a3["company_name"] == "Acme Inc"

# through the real cache layer: RAW rows carry the domain, last_enriched_at is the newest fetch, a person without a LinkedIn URL still gets a stable id
e.RAW.clear()
e.deepline = lambda tool, payload, backend="deepline": e.cached(backend, tool, payload, lambda k: fake_deepline(tool, payload) | {"_cost": 0.0})
import copy
NOURL = copy.deepcopy(LEADS); del NOURL["leads"][0]["linkedinUrl"]; del NOURL["leads"][1]["linkedinUrl"]

a7, ps7 = e.enrich_domain("acme.com", ["vp sales"], {}, {}, 5)
assert {r["domain"] for r in e.RAW} == {"acme.com"} and {r["tool"] for r in e.RAW} >= {"crustdata_v3_company_search", "dropleads_search_people", "leadmagic_email_finder"}
assert a7["last_enriched_at"] == e.iso(max(ts for _, _, _, ts in e.SPENT if ts)) and a7["data_as_of"] == a7["last_enriched_at"][:10]
assert ps7[0]["last_enriched_at"] == a7["last_enriched_at"] and e.CUR_DOMAIN is None
LEADS["leads"][:] = NOURL["leads"]
_, ps8 = e.enrich_domain("acme.com", ["vp sales"], {}, {}, 5)
assert ps8[0]["id"] == "acme.com|Ann|Lee" and ps8[0]["linkedin_url"] is None
LEADS["leads"][:] = [{"firstName": "Ann", "lastName": "Lee", "title": "VP Sales", "linkedinUrl": "li/ann"}, {"firstName": "Ann", "lastName": "Lee", "title": "Vp Sales", "linkedinUrl": "li/ann"}, {"firstName": "Bob", "lastName": "Ray", "title": "Engineer"}]
e.deepline = fake_deepline

# db_write: companies before people before raw, only schema columns, blanks -> NULL, raw deduped by key; db_upsert is a no-op without keys
assert e.db_upsert("companies", [{"domain": "x"}], "domain") == 0
writes = []
e.db_upsert = lambda table, rows, on_conflict: (writes.append((table, rows, on_conflict)), len(rows))[1]
e.RAW[:] = [{"cache_key": "k1", "domain": "acme.com", "tool": "t"}, {"cache_key": "k1", "domain": "acme.com", "tool": "t"}, {"cache_key": "k2", "domain": None, "tool": "free_simple_company_search"}]
assert e.db_write([a7 | {"tech_stale": "", "junk": 1}], ps7, "r1") == "companies 1, people 1, raw 2"
assert [w[0] for w in writes] == ["companies", "people", "raw_responses"] and [w[2] for w in writes] == ["domain", "id", "cache_key"]
crow = writes[0][1][0]; assert set(crow) == (set(e.ACCOUNT_COLS) | {"last_enriched_at", "run_id"}) - e.DB_HIDE and crow["tech_stale"] is None and crow["run_id"] == "r1"
prow = writes[1][1][0]; assert set(prow) == (set(e.PEOPLE_COLS) | {"id", "last_enriched_at", "run_id"}) - e.DB_HIDE and prow["id"] == "li/ann" and prow["domain"] == "acme.com"
assert "funding_source" not in crow and "deepline_cr" not in crow and "source" not in prow  # provider names never reach the demo tables
assert [r["cache_key"] for r in writes[2][1]] == ["k1", "k2"] and writes[2][1][0]["run_id"] == "r1"

# funding fallback: company record has no funding block -> funding rounds by website; a 0 in the company record is a real zero and never falls back
import copy
NOFUND = copy.deepcopy(CRUST); del NOFUND["companies"][0]["funding"]
e.deepline = lambda tool, payload, backend="deepline": {"raw": NOFUND} if tool == "crustdata_v3_company_search" else fake_deepline(tool, payload)
a5, _ = e.enrich_domain("acme.com", ["vp sales"], {}, {}, 5)
assert a5["funding_source"] == "aviato" and a5["funding_total_usd"] == 14606066 and a5["last_round_type"] == "Series A" and a5["last_round_date"] == "2022-05-04" and "funding_miss_reason" not in a5
ZERO = copy.deepcopy(CRUST); ZERO["companies"][0]["funding"] = {"total_investment_usd": 0}
e.deepline = lambda tool, payload, backend="deepline": {"raw": ZERO} if tool == "crustdata_v3_company_search" else (_ for _ in ()).throw(AssertionError("aviato must not run on 0")) if tool.startswith("aviato") else fake_deepline(tool, payload)
a6, _ = e.enrich_domain("acme.com", ["vp sales"], {}, {}, 5)
assert a6["funding_total_usd"] == 0 and a6["funding_source"] == "crustdata"
e.deepline = fake_deepline

# people fallback: nobody indexed under the input domain -> try alias domains (company record all_domains, job-company website), never shorteners
CRUST["companies"][0]["basic_info"]["all_domains"] = ["acme.com", "bit.ly", "acme-old.com"]
seen = []
def fake_dropleads(tool, payload, backend="deepline"):
    if tool == "leadmagic_email_finder":
        seen.append("email:" + payload["domain"]); return {"raw": {"data": {"email": None, "message": "not found"}}}
    if tool != "dropleads_search_people":
        return fake_deepline(tool, payload)
    dom = payload["filters"]["companyDomains"][0]; seen.append(dom)
    return {"raw": LEADS if dom == "acme-old.com" else {"leads": []}}
e.deepline = fake_dropleads
a4, ps4 = e.enrich_domain("acme.com", ["vp sales"], {}, {}, 5)
assert seen == ["acme.com", "acme-legacy.io", "acme-old.com", "email:acme-old.com"], seen  # email keyed by the alias, miss is free and carries a reason
assert a4["title_matches"] == 1 and ps4[0]["source"] == "dropleads:acme-old.com" and "people_miss_reason" not in a4
assert ps4[0]["email_miss_reason"] == "no_email" and "email" not in ps4[0]

# misses carry reasons; zero openings skips the paid jobs call
e.deepline = lambda tool, payload, backend="deepline": {"error": "http_401"} if tool != "crustdata_v3_company_search" else {"raw": {"companies": [{"basic_info": {"name": "Z"}, "hiring": {"openings_count": 0}}]}}
e.apify_jobs = lambda *a, **k: (_ for _ in ()).throw(AssertionError("apify must not be called"))
e.mail_gateway = lambda d: {"error": "dns_lifetimetimeout"}
a, ps = e.enrich_domain("z.com", ["ceo"], {}, {}, 5)
assert a["seg_miss_reason"] == "dns_lifetimetimeout" and "seg_vendor" not in a
assert a["jobs_miss_reason"] == "no_openings" and a["funding_miss_reason"] == "http_401" and a["linkedin_miss_reason"] == "no_linkedin_url"
assert a["tech_miss_reason"] == "http_401" and a["titles_miss_reason"] == "http_401" and a["people_miss_reason"] == "http_401"
assert ps == [] and a["title_matches"] == 0

# csv writer ignores unknown keys and fills blanks
d = tempfile.mkdtemp()
e.write_csvs([a | {"junk": 1}], ps, d)
head = Path(d, "accounts.csv").read_text().splitlines()
assert head[0] == ",".join(e.ACCOUNT_COLS) and head[1].startswith("z.com,crustdata,Z,")
print("ok")
