"""Filtering (India, graduation batch, pay), scoring (0-10) and optional Gemini screening."""
import json
import os
import re

import requests


def _has(text, kw):
    return re.search(r"(?<![a-z0-9])" + re.escape(str(kw).lower()) + r"(?![a-z0-9])", text) is not None


def _loc_ok(loc, profile):
    locs = profile.get("locations") or []
    if not locs:
        return True
    if not loc:
        return not profile.get("india_only", False)  # unknown location is rejected in India-only mode
    return any(_has(loc, k) for k in locs)  # whole-word match: "Indiana" does not match "india"


def passes_filters(job, profile):
    """Cheap title + location checks (no network)."""
    title = job["title"].lower()
    if any(_has(title, k) for k in profile["exclude_keywords"]):
        return False
    if not any(_has(title, k) for k in profile["include_keywords"]):
        return False
    if profile.get("tech_only", True) and not any(_has(title, k) for k in profile["role_keywords"]):
        return False
    return _loc_ok((job.get("location") or "").lower(), profile)


def batch_years(text):
    """Graduation years a posting restricts itself to, e.g. 'batch of 2027 or 2028' -> {2027, 2028}."""
    t = (text or "").lower()
    years = set()
    for m in re.finditer(r"batch|class of|graduat\w*|pass(?:ing)?[ -]?outs?|passout", t):
        window = t[max(0, m.start() - 35): m.end() + 60]
        years |= {int(y) for y in re.findall(r"(?<!\d)(20[2-3]\d)(?!\d)", window)}
    return years


def passes_batch(job, profile):
    """Drop postings that name graduation years but not yours. No mention = keep."""
    grad = profile.get("graduation_year")
    if not grad:
        return True
    years = batch_years(job["title"] + " " + (job.get("desc") or ""))
    return not years or int(grad) in years


def is_top_company(job, profile):
    name = (job.get("company") or "").lower()
    return any(_has(name, k) for k in (profile.get("pay") or {}).get("top_companies", []))


def passes_pay(job, profile):
    pay = profile.get("pay") or {}
    stipend = job.get("stipend_inr_month")
    if stipend is not None:  # a stated figure always decides
        return stipend >= pay.get("min_stipend_inr_month", 0)
    if pay.get("only_known_good"):  # pay not published: trust only companies on your top list
        return is_top_company(job, profile)
    return True


def heuristic_score(job, profile):
    title = job["title"].lower()
    blob = title + " " + (job.get("desc") or "").lower()
    pay = profile.get("pay") or {}
    s = 2
    if any(_has(title, k) for k in ("intern", "interns", "internship")):
        s += 1
    if any(_has(title, k) for k in profile.get("priority_keywords", [])):
        s += 1
    if _loc_ok((job.get("location") or "").lower(), profile):
        s += 1
    if any(_has(blob, k) for k in profile.get("skills", [])):
        s += 1
    if is_top_company(job, profile):
        s += 2
    stipend = job.get("stipend_inr_month")
    if stipend is not None and stipend >= pay.get("min_stipend_inr_month", 0):
        s += 1
    if profile.get("graduation_year") and int(profile["graduation_year"]) in batch_years(blob):
        s += 2
    if str(profile.get("target_year", "")) in blob:
        s += 1
    return min(10, s)


def llm_enrich(job, profile):
    """Optional: Gemini screens the posting. Returns dict or None."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = (
        "You screen job postings for a B.Tech computer science student in India who graduates in "
        f"{profile.get('graduation_year')} and wants an internship (target year {profile.get('target_year')}). "
        f"Skills: {', '.join(profile.get('skills', []))}.\n"
        f"Posting: title={job['title']!r}, company={job['company']!r}, location={job['location']!r}, "
        f"url={job['url']}, description={job.get('desc', '')[:1800]!r}\n"
        'Reply with JSON only: {"score": 0-10 relevance, "eligible": false only if it clearly needs a PhD or '
        "years of experience, is not an internship/entry-level role, or is restricted to a graduation batch "
        'other than the student\'s, "deadline": "YYYY-MM-DD" only if stated else null, '
        '"reason": "max 15 words"}'
    )
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}},
            timeout=40)
        r.raise_for_status()
        return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:
        print(f"[gemini skipped] {type(e).__name__}: {str(e)[:100]}")
        return None
