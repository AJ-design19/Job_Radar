"""Fetch jobs from public ATS APIs / search API and check whether a URL is still open."""
import os
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


def _job(jid, title, company, location, url, posted="", desc=""):
    return {"id": jid, "title": title or "", "company": company, "location": location or "",
            "url": url or "", "posted": posted or "", "desc": (desc or "")[:1500]}


def greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json()
    return [_job(f"gh:{slug}:{j['id']}", j["title"], slug.title(), (j.get("location") or {}).get("name"),
                 j["absolute_url"], j.get("updated_at")) for j in data.get("jobs", [])]


def lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json()
    return [_job(f"lv:{slug}:{j['id']}", j["text"], slug.title(), (j.get("categories") or {}).get("location"),
                 j["hostedUrl"], desc=j.get("descriptionPlain")) for j in data]


def ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    return [_job(f"ab:{slug}:{j['id']}", j["title"], slug.title(), j.get("location"), j.get("jobUrl"),
                 j.get("publishedAt"), j.get("descriptionPlain"))
            for j in data.get("jobs", []) if j.get("isListed", True)]


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
            out.append(_job(f"az:{j['id']}", j["title"], (j.get("company") or {}).get("display_name", "?"),
                            (j.get("location") or {}).get("display_name"), j["redirect_url"],
                            j.get("created"), j.get("description")))
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

    if cfg.get("adzuna"):
        try:
            res = adzuna(cfg["adzuna"])
            if res is not None:
                ok.add("adzuna")
                for j in res:
                    j.update(src="adzuna", verifiable=False)
                    jobs.append(j)
        except Exception as e:
            failed["adzuna"] = f"{type(e).__name__}: {str(e)[:100]}"
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
