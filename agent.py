#!/usr/bin/env python3
"""Job Radar.   python agent.py scan | digest | test"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from collectors import check_url, collect_all
from notify import alert_email, digest_email, send_email, send_telegram, telegram_job_text
from scoring import heuristic_score, llm_enrich, passes_filters

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state" / "state.json"
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
        return json.loads(STATE_PATH.read_text())
    return {"initialized": False, "jobs": {}, "watch": {}, "failures": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))


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
    profile, acfg = cfg["profile"], cfg["alerts"]
    jobs, ok, failed = collect_all(cfg)
    first_run = not state.get("initialized")
    known = set(state.get("sources_seen", []))
    new_srcs = ok - known  # a newly added company/source is baselined silently (no alert flood)
    seen, new_jobs, llm_calls = set(), [], 0

    for j in jobs:
        seen.add(j["id"])
        rec = state["jobs"].get(j["id"])
        if rec:
            rec["last_seen"] = ts
            if rec["status"] == "closed":  # reposted
                rec["status"] = "open"
                rec.pop("closed_at", None)
            continue
        if not passes_filters(j, profile):
            continue
        score = heuristic_score(j, profile)
        if score < acfg["digest_min_score"]:
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
        rec = {**j, "score": score, "reason": extra.get("reason", ""), "deadline": extra.get("deadline") or "",
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

    watch_changes = check_watch(cfg, state, ts)

    if first_run:
        print(f"First run: baseline saved ({len(state['jobs'])} matches). No instant alerts; see the daily digest.")
    elif new_jobs or watch_changes:
        send_email(*alert_email(new_jobs, watch_changes))
        for j in sorted(new_jobs, key=lambda x: -x["score"])[:10]:
            send_telegram(telegram_job_text(j))
        for c in watch_changes:
            send_telegram(f"{'✅' if c['status'] == 'open' else '⛔'} <b>{c['name']}</b> is now {c['status'].upper()}\n{c['url']}")
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
    date_str = n.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y")
    failures = {k: v for k, v in state["failures"].items() if v["count"] >= 3}
    send_email(*digest_email(date_str, {"open": len(open_jobs), "companies": len(by_company)},
                             new24, soon, closed24, state["watch"], failures, by_company))
    print(f"digest sent: {len(new24)} new, {len(closed24)} closed, {len(open_jobs)} open")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    if cmd == "test":
        ok = send_email("✅ Job Radar test", "<p>Email works. You're all set.</p>", "Email works. You're all set.")
        send_telegram("✅ Job Radar test: Telegram works.")
        print("test email sent" if ok else "email NOT sent (check secrets)")
        return
    state = load_state()
    {"scan": scan, "digest": digest}[cmd](cfg, state)
    save_state(state)


if __name__ == "__main__":
    main()
