"""Email (Gmail SMTP) + optional Telegram notifications and HTML rendering."""
import html
import os
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests


def _e(s):
    return html.escape(str(s if s is not None else ""))


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


def telegram_job_text(j):
    extra = f"\n<i>{_e(j['reason'])}</i>" if j.get("reason") else ""
    dl = f"\nDeadline: {_e(j['deadline'])}" if j.get("deadline") else ""
    return (f"🔔 <b>{_e(j['title'])}</b>\n{_e(j['company'])} · {_e(j['location'] or '-')} · match {j['score']}/10"
            f"{dl}{extra}\n{_e(j['url'])}")


def _job_html(j):
    bits = [j["company"], j["location"] or "-", f"match {j['score']}/10"]
    if j.get("deadline"):
        bits.append(f"deadline {j['deadline']}")
    reason = f"<br><span style='color:#777;font-size:13px'>{_e(j['reason'])}</span>" if j.get("reason") else ""
    return (f"<tr><td style='padding:10px 0;border-bottom:1px solid #eee'>"
            f"<a href='{_e(j['url'])}' style='font-weight:600;color:#1a56db;text-decoration:none'>{_e(j['title'])}</a>"
            f"<br><span style='color:#555;font-size:14px'>{_e(' · '.join(bits))}</span>{reason}</td></tr>")


def _job_text(j):
    return f"- {j['title']} @ {j['company']} ({j['location'] or '-'}) [{j['score']}/10]\n  {j['url']}"


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
