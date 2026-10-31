"""Fetch jobs from public ATS APIs / search APIs, extract pay, check whether a URL is still open."""
import html
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests

UA = {"User-Agent": "job-radar/1.0 (personal job search)"}
TIMEOUT = 20

CLOSED_PHRASES = [
    "no longer accepting", "no longer available", "no longer open", "position has been filled",
    "job has expired", "this job has expired", "applications are closed", "posting has been closed",
    "this position is closed", "job not found", "page not found",
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
def _job(jid, title, company, location, url, posted="", desc="", pay=None):
    return {"id": jid, "title": title or "", "company": company, "location": location or "",
            "url": url or "", "posted": posted or "", "desc": (desc or "")[:3000], "stipend_inr_month": pay}


def greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [_job(f"gh:{slug}:{j['id']}", j["title"], slug.title(), (j.get("location") or {}).get("name"),
                 j["absolute_url"], j.get("updated_at")) for j in data.get("jobs", [])]


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
                        j["hostedUrl"], desc=j.get("descriptionPlain"), pay=pay))
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


def collect_all(cfg):
    """Returns (jobs, ok_sources, failed_sources). Only ATS sources are 'verifiable':
    if a job disappears from a board that loaded fine, it is closed."""
    comp = cfg.get("companies") or {}
    tasks = [(f"gh:{s}", greenhouse, s) for s in comp.get("greenhouse") or []]
    tasks += [(f"lv:{s}", lever, s) for s in comp.get("lever") or []]
    tasks += [(f"ab:{s}", ashby, s) for s in comp.get("ashby") or []]

    def run(t):
        key, fn, arg = t
        try:
            return key, fn(arg), None
        except Exception as e:  # one bad source must never kill the run
            return key, None, f"{type(e).__name__}: {str(e)[:100]}"

    jobs, ok, failed = [], set(), {}
    with ThreadPoolExecutor(8) as ex:
        for key, res, err in ex.map(run, tasks):
            if err:
                failed[key] = err
                continue
            ok.add(key)
            for j in res:
                j.update(src=key, verifiable=True)
                jobs.append(j)

    for name, fn in (("adzuna", adzuna), ("themuse", themuse)):
        conf = cfg.get(name)
        if not conf or (name == "themuse" and not conf.get("enabled")):
            continue
        try:
            res = fn(conf)
            if res is not None:
                ok.add(name)
                for j in res:
                    j.update(src=name, verifiable=False)
                    jobs.append(j)
        except Exception as e:
            failed[name] = f"{type(e).__name__}: {str(e)[:100]}"
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
