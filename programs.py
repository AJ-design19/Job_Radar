"""Watch student hiring programs (hackathons / coding challenges) for the moment registrations open.

Two signals per program, checked on every scan:
  1. Google News RSS (free, no key): a NEW headline naming the program with an 'open / register / apply / launch' word.
  2. Official pages you list: alert when the page starts saying registrations are open, or changes a lot.
The first time a program is seen it is only baselined (no alert), so you are never spammed with old news.
"""
import difflib
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

try:  # turns a Google News redirect into the real article address; optional, the news link is kept if it is missing
    from googlenewsdecoder import gnewsdecoder
except Exception:
    gnewsdecoder = None

_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE = (r"(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:" + _MON + r")[a-z]*\.?,?\s+20\d\d"
         r"|(?:" + _MON + r")[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d\d)")
DEADLINE_RE = re.compile(r"(?:last\s+date|deadline|apply\s+(?:by|before)|registrations?\s+(?:close|end)s?(?:\s+on)?"
                         r"|closes?\s+on)[^.\n]{0,60}?(" + _DATE + ")", re.I)

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
        src = it.find("source")
        out.append({"title": title, "link": link, "pub": pub, "id": link or title,
                    "source": (src.text or "").strip() if src is not None else ""})
    return out


def fetch_page(url):
    """Returns (visible text lowercased, register/apply links found on the page)."""
    r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    if r.status_code >= 400:
        return None, []
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
    t = re.sub(r"<[^>]+>", " ", t)
    text = re.sub(r"\s+", " ", html.unescape(t)).strip().lower()
    links = []
    for m in re.finditer(r"(?is)<a\s[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r.text):
        href, label = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2))
        if re.search(r"regist|apply|sign\s*up|enrol|participate", label, re.I) or \
                re.search(r"unstop|dare2compete|rsvp\.withgoogle|hirepro", href, re.I):
            full = urllib.parse.urljoin(r.url, html.unescape(href))
            if full.startswith("http") and full not in links:
                links.append(full)
    return text, links[:3]


def batch_note(years, grad_year):
    if years and grad_year and int(grad_year) in years:
        return f"✅ page mentions your batch ({grad_year})"
    if years:
        return f"⚠ page lists batches {', '.join(map(str, sorted(years)))} - not {grad_year}"
    return "batch not stated on the page - check eligibility"


def _deadline(text):
    m = DEADLINE_RE.search(text or "")
    if not m:
        return ""
    d = m.group(1).title()
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda x: x.group(1) + x.group(2).lower(), d)


def resolve_link(url):
    """Google News redirect -> real article address (best effort)."""
    if not gnewsdecoder or "news.google.com" not in url:
        return url
    try:
        r = gnewsdecoder(url, interval=1)
        if r.get("status") and r.get("decoded_url"):
            return r["decoded_url"]
    except Exception:
        pass
    return url


def enrich(a, grad_year):
    """Resolve the news link, then read the article: register/apply links, batch check and deadline."""
    link = resolve_link(a["url"])
    out = {**a, "url": link}
    if link != a["url"]:
        out["google_url"] = a["url"]
    if "news.google.com" in link:
        return out
    try:
        text, links = fetch_page(link)
    except Exception:
        return out
    if text:
        out["batch"] = batch_note(sorted(batch_years(text)), grad_year)
        out["deadline"] = out.get("deadline") or _deadline(text)
        out["links"] = list(dict.fromkeys(links + list(out.get("links") or [])))[:4]
    return out


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
                              "via": it.get("source", ""), "links": [], "official": p.get("urls") or [],
                              "batch": "check the batch on the official page"})
            st["news_seen"] = seen[-300:]

        for url in p.get("urls") or []:
            try:
                txt, links = fetch_page(url)
            except Exception:
                txt, links = None, []
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
                              "deadline": _deadline(txt), "links": links, "official": p.get("urls") or [],
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
                alerts.append(enrich({**best, "more": len(cands) - 1, "detected": stamp}, grad_year))
                st["last_alert"] = stamp
        st["init"] = True
    return alerts


# ---------------------------------------------------------------------------------------------
# DISCOVERY: keep searching the news for opportunities you did NOT list, and alert on new ones.
# ---------------------------------------------------------------------------------------------
def _words(ws):
    return re.compile(r"\b(?:" + "|".join(ws) + r")", re.I)


_GENERIC = set("""internship internships intern interns applications application apply applying students student engineering
india indian open opens opened launch launches launched registrations registration register summer batch hiring program
programme programs opportunity opportunities announced announces challenge hackathon fellowship software for the and with from
their more your will now new engineers engineer 2026 2027 2028 2029 window apply application another startup
launches latest update news details know everything complete guide eligible eligibility last date how can open""".split())


def _story_tokens(t):
    return {w for w in re.findall(r"[a-z0-9]{4,}", t) if w not in _GENERIC}


def _same_story(norm, tokens, recent):
    for r in recent:
        if difflib.SequenceMatcher(None, norm, r["t"]).ratio() > 0.7:
            return True
        rt = set(r["k"])
        if tokens and rt and (len(tokens & rt) / len(tokens | rt) >= 0.6
                              or len(tokens & rt) / min(len(tokens), len(rt)) >= 0.8):  # one story is a subset of the other
            return True
    return False


def discover(cfg, state, grad_year=None):
    dcfg = cfg.get("discovery") or {}
    if not dcfg.get("enabled"):
        return []
    st = state.setdefault("discovery", {"seen": [], "titles": [], "init": False, "day": "", "count": 0})
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if st["day"] != today:
        st["day"], st["count"] = today, 0
    first = not st["init"]
    kinds = _words([r"interns?\b", r"internships?\b", r"hackathons?\b", r"challenges?\b", r"fellowships?\b",
                    r"summer analyst", r"codeathon", r"hiring contest"])
    actions = OPEN_RE
    relevant = _words([r"india\w*", r"students?\b", r"2027", r"2028", r"freshers?\b", r"b\.?tech", r"engineering",
                       r"graduates?\b"])
    tech = _words([r"software", r"engineer\w*", r"tech\w*", r"coding", r"developer", r"data", r"\bai\b", r"cyber",
                   r"cloud", r"machine learning", r"computer", r"hackathon", r"programming"])
    bad = re.compile("|".join(re.escape(w) for w in (dcfg.get("exclude") or [])), re.I) if dcfg.get("exclude") else None
    tracked = [m.lower() for p in cfg.get("programs") or [] for m in p.get("must_match", [p["name"]])]
    found = []
    for q in dcfg.get("queries") or []:
        try:
            items = news_items(q, dcfg.get("days", 2))
        except Exception:
            continue
        for it in items:
            if it["id"] in st["seen"]:
                continue
            st["seen"].append(it["id"])
            if first:
                continue
            title = it["title"]
            if it.get("source") and title.endswith(" - " + it["source"]):
                title = title[: -len(it["source"]) - 3]
            t = title.lower()
            if not (kinds.search(t) and actions.search(t) and relevant.search(t) and tech.search(t)):
                continue
            if DONE_RE.search(t) or (bad and bad.search(t)) or any(m in t for m in tracked):
                continue
            years = batch_years(title)
            if years and grad_year and int(grad_year) not in years:
                continue
            if it["pub"] and now - it["pub"] > timedelta(days=3):
                continue
            norm = re.sub(r"[^a-z0-9 ]", "", t)
            tokens = _story_tokens(t)
            recent = [r for r in st["titles"] if isinstance(r, dict) and now - datetime.fromisoformat(r["at"]) < timedelta(days=3)]
            if _same_story(norm, tokens, recent):
                continue  # same story already reported (other outlets repeat it, worded differently)
            st["titles"].append({"t": norm, "k": sorted(tokens), "at": now.isoformat(timespec="seconds")})
            found.append({"program": "New opportunity", "kind": "discovery", "text": title, "url": it["link"],
                          "via": it.get("source", ""), "published": it["pub"].isoformat(timespec="seconds") if it["pub"] else "",
                          "links": [], "official": [], "batch": "check the batch on the official page"})
    st["seen"], st["titles"], st["init"] = st["seen"][-600:], st["titles"][-150:], True
    room = max(0, int(dcfg.get("max_per_day", 12)) - st["count"])
    found = found[: min(room, int(dcfg.get("max_per_run", 5)))]
    st["count"] += len(found)
    stamp = now.isoformat(timespec="seconds")
    return [enrich({**a, "more": 0, "detected": stamp}, grad_year) for a in found]
