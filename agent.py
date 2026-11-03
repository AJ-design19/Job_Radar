#!/usr/bin/env python3
"""Job Radar.   python agent.py scan | digest | test"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from programs import check_programs
from collectors import check_url, collect_all, greenhouse_detail, parse_pay
from notify import (alert_email, digest_email, digest_push_text, push, send_email, send_telegram,
                    send_whatsapp, telegram_job_text, alert_markdown, digest_markdown, program_email,
                    program_markdown, program_push_text)
from scoring import heuristic_score, llm_enrich, passes_batch, passes_filters, passes_pay

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state" / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: no tz database needed (works on Windows)
MAX_DETAIL_CALLS = 60   # Greenhouse posting-detail fetches per run (description + pay)
MAX_LLM_CALLS = 15      # Gemini screenings per run
MAX_URL_CHECKS = 15     # open/closed page checks per digest for search-API jobs


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
    for f in (ROOT / "out").glob("alert*"):
        f.unlink()
    profile, acfg = cfg["profile"], cfg["alerts"]
    jobs, ok, failed = collect_all(cfg)
    first_run = not state.get("initialized")
    known = set(state.get("sources_seen", []))
    new_srcs = ok - known  # a newly added company/source is baselined silently (no alert flood)
    seen, new_jobs, llm_calls, detail_calls = set(), [], 0, 0
    rejected = state.setdefault("rejected", {})  # postings already judged unsuitable: never re-fetched

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
        rec = {**j, "desc": "", "score": score, "reason": extra.get("reason", ""), "deadline": extra.get("deadline") or "",
               "first_seen": ts, "last_seen": ts, "status": status}
        state["jobs"][j["id"]] = rec
        if status == "open" and score >= acfg["instant_min_score"] and j["src"] not in new_srcs:
            new_jobs.append(rec)

    # a job that vanished from a board that loaded fine is closed
    for jid, rec in state["jobs"].items():
        if rec["status"] == "open" and rec.get("verifiable") and rec["src"] in ok and jid not in seen:
            rec["status"], rec["closed_at"] = "closed", ts

    state["sources_seen"] = sorted(known | ok)
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

    watch_changes = check_watch(cfg, state, ts)

    try:  # hackathons / challenges: news + official pages
        prog_alerts = check_programs(cfg, state, profile.get("graduation_year"))
    except Exception as e:
        prog_alerts = []
        print(f"[programs skipped] {type(e).__name__}: {str(e)[:100]}")
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
    state["initialized"] = True
    print(f"scan: {len(jobs)} postings seen, {len(new_jobs)} new matches, {len(watch_changes)} watch changes, "
          f"{len(failed)} source failures")


def digest(cfg, state):
    n = now()
    since = n - timedelta(hours=24)
    checked = 0
    for rec in state["jobs"].values():  # search-API jobs can't be verified by absence: check the page
        if rec["status"] == "open" and not rec.get("verifiable") and checked < MAX_URL_CHECKS:
            checked += 1
            if check_url(rec["url"])[0] == "closed":
                rec["status"], rec["closed_at"] = "closed", iso(n)
    check_watch(cfg, state, iso(n))

    jobs = list(state["jobs"].values())
    open_jobs = [r for r in jobs if r["status"] == "open"]
    new24 = [r for r in open_jobs if parse(r["first_seen"]) >= since]
    closed24 = [r for r in jobs if r["status"] == "closed" and r.get("closed_at") and parse(r["closed_at"]) >= since]
    soon = []
    for r in open_jobs:
        try:
            d = datetime.fromisoformat(r["deadline"]).replace(tzinfo=timezone.utc)
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
        note = ", ".join(b for b in bits if b) or "no sources set"
        progs.append((p["name"], note + (f" - news check failing: {ps['error']}" if ps.get("error") else "")))
    md = digest_markdown(date_str, {"open": len(open_jobs), "companies": len(by_company)},
                         new24, soon, closed24, state["watch"], failures, by_company, progs)
    rdir = ROOT / "reports"
    rdir.mkdir(exist_ok=True)
    (rdir / "latest.md").write_text(md, encoding="utf-8")
    (rdir / f"{n.astimezone(IST).strftime('%Y-%m-%d')}.md").write_text(md, encoding="utf-8")
    for old in sorted(rdir.glob("20*.md"))[:-14]:  # keep the last 14 days
        old.unlink()
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:  # also shows on the workflow run page
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(md)
    push(digest_push_text(date_str, {"companies": len(by_company)}, new24, closed24, by_company))
    print(f"digest sent: {len(new24)} new, {len(closed24)} closed, {len(open_jobs)} open")


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
