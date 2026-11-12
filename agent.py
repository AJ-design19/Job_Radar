#!/usr/bin/env python3
"""Job Radar.   python agent.py scan | digest | test"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import collectors as _collectors
from programs import check_programs, discover
from collectors import check_url, collect_all, greenhouse_detail, interndoor_detail, parse_pay
from notify import (alert_email, digest_email, digest_push_text, push, send_email, send_telegram,
                    send_whatsapp, telegram_job_text, alert_markdown, digest_markdown, program_email,
                    program_markdown, program_push_text, fmt_dt, opportunities_markdown)
from scoring import (heuristic_score, is_elite, llm_enrich, passes_batch, passes_filters, passes_pay,
                     passes_title_rules, is_offtrack, watch_name)

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


def scan(cfg, state, news=True, light=False):
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
    jobs, ok, failed = collect_all(cfg, light=True) if light else collect_all(cfg)
    first_run = not state.get("initialized")
    known = set(state.get("sources_seen", []))
    new_srcs = ok - known  # a newly added company/source is baselined silently (no alert flood)
    seen, new_jobs, llm_calls, detail_calls = set(), [], 0, 0
    watchlist = cfg.get("watchlist") or []
    for jid in [k for k, r in state["jobs"].items() if r["status"] == "open" and not passes_title_rules(r, profile)]:
        del state["jobs"][jid]  # stored before the filters were tightened (hardware, graduate programs, design...)
    if not state.get("fix_watchlist"):  # jobs earlier rejected by score may now qualify as watchlist roles
        state["rejected"] = {}
        state["fix_watchlist"] = True
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
        elif j["src"] == "interndoor" and detail_calls < MAX_DETAIL_CALLS:
            detail_calls += 1
            j.update(interndoor_detail(j["url"]))  # apply_url + the company page text
        if j.get("stipend_inr_month") is None:
            j["stipend_inr_month"] = parse_pay(j.get("desc", ""))
        score = heuristic_score(j, profile)
        wn = watch_name(j, watchlist)  # FAANG / MANGO / your big-tech list: always reported
        if not (passes_batch(j, profile) and passes_pay(j, profile)) or (score < acfg["digest_min_score"] and not wn):
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
        recent = bool(pd and not j.get("old") and now() - pd <= timedelta(days=acfg.get("catchup_days", 7)))
        # a newly added source: its RECENT postings still alert (you want to hear about them), old ones are baseline
        baseline = first_run or (j["src"] in new_srcs and not recent)
        rec = {**j, "desc": "", "score": score, "reason": extra.get("reason", ""), "deadline": extra.get("deadline") or "",
               "first_seen": ts, "last_seen": ts, "status": status,
               "baseline": baseline, "elite": is_elite(j, profile), "watch": wn or ""}  # old listings of a newly added source are not "new"
        state["jobs"][j["id"]] = rec
        fp = fingerprint(j)
        already = fp in alerted  # re-posted under a new id? it is the same hiring: no second alert
        alerted.setdefault(fp, ts)
        pd = posted_dt(j.get("posted"))
        fresh = not (j.get("old") or (pd and now() - pd > timedelta(days=acfg.get("max_age_days", 14))))  # old: report only
        off = is_offtrack(j, profile)  # support / QA / embedded: still listed, but a big name alone does not trigger an alert
        if status == "open" and (score >= acfg["instant_min_score"] or (wn and not off)) and not baseline and not already and fresh:
            new_jobs.append(rec)

    # a job that vanished from a board that loaded fine is closed
    for jid, rec in state["jobs"].items():
        if rec["status"] == "open" and rec.get("verifiable") and rec["src"] in ok and jid not in seen:
            rec["status"], rec["closed_at"] = "closed", ts

    state["sources_seen"] = sorted(known | ok)
    if not light:
        for k in [k for k in state["failures"] if k not in ok and k not in failed]:
            del state["failures"][k]  # a source you removed from config.yaml must not stay in "Source problems"
        state["source_stats"] = dict(_collectors.LAST_STATS)
    else:
        state.setdefault("source_stats", {}).update(_collectors.LAST_STATS)
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
        prog_alerts = check_programs(cfg, state, profile.get("graduation_year")) if news else []
    except Exception as e:
        prog_alerts = []
        print(f"[programs skipped] {type(e).__name__}: {str(e)[:100]}")
    try:  # keep looking for opportunities you did not list
        prog_alerts += discover(cfg, state, profile.get("graduation_year")) if news else []
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
    raw_open = [r for r in jobs if r["status"] == "open"]
    best = {}
    for r in raw_open:  # the same role listed several times (re-posts, several requisitions) is shown once, with a count
        k = (fingerprint(r), re.sub(r"[^a-z0-9]", "", (r.get("location") or "").lower()))
        if k not in best:
            best[k] = dict(r, copies=1)
        else:
            keep = best[k] if best[k].get("first_seen", "") >= r.get("first_seen", "") else dict(r, copies=1)
            keep["copies"] = best[k]["copies"] + 1
            best[k] = keep
    open_jobs = list(best.values())
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
        note = f"{st_['fetched']} postings read, {matched} match you" + (f" ({st_['note']})" if st_.get("note") else "")
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
        sources.append(("scan timing", f"{len(stamps)} scans in the last 24h, longest gap {h}h {m}m (the radar loop aims for one every 5 minutes)"))
    opps = opportunity_sections(cfg, n.astimezone(IST).month)

    def live_for(match):  # the actual job links the radar can see for these companies
        ws = [{"name": "x", "match": match}]
        return [(r["title"], r["url"], r["location"]) for r in
                sorted((r for r in open_jobs if watch_name(r, ws)), key=lambda r: -r["score"])[:3]]
    for o in cfg.get("opportunities") or []:
        o["live"] = live_for(o["match"]) if o.get("match") else []
    women = [o for o in cfg.get("opportunities") or [] if o.get("tier") == "Women"]
    bigtech = []
    for w in cfg.get("watchlist") or []:
        js = sorted((r for r in open_jobs if watch_name(r, [w])), key=lambda r: -r["score"])
        bigtech.append((w["name"], [(r["title"], r["url"], r["location"], r.get("posted") or "") for r in js]))
    md = digest_markdown(date_str, {"open": len(open_jobs), "companies": len(by_company)},
                         new24, soon, closed24, state["watch"], failures, by_company, progs, opps, sources, bigtech, women)
    rdir = ROOT / "reports"
    rdir.mkdir(exist_ok=True)
    (rdir / "latest.md").write_text(md, encoding="utf-8")
    issue_md = md if len(md) < 60000 else md[:60000] + "\n\n_(shortened for GitHub; full report in reports/latest.md)_"
    (rdir / "issue.md").write_text(issue_md, encoding="utf-8")
    (rdir / "opportunities.md").write_text(opportunities_markdown(opps, date_str, women), encoding="utf-8")
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
        if o.get("tier") == "Women":
            continue
        if o.get("now"):
            now_.append(o)
        elif soon_months & set(o.get("months", [])):
            soon.append(o)
        else:
            later.append(o)
    return now_, soon, later


# ---------------------------------------------------------------------------------------------
# LOOP MODE: one long-running job that scans every few minutes (instead of waiting for GitHub's unreliable timer).
# ---------------------------------------------------------------------------------------------
def post_issues():
    """Turn alert files into GitHub issues (works where the gh CLI and GH_TOKEN exist, i.e. inside Actions)."""
    out = ROOT / "out"
    if not os.getenv("GH_TOKEN"):
        return
    for title_f, body_f in (("alert_title.txt", "alert.md"), ("alert_program_title.txt", "alert_program.md")):
        t, b = out / title_f, out / body_f
        if t.exists() and b.exists():
            subprocess.run(["gh", "issue", "create", "--title", t.read_text(encoding="utf-8").strip(),
                            "--body-file", str(b)], check=False, cwd=ROOT)
            t.unlink()
            b.unlink()


def post_report_issue(date_str):
    latest = ROOT / "reports" / "issue.md"
    if os.getenv("GH_TOKEN") and latest.exists():
        subprocess.run(["gh", "issue", "create", "--title", f"📋 Job Radar report {date_str}", "--body-file", str(latest)],
                       check=False, cwd=ROOT)


def git_sync(message):
    """Commit and push the saved state / reports so nothing is lost if the job is stopped."""
    def run(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
    run("config", "user.name", "job-radar-bot")
    run("config", "user.email", "job-radar-bot@users.noreply.github.com")
    run("add", "state", "reports")
    if run("diff", "--cached", "--quiet").returncode == 0:
        return
    run("commit", "-m", message)
    run("pull", "--rebase")
    run("push")


def active_burst(cfg, ist):
    """A date window in config.yaml (e.g. a program that opens for a few hours) where the radar polls faster."""
    today = ist.date().isoformat()
    for b in cfg.get("burst") or []:
        if str(b["start"]) <= today <= str(b["end"]):
            return b
    return None


def loop(cfg, minutes=340, every=300, news_every=3, digest_hour=8, sync_every=1200):
    """Scan every `every` seconds for `minutes`. Job sources every round, news searches every `news_every`th round
    (they are many requests), the daily report once after `digest_hour` India time."""
    end, last_sync, i = time.time() + minutes * 60, time.time(), 0
    print(f"[loop] scanning every {every}s for {minutes} min")
    while time.time() < end:
        t0 = time.time()
        burst = active_burst(cfg, datetime.now(IST))
        gap = int(burst.get("every", 120)) if burst else every
        try:
            state = load_state()
            if burst:  # fast feeds every couple of minutes, the slow full scan every Nth round
                scan(cfg, state, news=True, light=(i % int(burst.get("full_every", 5)) != 0))
            else:
                scan(cfg, state, news=(i % news_every == 0))
            ist = datetime.now(IST)
            if ist.hour >= digest_hour and state.get("last_digest") != ist.date().isoformat():
                digest(cfg, state)
                state["last_digest"] = ist.date().isoformat()
                save_state(state)
                post_report_issue(ist.strftime("%d %b %Y"))
            else:
                save_state(state)
            post_issues()
        except Exception as e:  # one bad round must never stop the loop
            print(f"[loop] round {i} failed: {type(e).__name__}: {str(e)[:150]}")
        i += 1
        if time.time() - last_sync > sync_every:
            git_sync("chore: update state [skip ci]")
            last_sync = time.time()
        time.sleep(max(5, gap - (time.time() - t0)))
    git_sync("chore: update state [skip ci]")
    print("[loop] finished this chunk; the next run takes over")


def check(cfg):
    """Test every source once and print a pass/fail table (run it with the 'check' workflow or locally)."""
    import collectors as c
    from concurrent.futures import ThreadPoolExecutor
    from programs import news_items, resolve_link
    profile = cfg["profile"]
    lines = ["JOB RADAR SOURCE CHECK  " + datetime.now(IST).strftime("%d %b %Y %I:%M %p IST"), ""]

    def fit(jobs):
        return [j for j in jobs if passes_filters(j, profile)]

    def one(label, fn, notes_key=None):
        try:
            jobs = fn()
            m = fit(jobs)
            note = c.LAST_NOTES.get(notes_key, "") if notes_key else ""
            sample = "; ".join(f"{j['title'][:34]} [{(j['location'] or '-')[:18]}]" for j in m[:2])
            return f"OK    {label:<40} {len(jobs):>4} read {len(m):>3} fit  {note} {sample}".rstrip()
        except Exception as e:
            return f"FAIL  {label:<40} {type(e).__name__}: {str(e)[:130]}"

    comp = cfg.get("companies") or {}
    lines.append("WORKDAY (big companies)")
    for e in comp.get("workday") or []:
        k = c.workday_key(e)
        lines.append("  " + one(k, lambda e=e: c.workday(e), k))
    lines += ["", "SEARCH FEEDS"]
    for name, fn in (("internscan", c.internscan), ("interndoor", c.interndoor), ("amazon", c.amazon),
                     ("atlassian", c.atlassian), ("themuse", c.themuse), ("adzuna", c.adzuna)):
        conf = cfg.get(name)
        if conf and conf.get("enabled") is not False:
            if name == "adzuna" and not (os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY")):
                lines.append("  SKIP  adzuna                                   no ADZUNA_APP_ID / ADZUNA_APP_KEY secrets set")
                continue
            lines.append("  " + one(name, lambda fn=fn, conf=conf: (fn(conf) or []), name))

    boards = [("gh:" + s, c.greenhouse, s) for s in comp.get("greenhouse") or []]
    boards += [("lv:" + s, c.lever, s) for s in comp.get("lever") or []] + [("ab:" + s, c.ashby, s) for s in comp.get("ashby") or []]

    def run(t):
        k, fn, arg = t
        try:
            return k, fn(arg), None
        except Exception as e:
            return k, None, f"{type(e).__name__}: {str(e)[:70]}"
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(run, boards))
    ok = [(k, j) for k, j, e in res if e is None]
    bad = [(k, e) for k, j, e in res if e]
    lines += ["", f"COMPANY BOARDS (Greenhouse / Lever / Ashby): {len(ok)} ok, {len(bad)} failing, "
                  f"{sum(len(fit(j)) for _, j in ok)} postings fit you"] + [f"  FAIL  {k}: {e}" for k, e in bad]

    lines += ["", "NEWS AND LINKS"]
    queries = [p["news"] for p in (cfg.get("programs") or []) if p.get("news")][:1] + (cfg.get("discovery") or {}).get("queries", [])[:1]
    items = []
    for q in queries:
        try:
            items = news_items(q, 3) or items
            lines.append(f"  OK    news search '{q[:34]}': {len(news_items(q, 3))} headlines")
        except Exception as e:
            lines.append(f"  FAIL  news search '{q[:34]}': {type(e).__name__}: {str(e)[:100]}")
    if items:
        u = items[0]["link"]
        r = resolve_link(u)
        lines.append("  OK    link decoder: " + r[:80] if r != u else "  WARN  link decoder: not working, news alerts keep Google News links")

    chan = {"email": os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"),
            "telegram": os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"),
            "whatsapp": os.getenv("CALLMEBOT_PHONE") and os.getenv("CALLMEBOT_APIKEY"),
            "gemini": os.getenv("GEMINI_API_KEY")}
    lines += ["", "ALERT CHANNELS: " + ", ".join(f"{k} {'ON' if v else 'off'}" for k, v in chan.items())]
    text = "\n".join(lines)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("```\n" + text + "\n```\n")


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
    if cmd == "loop":
        loop(cfg)
        return
    if cmd == "check":
        check(cfg)
        return
    state = load_state()
    {"scan": scan, "digest": digest}[cmd](cfg, state)
    save_state(state)


if __name__ == "__main__":
    main()
