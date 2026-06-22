import os
import logging
import asyncio
import httpx
from datetime import datetime

logger = logging.getLogger("notify")


# ─── Core sender ───────────────────────────────────────────────────────────────

def _get_telegram_creds() -> tuple[str, str]:
    """Read credentials at call-time so .env changes are respected."""
    return (
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
    )


def send_telegram(message: str) -> bool:
    """
    Fire a single POST to Telegram using httpx (supports HTTPS on macOS without cert issues).
    Reads credentials at call-time so they're always fresh.
    """
    token, chat_id = _get_telegram_creds()
    if not token or not chat_id:
        logger.warning("[TELEGRAM] Token or Chat ID not set — skipping.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url,
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            )
        resp.raise_for_status()
        logger.info(f"[TELEGRAM] Message sent (HTTP {resp.status_code}).")
        return True
    except httpx.TimeoutException:
        logger.error("[TELEGRAM] Request timed out — Telegram unreachable from this network.")
        return False
    except httpx.HTTPStatusError as e:
        logger.error(f"[TELEGRAM] HTTP error {e.response.status_code}: {e.response.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"[TELEGRAM] Failed: {e}")
        return False


async def send_telegram_async(message: str) -> bool:
    """
    Async wrapper — runs the blocking send_telegram() in a thread executor
    with a 12-second timeout so it never blocks the event loop.
    """
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, send_telegram, message),
            timeout=12.0,
        )
    except asyncio.TimeoutError:
        logger.error("[TELEGRAM] Async send timed out (12s).")
        return False


# ─── WhatsApp via Twilio (#16) ────────────────────────────────────────────────

def send_whatsapp(to_phone: str, message: str) -> bool:
    """
    Send a WhatsApp message via Twilio.
    Requires env vars: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

    if not account_sid or not auth_token:
        logger.debug("[WHATSAPP] Twilio credentials not set — skipping.")
        return False

    to_wa = f"whatsapp:{to_phone}" if not to_phone.startswith("whatsapp:") else to_phone
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                auth=(account_sid, auth_token),
                data={"From": from_number, "To": to_wa, "Body": message},
            )
        resp.raise_for_status()
        logger.info(f"[WHATSAPP] Sent to {to_phone}: {resp.status_code}")
        return True
    except Exception as e:
        logger.error(f"[WHATSAPP] Failed to send to {to_phone}: {e}")
        return False


def send_whatsapp_booking_confirmation(
    caller_phone: str,
    caller_name: str,
    booking_time_iso: str,
) -> bool:
    """Send WhatsApp confirmation after a booking is made."""
    try:
        dt = datetime.fromisoformat(booking_time_iso)
        readable = dt.strftime("%A, %d %B %Y at %I:%M %p IST")
    except Exception:
        readable = booking_time_iso

    message = (
        f"✅ Hi {caller_name or 'there'}! Your appointment is *confirmed*.\n\n"
        f"📅 *Date & Time:* {readable}\n\n"
        f"If you need to reschedule or cancel, just call us back.\n\n"
        f"— RapidX AI 🤖"
    )
    return send_whatsapp(caller_phone, message)


# ─── Message Templates ─────────────────────────────────────────────────────────

def notify_booking_confirmed(
    caller_name: str,
    caller_phone: str,
    booking_time_iso: str,
    booking_id: str,
    notes: str = "",
    tts_voice: str = "",
    ai_summary: str = "",
) -> bool:
    """Sends Telegram + WhatsApp when a booking is confirmed."""
    try:
        dt = datetime.fromisoformat(booking_time_iso)
        readable = dt.strftime("%A, %d %B %Y at %-I:%M %p IST")
    except Exception:
        readable = booking_time_iso

    message = (
        f"✅ *New Booking Confirmed!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *Name:*        {caller_name}\n"
        f"📞 *Phone:*       `{caller_phone}`\n"
        f"📅 *Time:*        {readable}\n"
        f"🔖 *Booking ID:*  `{booking_id}`\n"
        f"📝 *Notes:*       {notes or '—'}\n"
        f"🎙️ *Voice Model:* {tts_voice or '—'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        + (f"💬 *AI Summary:*\n_{ai_summary}_\n\n" if ai_summary else "")
        + f"_Booked via RapidX AI Voice Agent_ 🤖"
    )
    tg_ok = send_telegram(message)
    send_whatsapp_booking_confirmation(caller_phone, caller_name, booking_time_iso)
    return tg_ok


async def notify_booking_confirmed_async(
    caller_name: str,
    caller_phone: str,
    booking_time_iso: str,
    booking_id: str,
    notes: str = "",
    tts_voice: str = "",
    ai_summary: str = "",
) -> bool:
    """Async version — runs in thread executor so it never blocks the event loop."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: notify_booking_confirmed(
                    caller_name, caller_phone, booking_time_iso,
                    booking_id, notes, tts_voice, ai_summary,
                ),
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("[TELEGRAM] notify_booking_confirmed_async timed out (15s).")
        return False


def notify_booking_cancelled(
    caller_name: str,
    caller_phone: str,
    booking_id: str,
    reason: str = "",
) -> bool:
    message = (
        f"❌ *Booking Cancelled*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *Name:*       {caller_name}\n"
        f"📞 *Phone:*      `{caller_phone}`\n"
        f"🔖 *Booking ID:* `{booking_id}`\n"
        f"💬 *Reason:*     {reason or 'Caller changed mind'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_RapidX AI Voice Agent_ 🤖"
    )
    return send_telegram(message)


def notify_call_no_booking(
    caller_name: str,
    caller_phone: str,
    call_summary: str = "",
    tts_voice: str = "",
    ai_summary: str = "",
    duration_seconds: int = 0,
) -> bool:
    message = (
        f"📵 *Call Ended — No Booking*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *Name:*        {caller_name or 'Unknown'}\n"
        f"📞 *Phone:*       `{caller_phone}`\n"
        f"⏱️ *Duration:*    {duration_seconds}s\n"
        f"🎙️ *Voice Model:* {tts_voice or '—'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        + f"💬 *Summary:*\n_{ai_summary or call_summary or 'Caller did not schedule.'}_\n\n"
        + f"_Consider a manual follow-up call_ 📲\n"
        f"_RapidX AI Voice Agent_ 🤖"
    )
    return send_telegram(message)


async def notify_call_no_booking_async(
    caller_name: str,
    caller_phone: str,
    call_summary: str = "",
    tts_voice: str = "",
    ai_summary: str = "",
    duration_seconds: int = 0,
) -> bool:
    """Async version — runs in thread executor so it never blocks the event loop."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: notify_call_no_booking(
                    caller_name, caller_phone, call_summary,
                    tts_voice, ai_summary, duration_seconds,
                ),
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("[TELEGRAM] notify_call_no_booking_async timed out (15s).")
        return False


def notify_agent_error(caller_phone: str, error: str) -> bool:
    message = (
        f"⚠️ *Agent Error During Call*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📞 *Phone:*  `{caller_phone}`\n"
        f"🔴 *Error:*  `{error}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"_RapidX AI Voice Agent_ 🤖"
    )
    return send_telegram(message)


# ─── n8n / Custom Webhook (#35) ──────────────────────────────────────────────

async def send_webhook(webhook_url: str, event_type: str, payload: dict) -> bool:
    """Deliver an event to a configurable webhook URL (for CRM embeds)."""
    if not webhook_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                webhook_url,
                json={
                    "event":     event_type,
                    "timestamp": datetime.utcnow().isoformat(),
                    "data":      payload,
                },
                headers={"Content-Type": "application/json"},
            )
            logger.info(f"[WEBHOOK] Delivered {event_type} → {resp.status_code}")
            return resp.status_code < 300
    except Exception as e:
        logger.warning(f"[WEBHOOK] Failed to deliver {event_type}: {e}")
        return False
