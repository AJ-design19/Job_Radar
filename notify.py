"""Email (Gmail SMTP) + optional Telegram notifications and HTML rendering."""
import html
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests


def _e(s):
    return html.escape(str(s if s is not None else ""))


IST = timezone(timedelta(hours=5, minutes=30))


def fmt_dt(v):
    """'30 Oct 2026, 07:42 PM IST' from an ISO string; date-only values stay date-only; '' if unknown."""
    if not v:
        return ""
    s = str(v).strip()
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%d %b %Y")
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def _when(j):
    """Posted / detected / alert-sent times for one job."""
    parts = []
    p = fmt_dt(j.get("posted"))
    if p:
        parts.append(f"{'last updated' if j.get('posted_kind') == 'updated' else 'posted'} {p}")
    if fmt_dt(j.get("first_seen")):
        parts.append(f"detected {fmt_dt(j['first_seen'])}")
    if fmt_dt(j.get("alerted_at")):
        parts.append(f"🔔 alert sent {fmt_dt(j['alerted_at'])}")
    return " · ".join(parts)


def send_email(subject, body_html, body_text):
    user, pwd = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("NOTIFY_TO") or user
    if not (user and pwd):
        print(f"[email skipped - GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set]\n{subject}\n{body_text}\n")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = f"Job Radar <{user}>"
    msg["To"] = to
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return True


def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20, json={
            "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        return r.ok
    except Exception:
        return False


def _wa(text):
    """Telegram-style HTML -> WhatsApp formatting (*bold*, _italic_)."""
    text = re.sub(r"<b>(.*?)</b>", r"*\1*", text, flags=re.S)
    text = re.sub(r"<i>(.*?)</i>", r"_\1_", text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_whatsapp(text):
    """CallMeBot (free, personal use) and/or Twilio WhatsApp. Either one, both or none."""
    text, sent = _wa(text)[:1500], False
    phone, key = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if phone and key:
        try:
            r = requests.get("https://api.callmebot.com/whatsapp.php", timeout=30,
                             params={"phone": phone, "text": text, "apikey": key})
            sent = r.ok
            if not r.ok:
                print(f"[callmebot failed] HTTP {r.status_code}")
        except Exception as e:
            print(f"[callmebot failed] {type(e).__name__}")
    sid, tok, to = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"), os.getenv("WHATSAPP_TO")
    if sid and tok and to:
        try:
            r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", timeout=30,
                              auth=(sid, tok),
                              data={"From": os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
                                    "To": f"whatsapp:{to}", "Body": text})
            sent = sent or r.ok
            if not r.ok:
                print(f"[twilio failed] HTTP {r.status_code}")
        except Exception as e:
            print(f"[twilio failed] {type(e).__name__}")
    return sent


def push(text):
    """Send one short message to every phone channel that is configured."""
    a = send_telegram(text)
    b = send_whatsapp(text)
    return a or b


def digest_push_text(date_str, stats, new24, closed24, by_company):
    lines = [f"📋 <b>Job Radar {date_str}</b>",
             f"{stats['companies']} companies hiring interns · {len(new24)} new · {len(closed24)} closed"]
    top = sorted(new24, key=lambda j: -j["score"])[:6]
    if top:
        lines.append("\n<b>New:</b>")
        lines += [f"• {_e(j['title'])} @ {_e(j['company'])} ({j['score']}/10)\n{_e(j['url'])}" for j in top]
    if by_company:
        items = sorted(by_company.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))[:15]
        lines.append("\n<b>Hiring now:</b> " + ", ".join(f"{_e(c)} ({len(js)})" for c, js in items))
    return "\n".join(lines)


def telegram_job_text(j):
    extra = f"\n<i>{_e(j['reason'])}</i>" if j.get("reason") else ""
    dl = f"\nDeadline: {_e(j['deadline'])}" if j.get("deadline") else ""
    dl += f"\nStipend: {_pay(j)}" if _pay(j) else ""
    when = f"\n🕒 {_e(_when(j))}" if _when(j) else ""
    return (f"🔔 <b>{_e(j['title'])}</b>\n{_e(j['company'])} · {_e(j['location'] or '-')} · match {j['score']}/10"
            f"{dl}{when}{extra}\n{_e(j['url'])}")


def _pay(j):
    p = j.get("stipend_inr_month")
    return f"₹{int(p):,}/month" if p else ""


def _job_html(j):
    bits = [j["company"], j["location"] or "-", f"match {j['score']}/10"]
    if _pay(j):
        bits.append(_pay(j))
    if j.get("deadline"):
        bits.append(f"deadline {j['deadline']}")
    reason = f"<br><span style='color:#777;font-size:13px'>{_e(j['reason'])}</span>" if j.get("reason") else ""
    when = f"<br><span style='color:#777;font-size:13px'>🕒 {_e(_when(j))}</span>" if _when(j) else ""
    return (f"<tr><td style='padding:10px 0;border-bottom:1px solid #eee'>"
            f"<a href='{_e(j['url'])}' style='font-weight:600;color:#1a56db;text-decoration:none'>{_e(j['title'])}</a>"
            f"<br><span style='color:#555;font-size:14px'>{_e(' · '.join(bits))}</span>{when}{reason}</td></tr>")


def _job_text(j):
    when = f"\n  {_when(j)}" if _when(j) else ""
    return f"- {j['title']} @ {j['company']} ({j['location'] or '-'}) [{j['score']}/10]{when}\n  {j['url']}"


def _section(title, rows_html):
    return (f"<h3 style='margin:22px 0 4px;font-size:16px'>{_e(title)}</h3>"
            f"<table width='100%' cellpadding='0' cellspacing='0'>{rows_html}</table>")


def _wrap(title, inner):
    return (f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:auto;"
            f"padding:16px;color:#222'><h2 style='margin:0 0 4px'>{_e(title)}</h2>{inner}"
            f"<p style='color:#999;font-size:12px;margin-top:28px'>Job Radar · running on your GitHub Actions</p></div>")


def alert_email(new_jobs, watch_changes):
    new_jobs = sorted(new_jobs, key=lambda j: -j["score"])
    parts, text = [], []
    if new_jobs:
        parts.append(_section("New matches", "".join(_job_html(j) for j in new_jobs)))
        text += ["NEW MATCHES"] + [_job_text(j) for j in new_jobs]
    if watch_changes:
        rows = "".join(f"<tr><td style='padding:8px 0'><b>{_e(c['name'])}</b>: {_e(c['status'].upper())} "
                       f"- <a href='{_e(c['url'])}'>open page</a><br><span style='color:#777;font-size:13px'>"
                       f"{_e(c['note'])}</span></td></tr>" for c in watch_changes)
        parts.append(_section("Watchlist changes", rows))
        text += ["WATCHLIST"] + [f"- {c['name']}: {c['status'].upper()} {c['url']}" for c in watch_changes]
    if new_jobs:
        top = new_jobs[0]
        subject = f"🔔 {len(new_jobs)} new match{'es' if len(new_jobs) > 1 else ''}: {top['title']} @ {top['company']}"
    else:
        c = watch_changes[0]
        subject = f"{'✅' if c['status'] == 'open' else '⛔'} {c['name']} is now {c['status'].upper()}"
    return subject, _wrap("Job Radar alert", "".join(parts)), "\n".join(text)


def digest_email(date_str, stats, new24, soon, closed24, watch, failures, by_company=None):
    parts, text = [], []
    s = (f"<p style='color:#555'>{stats['open']} open matches tracked · {len(new24)} new in 24h · "
         f"{len(closed24)} closed in 24h</p>")
    parts.append(s)
    text.append(f"{stats['open']} open tracked, {len(new24)} new, {len(closed24)} closed (24h)")
    if soon:
        parts.append(_section("⏰ Deadline within 7 days", "".join(_job_html(j) for j in soon)))
        text += ["DEADLINE SOON"] + [_job_text(j) + f"\n  deadline {j['deadline']}" for j in soon]
    top = sorted(new24, key=lambda j: -j["score"])[:25]
    parts.append(_section("New in the last 24 hours (best first)",
                          "".join(_job_html(j) for j in top) or "<tr><td style='color:#777'>Nothing new.</td></tr>"))
    text += ["NEW (24h)"] + ([_job_text(j) for j in top] or ["Nothing new."])
    if by_company:
        total = sum(len(v) for v in by_company.values())
        items = sorted(by_company.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))[:80]
        rows, lines = "", []
        for comp, js in items:
            links = "<br>".join(
                f"<a href='{_e(j['url'])}' style='color:#1a56db;text-decoration:none'>{_e(j['title'])}</a> "
                f"<span style='color:#777;font-size:13px'>({_e(j['location'] or '-')}, {j['score']}/10)</span>"
                for j in js[:6])
            more = (f"<br><span style='color:#777;font-size:13px'>+{len(js) - 6} more</span>"
                    if len(js) > 6 else "")
            rows += (f"<tr><td style='padding:10px 0;border-bottom:1px solid #eee'><b>{_e(comp)}</b> "
                     f"<span style='color:#777'>- {len(js)} role{'s' if len(js) != 1 else ''}</span>"
                     f"<br>{links}{more}</td></tr>")
            lines.append(f"{comp} ({len(js)}): " + "; ".join(j["title"] for j in js[:6]))
        title = f"🏢 Companies hiring interns right now ({len(by_company)} companies, {total} roles)"
        parts.append(_section(title, rows))
        text += [title.upper()] + lines
    if closed24:
        parts.append(_section("Closed since yesterday", "".join(_job_html(j) for j in closed24[:15])))
        text += ["CLOSED"] + [_job_text(j) for j in closed24[:15]]
    if watch:
        rows = "".join(f"<tr><td style='padding:6px 0'><b>{_e(n)}</b>: {_e(w['status'].upper())} "
                       f"<span style='color:#777;font-size:13px'>({_e(w['note'])})</span></td></tr>"
                       for n, w in watch.items())
        parts.append(_section("Watchlist", rows))
        text += ["WATCHLIST"] + [f"- {n}: {w['status'].upper()}" for n, w in watch.items()]
    if failures:
        rows = "".join(f"<tr><td style='padding:4px 0;color:#a33;font-size:13px'>{_e(k)}: {_e(v['error'])} "
                       f"(x{v['count']})</td></tr>" for k, v in failures.items())
        parts.append(_section("Source problems (check slug / network)", rows))
        text += ["SOURCE PROBLEMS"] + [f"- {k}: {v['error']}" for k, v in failures.items()]
    subject = (f"📋 Job Radar {date_str}: {stats.get('companies', 0)} companies hiring interns, "
               f"{len(new24)} new, {len(closed24)} closed")
    return subject, _wrap(f"Daily report - {date_str}", "".join(parts)), "\n".join(text)


# ---------------- GitHub-only output (markdown) ----------------
def _md_job(j):
    title = str(j["title"]).replace("[", "(").replace("]", ")")
    extra = f" - {j['reason']}" if j.get("reason") else ""
    dl = f" · deadline **{j['deadline']}**" if j.get("deadline") else ""
    dl += f" · {_pay(j)}" if _pay(j) else ""
    url = str(j["url"]).replace(" ", "%20").replace(")", "%29")
    when = f"\n  - 🕒 {_when(j)}" if _when(j) else ""
    return f"- [{title}]({url}) - {j['company']} · {j['location'] or '-'} · **{j['score']}/10**{dl}{extra}{when}"


def _mention():
    owner = os.getenv("GITHUB_REPOSITORY_OWNER")
    return [f"@{owner}", ""] if owner else []


def alert_markdown(new_jobs, watch_changes):
    new_jobs = sorted(new_jobs, key=lambda j: -j["score"])
    L = _mention()
    if new_jobs:
        L += ["## New matches"] + [_md_job(j) for j in new_jobs] + [""]
    if watch_changes:
        L += ["## Watchlist changes"] + [f"- **{c['name']}** is now **{c['status'].upper()}** - {c['url']} ({c['note']})"
                                         for c in watch_changes]
    if new_jobs:
        top = new_jobs[0]
        title = f"🔔 {len(new_jobs)} new match{'es' if len(new_jobs) > 1 else ''}: {top['title']} @ {top['company']}"
    else:
        c = watch_changes[0]
        title = f"{'✅' if c['status'] == 'open' else '⛔'} {c['name']} is now {c['status'].upper()}"
    return title, "\n".join(L)


def digest_markdown(date_str, stats, new24, soon, closed24, watch, failures, by_company, programs=None, opps=None,
                    sources=None):
    L = _mention() + [f"# Job Radar daily report - {date_str}",
                      f"**{stats.get('companies', 0)} companies** hiring interns · {stats['open']} open roles · "
                      f"{len(new24)} new in 24h · {len(closed24)} closed in 24h", ""]
    if soon:
        L += ["## ⏰ Deadline within 7 days"] + [_md_job(j) for j in soon] + [""]
    top = sorted(new24, key=lambda j: -j["score"])[:40]
    L += ["## New in the last 24 hours"] + ([_md_job(j) for j in top] or ["_Nothing new._"]) + [""]
    if by_company:
        items = sorted(by_company.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))[:100]
        L += [f"## 🏢 Companies hiring interns right now ({len(by_company)})", ""]
        for comp, js in items:
            L += [f"<details><summary><b>{_e(comp)}</b> ({len(js)})</summary>", ""]
            L += [_md_job(j) for j in js[:10]]
            if len(js) > 10:
                L.append(f"- _+{len(js) - 10} more_")
            L += ["", "</details>", ""]
    if opps:
        now_, soon_, later_ = opps
        L += ["## 📅 Opportunity tracker (full list: reports/opportunities.md)"]
        if now_:
            L += ["**Check / apply now**"] + [_opp_line(o) for o in now_] + [""]
        if soon_:
            L += ["**Open or usually opening in the next ~3 months**"] + [_opp_line(o) for o in soon_] + [""]
        L += [f"_{len(later_)} more programs open later in the year; see the full list._", ""]
    if programs:
        L += ["## 🎯 Programs being watched"] + [f"- **{n}** - {d}" for n, d in programs] + [""]
    if closed24:
        L += ["## Closed since yesterday"] + [_md_job(j) for j in closed24[:15]] + [""]
    if watch:
        L += ["## Watchlist"] + [f"- **{n}**: {w['status'].upper()} ({w['note']})" for n, w in watch.items()] + [""]
    if sources:
        L += ["## 📡 What the radar read this run"] + [f"- `{k}`: {d}" for k, d in sources] + [""]
    if failures:
        L += ["## Source problems (check slug / network)"] + [f"- `{k}`: {v['error']} (x{v['count']})"
                                                               for k, v in failures.items()]
    return "\n".join(L)


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _opp_line(o):
    when = f" · usually {', '.join(MONTHS[m - 1] for m in o['months'])}" if o.get("months") else ""
    return (f"- **{o['name']}** [{o.get('tier', '')}] {o.get('batch', '')}{when} - {o.get('note', '')} "
            f"_({o.get('confidence', 'typical')})_ · where: {o.get('where', '-')}")


def opportunities_markdown(opps, date_str):
    now_, soon_, later_ = opps
    L = [f"# Internship opportunity tracker - {date_str}",
         "Tier A = best-paying employers. 'confirmed' = seen on a recent edition; 'typical' = usual pattern, check the official page.", ""]
    for title, items in (("Check / apply now", now_), ("Open or usually opening in the next ~3 months", soon_), ("Later in the year", later_)):
        if items:
            L += [f"## {title}"] + [_opp_line(o) for o in items] + [""]
    return "\n".join(L)


# ---------------- program (hackathon / challenge) alerts ----------------
def _prog_subject(alerts):
    names = sorted({a["program"] for a in alerts})
    return f"🚨 {', '.join(names)}: registrations may be OPEN"


def _prog_when(a):
    parts = []
    if fmt_dt(a.get("published")):
        parts.append(f"headline published {fmt_dt(a['published'])}")
    if fmt_dt(a.get("detected")):
        parts.append(f"detected {fmt_dt(a['detected'])}")
    if a.get("more"):
        parts.append(f"+{a['more']} more signal{'s' if a['more'] > 1 else ''} merged into this alert")
    return " · ".join(parts)


def _prog_links(a):
    """(label, url) pairs: register/apply links found on the page, plus the official pages you configured."""
    out = [("Register / apply link", u) for u in a.get("links") or []]
    out += [("Official page", u) for u in a.get("official") or [] if u != a.get("url")]
    return out


def program_push_text(a):
    what = "Headline" if a["kind"] == "news" else "Official page"
    return (f"🚨 <b>{_e(a['program'])}</b> - possible opening\n{what}: {_e(a['text'])}\n"
            f"🕒 {_e(_prog_when(a))}\n{_e(a['batch'])}\n{_e(a['url'])}"
            + "".join(f"\n{lbl}: {_e(u)}" for lbl, u in _prog_links(a)))


def program_email(alerts):
    rows = "".join(
        f"<tr><td style='padding:10px 0;border-bottom:1px solid #eee'><b>{_e(a['program'])}</b><br>"
        f"{_e(a['text'])}<br><span style='color:#555;font-size:14px'>{_e(a['batch'])}</span><br>"
        f"<span style='color:#777;font-size:13px'>🕒 {_e(_prog_when(a))}</span><br>"
        f"<a href='{_e(a['url'])}'>{_e(a['url'])}</a>"
        + "".join(f"<br>{lbl}: <a href='{_e(u)}'>{_e(u)}</a>" for lbl, u in _prog_links(a)) + "</td></tr>"
        for a in alerts)
    text = "\n".join(f"{a['program']}: {a['text']}\n  {a['batch']}\n  {_prog_when(a)}\n  {a['url']}"
                     + "".join(f"\n  {lbl}: {u}" for lbl, u in _prog_links(a)) for a in alerts)
    return (_prog_subject(alerts), _wrap("Program alert", _section("Possible registration opening", rows)), text)


def program_markdown(alerts):
    L = _mention() + ["## Possible registration opening", ""]
    for a in alerts:
        L += [f"### {a['program']}", a["text"], "", f"🕒 {_prog_when(a)}", "", a["batch"], "", a["url"], ""]
        L += [f"- {lbl}: {u}" for lbl, u in _prog_links(a)] + [""]
    L.append("_You will not get another alert for this program for a while. Always confirm dates and eligibility on the official page._")
    return _prog_subject(alerts), "\n".join(L)
