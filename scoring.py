"""Filtering, scoring (0-10) and optional Gemini screening."""
import json
import os
import re

import requests


def _has(text, kw):
    return re.search(r"(?<![a-z0-9])" + re.escape(str(kw).lower()) + r"(?![a-z0-9])", text) is not None


def passes_filters(job, profile):
    title = job["title"].lower()
    if any(_has(title, k) for k in profile["exclude_keywords"]):
        return False
    if not any(_has(title, k) for k in profile["include_keywords"]):
        return False
    if profile.get("tech_only", True) and not any(_has(title, k) for k in profile["role_keywords"]):
        return False
    loc, locs = (job.get("location") or "").lower(), profile.get("locations") or []
    return not (loc and locs and not any(str(k).lower() in loc for k in locs))


def heuristic_score(job, profile):
    title = job["title"].lower()
    blob = title + " " + (job.get("desc") or "").lower()
    loc = (job.get("location") or "").lower()
    s = 3
    if any(_has(title, k) for k in profile.get("priority_keywords", [])):
        s += 2
    if any(_has(title, k) for k in ("intern", "interns", "internship")):
        s += 1
    if loc and any(str(k).lower() in loc for k in profile.get("locations", [])):
        s += 1
    s += min(2, sum(1 for k in profile.get("skills", []) if _has(blob, k)))
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
        "You screen job postings for a B.Tech computer science student in India seeking internships. "
        f"Skills: {', '.join(profile.get('skills', []))}. Target year: {profile.get('target_year')}.\n"
        f"Posting: title={job['title']!r}, company={job['company']!r}, location={job['location']!r}, "
        f"url={job['url']}, description={job.get('desc', '')[:1200]!r}\n"
        'Reply with JSON only: {"score": 0-10 relevance, "eligible": false only if it clearly needs a PhD or '
        'years of experience or is not an internship/entry-level role, "deadline": "YYYY-MM-DD" only if '
        'stated else null, "reason": "max 15 words"}'
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
