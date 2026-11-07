#!/usr/bin/env python3
"""Job Radar.   python agent.py scan | digest | test"""
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import collectors as _collectors
from programs import check_programs, discover
from collectors import check_url, collect_all, greenhouse_detail, parse_pay
from notify import (alert_email, digest_email, digest_push_text, push, send_email, send_telegram,
                    send_whatsapp, telegram_job_text, alert_markdown, digest_markdown, program_email,
                    program_markdown, program_push_text, fmt_dt, opportunities_markdown)
from scoring import heuristic_score, is_elite, llm_enrich, passes_batch, passes_filters, passes_pay

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state" / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: no tz database needed (works on Windows)
MAX_DETAIL_CALLS = 60   # Greenhouse posting-detail fetches per run (description + pay)
MAX_LLM_CALLS = 15      # Gemini screenings per run
MAX_URL_CHECKS = 15     # open/closed page checks per digest for search-API jobs


def fingerprint(j):
    """Same company + same title = same hiring, even if the board re-posts it under a new id."""
    import re
    return "|".join(re.sub(r"[^a-z0-9]", "", (j.get(k) or "").lower()) for k in ("company", "title"))


def posted_dt(s):
    """Posting time as an aware datetime, or None if unknown."""
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"initialized": False, "jobs": {}, "watch": {}, "failures": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def check_watch(cfg, state, ts):
    changes = []
    for w in cfg.get("watch_urls") or []:
        status, note = check_url(w["url"], w.get("closed_phrases"), w.get("must_contain"))
        if status == "unknown":
            continue
        prev = state["watch"].get(w["name"])
        if prev is None or prev["status"] != status:
            state["watch"][w["name"]] = {"status": status, "since": ts, "note": note, "url": w["url"]}
            if prev is not None:  # first observation is a baseline, not an alert
                changes.append({"name": w["name"], "status": status, "note": note, "url": w["url"]})
        else:
            prev["note"] = note
    return changes


def scan(cfg, state):
    ts = iso(now())
    log = state.setdefault("scan_log", [])  # when each scan really ran: shows how late GitHub's timer is
    gap_note = ""
    if log:
        gap = now() - parse(log[-1])
        if gap > timedelta(hours=cfg["alerts"].get("gap_alert_hours", 3)):
            h, m = divmod(int(gap.total_seconds() // 60), 60)
            gap_note = f"⚠ Job Radar was not running for {h}h {m}m (GitHub's timer was late). It is back and has caught up."
    log.append(ts)
    del log[:-300]
    if not state.get("fix_interndoor_closed"):  # the old page check wrongly closed every InternDoor job: undo it
        for r in state["jobs"].values():
            pd = posted_dt(r.get("posted"))
            if r.get("src") == "interndoor" and r["status"] == "closed" and pd and now() - pd < timedelta(days=14):
                r["status"] = "open"
                r.pop("closed_at", None)
        state["fix_interndoor_closed"] = True
    for f in (ROOT / "out").glob("alert*"):
        f.unlink()
    profile, acfg = cfg["profile"], cfg["alerts"]
    jobs, ok, failed = collect_all(cfg)
    first_run = not state.get("initialized")
    known = set(state.get("sources_seen", []))
    new_srcs = ok - known  # a newly added company/source is baselined silently (no alert flood)
    seen, new_jobs, llm_calls, detail_calls = set(), [], 0, 0
    rejected = state.setdefault("rejected", {})  # postings already judged unsuitable: never re-fetched
    alerted = state.setdefault("alerted", {})  # hirings already reported once: fingerprint -> time

    for j in jobs:
        seen.add(j["id"])
        rec = state["jobs"].get(j["id"])
        if rec:
            rec["last_seen"] = ts
            if rec["status"] == "closed":  # reposted
                rec["status"] = "open"
                rec.pop("closed_at", None)
            continue
        if j["id"] in rejected or not passes_filters(j, profile):
            continue
        if j["src"].startswith("gh:"):  # Greenhouse lists carry no text: fetch it for likely matches only
            if detail_calls >= MAX_DETAIL_CALLS:
                continue  # try again next run
            detail_calls += 1
            j.update(greenhouse_detail(j["src"][3:], j["id"].rsplit(":", 1)[1]))
        if j.get("stipend_inr_month") is None:
            j["stipend_inr_month"] = parse_pay(j.get("desc", ""))
        score = heuristic_score(j, profile)
        if not (passes_batch(j, profile) and passes_pay(j, profile)) or score < acfg["digest_min_score"]:
            rejected[j["id"]] = ts
            continue
        extra = {}
        if llm_calls < MAX_LLM_CALLS and os.getenv("GEMINI_API_KEY"):
            llm_calls += 1
            extra = llm_enrich(j, profile) or {}
        status = "open"
        if extra:
            if extra.get("eligible") is False:
                status = "ignored"  # remembered so we don't re-screen it every run
            elif isinstance(extra.get("score"), (int, float)):
                score = round((score + extra["score"]) / 2)
        pd = posted_dt(j.get("posted"))
        recent = bool(pd and now() - pd <= timedelta(days=acfg.get("catchup_days", 7)))
        # a newly added source: its RECENT postings still alert (you want to hear about them), old ones are baseline
        baseline = first_run or (j["src"] in new_srcs and not recent)
        rec = {**j, "desc": "", "score": score, "reason": extra.get("reason", ""), "deadline": extra.get("deadline") or "",
               "first_seen": ts, "last_seen": ts, "status": status,
               "baseline": baseline, "elite": is_elite(j, profile)}  # old listings of a newly added source are not "new"
        state["jobs"][j["id"]] = rec
        fp = fingerprint(j)
        already = fp in alerted  # re-posted under a new id? it is the same hiring: no second alert
        alerted.setdefault(fp, ts)
        pd = posted_dt(j.get("posted"))
        fresh = not (pd and now() - pd > timedelta(days=acfg.get("max_age_days", 14)))  # old posting: report only
        if status == "open" and score >= acfg["instant_min_score"] and not baseline and not already and fresh:
            new_jobs.append(rec)

    # a job that vanished from a board that loaded fine is closed
    for jid, rec in state["jobs"].items():
        if rec["status"] == "open" and rec.get("verifiable") and rec["src"] in ok and jid not in seen:
            rec["status"], rec["closed_at"] = "closed", ts

    state["sources_seen"] = sorted(known | ok)
    for k in [k for k in state["failures"] if k not in ok and k not in failed]:
        del state["failures"][k]  # a source you removed from config.yaml must not stay in "Source problems"
    state["source_stats"] = dict(_collectors.LAST_STATS)
    for src in ok:
        state["failures"].pop(src, None)
    for src, err in failed.items():
        f = state["failures"].setdefault(src, {"count": 0})
        f["count"] += 1
        f["error"] = err

    cutoff = now() - timedelta(days=30)
    for jid in [k for k, r in state["jobs"].items() if parse(r["last_seen"]) < cutoff]:
        del state["jobs"][jid]
    for k in [k for k, v in rejected.items() if parse(v) < cutoff]:
        del rejected[k]
    for k in [k for k, v in alerted.items() if parse(v) < now() - timedelta(days=180)]:
        del alerted[k]

    watch_changes = check_watch(cfg, state, ts)

    try:  # hackathons / challenges: news + official pages
        prog_alerts = check_programs(cfg, state, profile.get("graduation_year"))
    except Exception as e:
        prog_alerts = []
        print(f"[programs skipped] {type(e).__name__}: {str(e)[:100]}")
    try:  # keep looking for opportunities you did not list
        prog_alerts += discover(cfg, state, profile.get("graduation_year"))
    except Exception as e:
        print(f"[discovery skipped] {type(e).__name__}: {str(e)[:100]}")
    if gap_note and not first_run:
        push(gap_note)
    if prog_alerts:
        send_email(*program_email(prog_alerts))
        title, body = program_markdown(prog_alerts)  # picked up by the workflow -> GitHub issue
        (ROOT / "out").mkdir(exist_ok=True)
        (ROOT / "out" / "alert_program_title.txt").write_text(title, encoding="utf-8")
        (ROOT / "out" / "alert_program.md").write_text(body, encoding="utf-8")
        for a in prog_alerts:
            push(program_push_text(a))

    if first_run:
        print(f"First run: baseline saved ({len(state['jobs'])} matches). No instant alerts; see the daily digest.")
    elif new_jobs or watch_changes:
        send_email(*alert_email(new_jobs, watch_changes))
        title, body = alert_markdown(new_jobs, watch_changes)  # picked up by the workflow -> GitHub issue
        (ROOT / "out").mkdir(exist_ok=True)
        (ROOT / "out" / "alert_title.txt").write_text(title, encoding="utf-8")
        (ROOT / "out" / "alert.md").write_text(body, encoding="utf-8")
        for j in sorted(new_jobs, key=lambda x: -x["score"])[:10]:
            push(telegram_job_text(j))
        for c in watch_changes:
            push(f"{'✅' if c['status'] == 'open' else '⛔'} <b>{c['name']}</b> is now {c['status'].upper()}\n{c['url']}")
    for j in new_jobs:
        j["alerted_at"] = ts  # shown in the daily report so you can see it was already sent
    state["initialized"] = True
    print(f"scan: {len(jobs)} postings seen, {len(new_jobs)} new matches, {len(watch_changes)} watch changes, "
          f"{len(failed)} source failures")


def digest(cfg, state):
    n = now()
    since = n - timedelta(hours=24)
    # jobs from search feeds can't be verified by absence: re-open the page of the ones checked longest ago,
    # and only call a job closed after TWO bad checks in a row (one odd page must not hide a live job)
    todo = sorted((r for r in state["jobs"].values() if r["status"] == "open" and not r.get("verifiable")),
                  key=lambda r: r.get("last_checked", ""))[:MAX_URL_CHECKS]
    for rec in todo:
        rec["last_checked"] = iso(n)
        if check_url(rec["url"])[0] == "closed":
            rec["closed_strikes"] = rec.get("closed_strikes", 0) + 1
            if rec["closed_strikes"] >= 2:
                rec["status"], rec["closed_at"] = "closed", iso(n)
        else:
            rec["closed_strikes"] = 0
    check_watch(cfg, state, iso(n))

    jobs = list(state["jobs"].values())
    open_jobs = [r for r in jobs if r["status"] == "open"]
    max_age = timedelta(days=cfg["alerts"].get("max_age_days", 14))

    def fresh(r):  # genuinely new: found in the last 24h, not a baseline of a new source, not an old posting
        pd = posted_dt(r.get("posted"))
        return not r.get("baseline") and not (pd and n - pd > max_age)
    new24 = [r for r in open_jobs if parse(r["first_seen"]) >= since and fresh(r)]
    closed24 = [r for r in jobs if r["status"] == "closed" and r.get("closed_at") and parse(r["closed_at"]) >= since]
    soon = []
    for r in open_jobs:
        try:
            d = datetime.fromisoformat(r.get("deadline") or "").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if n <= d <= n + timedelta(days=7):
            soon.append(r)
    by_company = {}
    for r in sorted(open_jobs, key=lambda r: -r["score"]):
        by_company.setdefault(r["company"], []).append(r)
    date_str = n.astimezone(IST).strftime("%d %b %Y")
    failures = {k: v for k, v in state["failures"].items() if v["count"] >= 3}
    send_email(*digest_email(date_str, {"open": len(open_jobs), "companies": len(by_company)},
                             new24, soon, closed24, state["watch"], failures, by_company))
    progs = []
    for p in cfg.get("programs") or []:
        ps = state.get("programs", {}).get(p["name"], {})
        bits = ["news" if p.get("news") else "", f"{len(p.get('urls') or [])} official page(s)" if p.get("urls") else ""]
        links = []
        if p.get("news"):
            q = urllib.parse.urlencode({"q": p["news"], "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
            links.append(f"[latest news](https://news.google.com/search?{q})")
        links += [f"[official page]({u})" for u in p.get("urls") or []]
        note = ", ".join(links) or "no sources set"
        if ps.get("last_alert"):
            note += f" - alerted {fmt_dt(ps['last_alert'])}"
        progs.append((p["name"], note + (f" - news check failing: {ps['error']}" if ps.get("error") else "")))
    sources = []
    plain = [k for k in state.get("source_stats", {}) if k.split(":")[0] not in ("gh", "lv", "ab")]
    for k in sorted(plain):
        st_ = state["source_stats"][k]
        matched = sum(1 for r in open_jobs if r.get("src") == k)
        note = f"{st_['fetched']} postings read, {matched} match you"
        if st_.get("capped"):
            note += " (list cut off at the page limit)"
        sources.append((k, note))
    boards = [k for k in state.get("source_stats", {}) if k.split(":")[0] in ("gh", "lv", "ab")]
    if boards:
        sources.append(("company boards (Greenhouse / Lever / Ashby)",
                        f"{len(boards)} read fine, {sum(1 for r in open_jobs if r.get('src', '')[:3] in ('gh:', 'lv:', 'ab:'))} match you"))
    stamps = sorted(parse(t) for t in state.get("scan_log", []) if parse(t) >= n - timedelta(hours=24))
    if len(stamps) >= 2:
        gap = max(b - a for a, b in zip(stamps, stamps[1:]))
        h, m = divmod(int(gap.total_seconds() // 60), 60)
        sources.append(("scan timing", f"{len(stamps)} scans in the last 24h, longest gap {h}h {m}m (set to run every 10 minutes)"))
    opps = opportunity_sections(cfg, n.astimezone(IST).month)
    md = digest_markdown(date_str, {"open": len(open_jobs), "companies": len(by_company)},
                         new24, soon, closed24, state["watch"], failures, by_company, progs, opps, sources)
    rdir = ROOT / "reports"
    rdir.mkdir(exist_ok=True)
    (rdir / "latest.md").write_text(md, encoding="utf-8")
    (rdir / "opportunities.md").write_text(opportunities_markdown(opps, date_str), encoding="utf-8")
    (rdir / f"{n.astimezone(IST).strftime('%Y-%m-%d')}.md").write_text(md, encoding="utf-8")
    for old in sorted(rdir.glob("20*.md"))[:-14]:  # keep the last 14 days
        old.unlink()
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:  # also shows on the workflow run page
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    push(digest_push_text(date_str, {"companies": len(by_company)}, new24, closed24, by_company))
    print(f"digest sent: {len(new24)} new, {len(closed24)} closed, {len(open_jobs)} open")


def opportunity_sections(cfg, month):
    """(check now, usually opens within ~3 months, later) from the static tracker in config.yaml."""
    soon_months = {((month - 1 + k) % 12) + 1 for k in range(4)}
    now_, soon, later = [], [], []
    for o in cfg.get("opportunities") or []:
        if o.get("now"):
            now_.append(o)
        elif soon_months & set(o.get("months", [])):
            soon.append(o)
        else:
            later.append(o)
    return now_, soon, later


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 and choke on emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if cmd == "test":
        ok = send_email("✅ Job Radar test", "<p>Email works. You're all set.</p>", "Email works. You're all set.")
        tg = send_telegram("✅ Job Radar test: Telegram works.")
        wa = send_whatsapp("✅ <b>Job Radar test</b>: WhatsApp works.")
        print("email:", "sent" if ok else "not sent", "| telegram:", "sent" if tg else "not sent",
              "| whatsapp:", "sent" if wa else "not sent")
        return
    state = load_state()
    {"scan": scan, "digest": digest}[cmd](cfg, state)
    save_state(state)


if __name__ == "__main__":
    main()
