"""Per-channel senders. Each takes a RESOLVED channel config (secrets already
decrypted to plaintext) plus a subject + text, and raises on failure.

Only Email (SMTP), Telegram (Bot API), and SMS (Twilio) are implemented so far;
WhatsApp / webhook / push are config-only until their senders are added here.
httpx is a core dependency; smtplib is stdlib (run off-thread so it never blocks
the event loop).
"""
from __future__ import annotations

import asyncio
import html

_TELEGRAM_LIMIT = 4096


def build_telegram_body(subject: str, text: str) -> str:
    """Assemble the HTML message body for Telegram (parse_mode=HTML), guaranteed
    <= _TELEGRAM_LIMIT chars and with balanced <b>/<pre> tags and whole entities.

    HTML parse mode + escaping every interpolated value: legacy Markdown 400s on
    ordinary content (a channel named "@Gold_Signals_VIP*" has unbalanced _ / *),
    which — since delivery is best-effort — silently dropped the alert (#39).

    Oversized detail is fitted by trimming the RAW text and escaping AFTER, so
    HTML entities never split and the tags stay balanced. The old guard (#39
    length limit) char-sliced the already-assembled HTML, cutting mid-<pre> or
    mid-entity into malformed HTML that Telegram 400s on and silently drops — the
    exact failure #39 was created to fix (#76). _head is bounded (built only from
    symbol/direction/label/P&L in dispatch.format_message)."""
    _head = html.escape(subject or "")
    body = f"<b>{_head}</b>"
    _detail = (text or "").strip()
    if _detail:
        _budget = _TELEGRAM_LIMIT - len(body) - len("\n<pre></pre>\n…(truncated)")
        _esc = html.escape(_detail)
        if len(_esc) > _budget:
            while _detail and len(html.escape(_detail)) > _budget:
                _detail = _detail[:-1]
            _esc = html.escape(_detail) + "\n…(truncated)"
        body += f"\n<pre>{_esc}</pre>"              # monospace -> columns align
    return body


async def send_telegram(cfg: dict, subject: str, text: str) -> None:
    import httpx
    token, chat = cfg.get("bot_token"), cfg.get("chat_id")
    if not token or not chat:
        raise ValueError("Telegram needs a bot_token and chat_id")
    body = build_telegram_body(subject, text)
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": str(chat), "text": body, "parse_mode": "HTML",
                  "disable_web_page_preview": True})
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram API {r.status_code}: {r.text[:200]}")


async def send_sms(cfg: dict, subject: str, text: str) -> None:
    import httpx
    sid, tok = cfg.get("account_sid"), cfg.get("auth_token")
    frm, to = cfg.get("from_number"), cfg.get("to_number")
    if not all([sid, tok, frm, to]):
        raise ValueError("SMS needs account_sid, auth_token, from_number, to_number")
    body = (f"{subject}\n{text}" if subject else text)[:1500]
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, tok), data={"From": frm, "To": to, "Body": body})
        if r.status_code >= 400:
            raise RuntimeError(f"Twilio API {r.status_code}: {r.text[:200]}")


def _send_email_blocking(cfg: dict, subject: str, text: str) -> None:
    import smtplib
    import ssl
    from email.message import EmailMessage

    host = cfg.get("smtp_host")
    port = int(cfg.get("smtp_port") or 587)
    user, pw = cfg.get("smtp_user"), cfg.get("smtp_password")
    frm = cfg.get("from_addr") or user
    tos = [a.strip() for a in str(cfg.get("to_addrs") or "").split(",") if a.strip()]
    if not host or not tos or not frm:
        raise ValueError("Email needs smtp_host, from_addr and at least one to address")

    msg = EmailMessage()
    msg["Subject"] = subject or "Beacon notification"
    msg["From"] = frm
    msg["To"] = ", ".join(tos)
    msg.set_content(text or "")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            if cfg.get("use_tls", True):
                s.starttls(context=ctx)
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)


async def send_email(cfg: dict, subject: str, text: str) -> None:
    await asyncio.to_thread(_send_email_blocking, cfg, subject, text)


# channel id -> async sender(cfg, subject, text). Absence == delivery not built.
SENDERS = {
    "email": send_email,
    "telegram": send_telegram,
    "sms": send_sms,
}
