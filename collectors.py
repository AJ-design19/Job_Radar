"""Fetch jobs from public ATS APIs / search APIs, extract pay, check whether a URL is still open."""
import html
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

UA = {"User-Agent": "job-radar/1.0 (personal job search)"}
TIMEOUT = 20

CLOSED_PHRASES = [
    "no longer accepting", "no longer available", "no longer open", "position has been filled",
    "job has expired", "this job has expired", "applications are closed", "posting has been closed",
    "this position is closed", "job not found", "page not found", "this role has closed", "this listing has closed",
]


def _get(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------- pay helpers (INR only; most Indian intern posts don't list pay) ----------------
def to_month_inr(amount, interval=None):
    """Monthly INR from an amount. Unknown interval: below 2 lakh is read as monthly, above as yearly."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    iv = str(interval or "").lower()
    if "hour" in iv or "day" in iv or "week" in iv:
        return None
    if "month" in iv:
        monthly = amount
    elif any(w in iv for w in ("year", "annual", "annum")):
        monthly = amount / 12
    else:
        monthly = amount if amount < 200000 else amount / 12
    return int(monthly) if monthly >= 1000 else None


_PAY = re.compile(r"(?:stipend|compensation|salary|ctc|remuneration)[^.\n]{0,80}?(?:₹|rs\.?|inr)\s?"
                  r"(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lpa|l)?\b(.{0,30})", re.S)


def parse_pay(text):
    """Find a stated stipend/salary in free text, e.g. 'Stipend: ₹50,000 per month' or 'CTC Rs 12 LPA'."""
    for m in _PAY.finditer((text or "").lower()):
        amt = float(m.group(1).replace(",", ""))
        unit, tail = m.group(2) or "", m.group(3) or ""
        if unit == "k":
            amt *= 1000
        elif unit:
            amt *= 100000
        annual = unit == "lpa" or any(w in tail for w in ("per annum", "/year", "per year", "annum", "p.a", "annual"))
        monthly = amt / 12 if annual else amt
        if monthly >= 1000:
            return int(monthly)
    return None


# ---------------- collectors ----------------
def _iso(v):
    """Posting time as an ISO string. Accepts ISO strings, dates, or epoch seconds / milliseconds."""
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc).isoformat(timespec="seconds")
    return str(v)


def _job(jid, title, company, location, url, posted="", desc="", pay=None, kind="posted"):
    return {"id": jid, "title": title or "", "company": company, "location": location or "",
            "url": url or "", "posted": _iso(posted), "posted_kind": kind, "desc": (desc or "")[:3000], "stipend_inr_month": pay}


def greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [_job(f"gh:{slug}:{j['id']}", j["title"], slug.title(), (j.get("location") or {}).get("name"),
                 j["absolute_url"], j.get("updated_at"), kind="updated") for j in data.get("jobs", [])]


def greenhouse_detail(slug, gid):
    """Full description + pay range for ONE Greenhouse posting (used only for likely matches)."""
    try:
        d = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{gid}",
                 params={"pay_transparency": "true"}).json()
    except Exception:
        return {}
    text = re.sub(r"<[^>]+>", " ", html.unescape(d.get("content") or ""))
    out = {"desc": re.sub(r"\s+", " ", text)[:4000]}
    for r in d.get("pay_input_ranges") or []:
        if str(r.get("currency_type", "")).upper() == "INR":
            pay = to_month_inr((r.get("min_cents") or 0) / 100)
            if pay:
                out["stipend_inr_month"] = pay
                break
    return out


def lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json()
    out = []
    for j in data:
        sr = j.get("salaryRange") or {}
        pay = to_month_inr(sr.get("min"), sr.get("interval")) if str(sr.get("currency", "")).upper() == "INR" else None
        out.append(_job(f"lv:{slug}:{j['id']}", j["text"], slug.title(), (j.get("categories") or {}).get("location"),
                        j["hostedUrl"], j.get("createdAt"), desc=j.get("descriptionPlain"), pay=pay))
    return out


def ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}).json()
    out = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        pay = None
        for c in (j.get("compensation") or {}).get("summaryComponents") or []:
            if c.get("compensationType") == "Salary" and str(c.get("currencyCode", "")).upper() == "INR":
                pay = to_month_inr(c.get("minValue"), c.get("interval"))
                break
        out.append(_job(f"ab:{slug}:{j['id']}", j["title"], slug.title(), j.get("location"), j.get("jobUrl"),
                        j.get("publishedAt"), j.get("descriptionPlain"), pay))
    return out


def adzuna(cfg):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return None
    out, seen = [], set()
    for q in cfg.get("queries", []):
        data = _get(f"https://api.adzuna.com/v1/api/jobs/{cfg.get('country', 'in')}/search/1", params={
            "app_id": app_id, "app_key": app_key, "what": q, "results_per_page": 50,
            "max_days_old": cfg.get("max_days_old", 3), "sort_by": "date"}).json()
        for j in data.get("results", []):
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            real = str(j.get("salary_is_predicted", "0")) != "1" and j.get("salary_min")
            out.append(_job(f"az:{j['id']}", j["title"], (j.get("company") or {}).get("display_name", "?"),
                            (j.get("location") or {}).get("display_name"), j["redirect_url"],
                            j.get("created"), j.get("description"),
                            to_month_inr(j["salary_min"], "year") if real else None))
    return out


def themuse(cfg):
    """The Muse public API: every posting tagged level=Internship (no API key)."""
    out = []
    for page in range(cfg.get("pages", 5)):
        data = _get("https://www.themuse.com/api/public/jobs", params={"level": "Internship", "page": page}).json()
        for j in data.get("results", []):
            locs = ", ".join(l.get("name", "") for l in j.get("locations", []))
            out.append(_job(f"tm:{j['id']}", j.get("name"), (j.get("company") or {}).get("name", "?"), locs,
                            (j.get("refs") or {}).get("landing_page"), j.get("publication_date"),
                            re.sub(r"<[^>]+>", " ", j.get("contents") or "")))
        if page + 1 >= data.get("page_count", 0):
            break
    return out


def internscan(cfg):
    """InternScan (internscan.vercel.app): free, MIT-licensed feed of tech internships scraped daily from the
    official career portals of Google, Microsoft, Meta, NVIDIA, Qualcomm, Walmart and ~60 more employers.
    Jobs that employers pull are removed from the feed, so absence = closed."""
    data = _get(cfg.get("url", "https://internscan.vercel.app/in/api/active.json")).json()
    skip = set(cfg.get("exclude_categories") or [])
    rows = data.get("jobs") or []
    if not rows:
        raise ValueError("empty feed")  # never treat a glitchy empty feed as 'everything closed'
    out = []
    for j in rows:
        if j.get("category") in skip or not j.get("url"):
            continue
        job = _job(f"is:{j['url']}", j.get("title"), j.get("company"), ", ".join(j.get("locations") or []),
                   j["url"], j.get("posted"))
        job["tech_checked"] = True  # InternScan only lists engineering/tech roles
        out.append(job)
    return out


# ---------------- Workday (Visa, HPE, Nvidia, Salesforce... most big employers) ----------------
_WD = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Za-z]{2}/)?([^/?#]+)", re.I)
WD_PAGES = 5  # default: 5 x 20 = 100 postings per site (override with pages: in config)


def _wd_age(text):
    """Workday only says 'Posted 3 Days Ago'. Returns (date or '', older_than_30_days)."""
    t = (text or "").lower()
    if "30+" in t:
        return "", True  # '30+ days ago' is NOT a date: never invent one
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\s*day", t)
        if not m:
            return "", False
        days = int(m.group(1))
    ist = timezone(timedelta(hours=5, minutes=30))
    return (datetime.now(ist).date() - timedelta(days=days)).isoformat(), False


def _wd_posted(text):
    return _wd_age(text)[0]


WD_SEARCHES = ["intern india", "intern bangalore"]  # global lists hide India roles: ask Workday for India directly
LAST_NOTES = {}  # source -> short explanation shown in the report


def workday(entry):
    """entry: a Workday careers URL, or {url, name, searches, pages}. Reads the public job-search endpoint behind the page."""
    e = entry if isinstance(entry, dict) else {"url": entry}
    m = _WD.match(e["url"])
    if not m:
        raise ValueError("not a Workday careers URL")
    tenant, wd, site = m.groups()
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    pages = int(e.get("pages", 3))
    seen, out, notes, capped = set(), [], [], False
    for term in e.get("searches", WD_SEARCHES):
        got = 0
        for page in range(pages):
            r = requests.post(f"{base}/wday/cxs/{tenant}/{site}/jobs", timeout=TIMEOUT,
                              headers={**UA, "Content-Type": "application/json", "Accept": "application/json"},
                              json={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": term})
            r.raise_for_status()
            posts = r.json().get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                got += 1
                posted, old = _wd_age(p.get("postedOn"))
                job = _job(f"wd:{tenant}:{site}:{path}", p.get("title"), e.get("name") or tenant.title(),
                           p.get("locationsText"), f"{base}/en-US/{site}{path}", posted, kind="listed")
                job["old"] = old
                out.append(job)
            if len(posts) < 20:
                break
            if page == pages - 1:
                capped = True  # this search had more results than we read
        notes.append(f"'{term}' {got}")
    LAST_NOTES[f"wd:{tenant}/{site}"] = "searches: " + ", ".join(notes)
    for j in out:
        j["capped"] = capped
    return out


def workday_key(entry):
    m = _WD.match(entry["url"] if isinstance(entry, dict) else entry)
    return f"wd:{m.group(1)}/{m.group(3)}" if m else f"wd:{entry}"


# ---------------- Amazon (amazon.jobs public search; biggest India intern recruiter) ----------------
def _amazon_date(s):
    try:
        return datetime.strptime(" ".join(str(s).split()), "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def amazon(cfg):
    """amazon.jobs search endpoint behind the careers site. Qualifications text is included, so the batch check works.
    Tries several query forms (the site changes them) and uses the first that returns jobs."""
    base_q = cfg.get("query", "intern")
    variants = [
        {"base_query": base_q, "loc_query": "India", "country": "IND"},
        {"base_query": base_q, "normalized_country_code[]": "IND"},
        {"base_query": base_q + " india"},
        {"base_query": base_q, "loc_query": "India"},
    ]
    pages, notes = int(cfg.get("pages", 3)), []
    for v in variants:
        out = []
        for page in range(pages):
            try:
                data = _get("https://www.amazon.jobs/en/search.json",
                            params={**v, "sort": "recent", "result_limit": 100, "offset": page * 100}).json()
            except Exception as e:
                notes.append(f"{type(e).__name__}")
                break
            jobs = data.get("jobs") or []
            if not jobs and page == 0:
                notes.append(f"0 jobs (keys {list(data)[:5]}, hits {data.get('hits')})")
            for j in jobs:
                path = j.get("job_path") or ""
                if not path:
                    continue
                text = " ".join(str(j.get(k) or "") for k in ("basic_qualifications", "description_short", "preferred_qualifications"))
                out.append(_job(f"am:{j.get('id_icims') or path}", j.get("title"), "Amazon",
                                j.get("normalized_location") or j.get("location"), "https://www.amazon.jobs" + path,
                                _amazon_date(j.get("posted_date")), re.sub(r"<[^>]+>", " ", html.unescape(text)),
                                kind="posted"))
            if len(jobs) < 100:
                break
        if out:
            LAST_NOTES["amazon"] = f"query form {variants.index(v) + 1} worked"
            capped = len(out) >= pages * 100
            for j in out:
                j["capped"] = capped
            return out
    raise ValueError("no results from any query form: " + "; ".join(notes)[:200])


def atlassian(cfg):
    """Atlassian's careers listing feed (best effort: shows under Source problems if the address changed)."""
    data = _get(cfg.get("url", "https://www.atlassian.com/endpoint/careers/listings")).json()
    rows = data if isinstance(data, list) else (data.get("jobs") or data.get("listings") or [])
    out = []
    for j in rows:
        jid = j.get("id") or (j.get("portalJobPost") or {}).get("id")
        if not jid:
            continue
        locs = j.get("locations") or j.get("location") or ""
        locs = ", ".join(locs) if isinstance(locs, list) else str(locs)
        out.append(_job(f"at:{jid}", j.get("title"), "Atlassian", locs, f"https://www.atlassian.com/company/careers/details/{jid}",
                        (j.get("portalJobPost") or {}).get("updatedDate"), "", kind="updated"))
    if not out:
        raise ValueError("no listings (feed address changed?)")
    return out


def interndoor_detail(url):
    """Open an InternDoor listing, find the company's own apply link, and read that page (batch, pay)."""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception:
        return {}
    out = {}
    for a in re.finditer(r"(?is)<a\s[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r.text):
        href, label = html.unescape(a.group(1)).strip(), re.sub(r"<[^>]+>", " ", a.group(2))
        if href.startswith("http") and "interndoor.com" not in href and (re.search(r"apply", label, re.I) or "interndoor" in href.lower()):
            out["apply_url"] = href
            break
    if out.get("apply_url"):
        try:
            r2 = requests.get(out["apply_url"], headers=UA, timeout=TIMEOUT, allow_redirects=True)
            if r2.status_code < 400:
                t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r2.text)
                t = re.sub(r"<[^>]+>", " ", t)
                out["desc"] = re.sub(r"\s+", " ", html.unescape(t))[:4000]
        except Exception:
            pass
    return out


# ---------------- InternDoor (India engineering internships, listed within minutes) ----------------
def interndoor(cfg):
    """InternDoor RSS: newest engineering roles in India, incl. employers on Workday / Oracle / Greenhouse."""
    root = ET.fromstring(_get(cfg.get("url", "https://interndoor.com/feed.xml")).content)
    out = []
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = html.unescape(it.findtext("description") or "")
        role, _, company = title.rpartition(" \u2014 ")
        if not role:
            role, company = title, ""
        c = re.search(r"Company:\s*([^\u00b7\n]+)", desc)
        loc = re.search(r"Location:\s*([^\u00b7\n]+)", desc)
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).isoformat(timespec="seconds")
        except Exception:
            pub = ""
        if not link:
            continue
        job = _job(f"id:{link}", role, (c.group(1).strip() if c else company), loc.group(1).strip() if loc else "",
                   link, pub, desc, kind="listed")
        job["role_from_desc"] = True  # generic titles like "Intern - HARMAN": judge the role by the description too
        out.append(job)
    if not out:
        raise ValueError("empty feed")  # a glitchy empty feed must never look like 'everything vanished'
    return out


LAST_STATS = {}  # source -> {fetched, capped} for the latest collect_all (shown in the daily report)


def collect_all(cfg, light=False):
    """Returns (jobs, ok_sources, failed_sources). Only ATS sources are 'verifiable':
    if a job disappears from a board that loaded fine, it is closed."""
    comp = cfg.get("companies") or {}
    tasks = [(f"gh:{s}", greenhouse, s) for s in comp.get("greenhouse") or []]
    tasks += [(f"lv:{s}", lever, s) for s in comp.get("lever") or []]
    tasks += [(f"ab:{s}", ashby, s) for s in comp.get("ashby") or []]
    tasks += [(workday_key(e), workday, e) for e in comp.get("workday") or []]
    if light:  # burst mode: only the fast feeds (company boards and Workday change slowly and are many requests)
        tasks = []

    def run(t):
        key, fn, arg = t
        try:
            return key, fn(arg), None
        except Exception as e:  # one bad source must never kill the run
            return key, None, f"{type(e).__name__}: {str(e)[:100]}"

    jobs, ok, failed = [], set(), {}
    LAST_STATS.clear()
    with ThreadPoolExecutor(8) as ex:
        for key, res, err in ex.map(run, tasks):
            if err:
                failed[key] = err
                continue
            ok.add(key)
            capped = any([j.pop("capped", False) for j in res])  # list cut off: absence proves nothing
            LAST_STATS[key] = {"fetched": len(res), "capped": capped, "note": LAST_NOTES.pop(key, "")}
            for j in res:
                j.update(src=key, verifiable=not capped)
                jobs.append(j)

    for name, fn in (("internscan", internscan), ("amazon", amazon), ("interndoor", interndoor), ("adzuna", adzuna),
                     ("themuse", themuse), ("atlassian", atlassian)):
        conf = cfg.get(name)
        if not conf or conf.get("enabled") is False:
            continue
        try:
            res = fn(conf)
            if res is not None:
                ok.add(name)
                capped = any([j.pop("capped", False) for j in res])
                LAST_STATS[name] = {"fetched": len(res), "capped": capped, "note": LAST_NOTES.pop(name, "")}
                for j in res:
                    j.update(src=name, verifiable=(name in ("internscan", "amazon", "atlassian")) and not capped)
                    jobs.append(j)
        except Exception as e:
            failed[name] = f"{type(e).__name__}: {str(e)[:100]}"

    # the same posting can come from several places: keep the most direct copy (company board > InternScan > InternDoor)
    def key(j):
        return re.sub(r"[^a-z0-9]", "", (j["company"] or "").lower()), re.sub(r"[^a-z0-9]", "", (j["title"] or "").lower())
    second = ("internscan", "interndoor")
    direct = {key(j) for j in jobs if j["src"] not in second}
    jobs = [j for j in jobs if j["src"] not in second or key(j) not in direct]
    scanned = {key(j) for j in jobs if j["src"] == "internscan"}
    jobs = [j for j in jobs if j["src"] != "interndoor" or key(j) not in scanned]
    return jobs, ok, failed


def check_url(url, closed_phrases=None, must_contain=None):
    """Returns ('open'|'closed'|'unknown', note)."""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    except Exception:
        return "unknown", "network error"
    if r.status_code in (404, 410):
        return "closed", f"HTTP {r.status_code}"
    if r.status_code >= 400:
        return "unknown", f"HTTP {r.status_code}"
    if "error=true" in r.url:  # Greenhouse redirects closed postings to the board with ?error=true
        return "closed", "redirected to board (posting removed)"
    text = r.text.lower()
    for p in (closed_phrases or []) + CLOSED_PHRASES:
        if p.lower() in text:
            return "closed", f'page says "{p}"'
    if must_contain and not all(m.lower() in text for m in must_contain):
        return "closed", "expected text not found on page"
    return "open", "page reachable"
