"""Watch student hiring programs (hackathons / coding challenges) for the moment registrations open.

Two signals per program, checked on every scan:
  1. Google News RSS (free, no key): a NEW headline naming the program with an 'open / register / apply / launch' word.
  2. Official pages you list: alert when the page starts saying registrations are open, or changes a lot.
The first time a program is seen it is only baselined (no alert), so you are never spammed with old news.
"""
import hashlib
import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from collectors import TIMEOUT, UA
from scoring import batch_years

OPEN_RE = re.compile(r"regist\w*|appl(?:y|ication\w*)|open\w*|launch\w*|announc\w*|enrol\w*|kick[- ]?off|invit\w*")
DONE_RE = re.compile(r"winner|result|shortlist|finalist|closed|concluded|congratulat|ended")
PAGE_OPEN = re.compile(r"(?:registrations?|applications?)\s+(?:are\s+|is\s+)?(?:now\s+)?(?:open|live)"
                       r"|register\s+now|apply\s+now|apply\s+here|last\s+date\s+to\s+(?:register|apply)")
PAGE_CLOSED = re.compile(r"(?:registrations?|applications?)\s+(?:are\s+|is\s+|have\s+been\s+)?(?:now\s+)?closed"
                         r"|no\s+longer\s+accepting")


def news_items(query, days=3):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            pub = None
        link = (it.findtext("link") or "").strip()
        title = (it.findtext("title") or "").strip()
        out.append({"title": title, "link": link, "pub": pub, "id": link or title})
    return out


def page_text(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    if r.status_code >= 400:
        return None
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip().lower()


def batch_note(years, grad_year):
    if years and grad_year and int(grad_year) in years:
        return f"✅ page mentions your batch ({grad_year})"
    if years:
        return f"⚠ page lists batches {', '.join(map(str, sorted(years)))} - not {grad_year}"
    return "batch not stated on the page - check eligibility"


def check_programs(cfg, state, grad_year=None):
    """Returns a list of alerts: {program, kind, text, url, batch}."""
    alerts, now = [], datetime.now(timezone.utc)
    store = state.setdefault("programs", {})
    for p in cfg.get("programs") or []:
        name = p["name"]
        st = store.setdefault(name, {"news_seen": [], "pages": {}, "init": False})
        first = not st["init"]
        cands = []  # everything that fired for this program in this scan

        if p.get("news"):
            try:
                items = news_items(p["news"], p.get("days", 3))
                st.pop("error", None)
            except Exception as e:
                st["error"] = f"{type(e).__name__}: {str(e)[:80]}"
                items = []
            seen = st["news_seen"]
            must = [m.lower() for m in p.get("must_match", [name])]
            also = [m.lower() for m in p.get("also_match", [])]
            for it in items:
                if it["id"] in seen:
                    continue
                seen.append(it["id"])
                t = it["title"].lower()
                if first or not any(m in t for m in must) or (also and not any(m in t for m in also)):
                    continue
                if not OPEN_RE.search(t) or DONE_RE.search(t):
                    continue
                if it["pub"] and now - it["pub"] > timedelta(days=3):
                    continue
                cands.append({"program": name, "kind": "news", "text": it["title"], "url": it["link"],
                              "published": it["pub"].isoformat(timespec="seconds") if it["pub"] else "",
                              "batch": "check the batch on the official page"})
            st["news_seen"] = seen[-300:]

        for url in p.get("urls") or []:
            try:
                txt = page_text(url)
            except Exception:
                txt = None
            if not txt:
                continue
            years = sorted(batch_years(txt))
            cur = {"hash": hashlib.md5(txt.encode()).hexdigest(), "len": len(txt), "years": years,
                   "open": bool(PAGE_OPEN.search(txt)) and not PAGE_CLOSED.search(txt)}
            prev = st["pages"].get(url)
            st["pages"][url] = cur
            if prev is None:
                continue  # baseline
            reason = None
            if cur["open"] and not prev["open"]:
                reason = "the official page now says registrations / applications are open"
            elif cur["hash"] != prev["hash"] and abs(cur["len"] - prev["len"]) >= max(300, 0.2 * prev["len"]):
                reason = "the official page changed a lot (possibly a new edition)"
            if reason:
                cands.append({"program": name, "kind": "page", "text": reason, "url": url, "published": "",
                              "batch": batch_note(years, grad_year)})

        # ONE alert per program: merge this scan's signals, then stay quiet for the cooldown
        if cands:
            cooldown = timedelta(days=p.get("cooldown_days", cfg.get("program_cooldown_days", 30)))
            last = st.get("last_alert")
            if last and now - datetime.fromisoformat(last) < cooldown:
                st["suppressed"] = st.get("suppressed", 0) + len(cands)  # same hiring, already reported
            else:
                best = next((c for c in cands if c["kind"] == "page"), cands[0])  # official page beats a headline
                stamp = now.isoformat(timespec="seconds")
                alerts.append({**best, "more": len(cands) - 1, "detected": stamp})
                st["last_alert"] = stamp
        st["init"] = True
    return alerts
