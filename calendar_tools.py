import os
import logging
import time
import httpx
from datetime import datetime
import pytz

logger = logging.getLogger("calendar-tools")

# ── Cal.com API v2 base ────────────────────────────────────────────────────────
# v1 was DECOMMISSIONED on 2025-09-01 (returns HTTP 410).
# All calls now go to v2.
CAL_V2_BASE = "https://api.cal.com/v2"
IST = pytz.timezone("Asia/Kolkata")


def get_cal_creds() -> dict:
    return {
        "api_key":  os.environ.get("CAL_API_KEY", ""),
        "event_id": int(os.environ.get("CAL_EVENT_TYPE_ID", "0") or "0"),
    }


# ─── Cal.com v2: Get available slots ──────────────────────────────────────────

# ── Booking window constants ──────────────────────────────────────────────────
# Appointments are available 10:00 AM – 5:00 PM IST, every 30 minutes.
# Last slot: 5:00 PM (appointment may run until 5:30 PM).
BOOKING_HOUR_START = 10   # 10:00 AM IST
BOOKING_HOUR_END   = 17   # up to and including 5:00 PM IST


def _apply_booking_window(slots: list) -> list:
    """
    Filter a raw slot list to only include times within the booking window
    (10:00 AM – 5:00 PM IST) at 30-minute intervals.
    Slots that fall on :00 or :30 and are within the window are kept;
    everything else is discarded.
    """
    filtered = []
    for s in slots:
        t = s.get("time", "")
        try:
            dt = datetime.fromisoformat(t).astimezone(IST)
            # Must be on a 30-minute boundary
            if dt.minute not in (0, 30):
                continue
            # Must be at or after 10:00 AM
            if dt.hour < BOOKING_HOUR_START:
                continue
            # Must be at or before 5:00 PM (17:00); disallow 5:30 PM+
            if dt.hour > BOOKING_HOUR_END:
                continue
            if dt.hour == BOOKING_HOUR_END and dt.minute > 0:
                continue
            filtered.append(s)
        except Exception:
            continue
    return filtered


def get_available_slots(date_str: str) -> list:
    """
    Fetch open slots for a given date from Cal.com v2 OR Google Calendar.
    date_str: "YYYY-MM-DD" (IST local date)
    Returns list of dicts: [{"time": <ISO string in IST>, "label": "10:00 AM"}]
    Slots are capped to 10:00 AM – 5:00 PM IST, every 30 minutes.
    """
    t0 = time.perf_counter()

    # Try Google Calendar first if configured (#36)
    gcal_id    = os.environ.get("GOOGLE_CALENDAR_ID", "")
    gcal_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "google_creds.json")
    if gcal_id and os.path.exists(gcal_creds):
        try:
            result = _get_slots_gcal(date_str, gcal_id, gcal_creds)
            result = _apply_booking_window(result)
            logger.info(f"[GCAL] {len(result)} slots in window in {(time.perf_counter()-t0)*1000:.0f}ms")
            return result
        except Exception as e:
            logger.warning(f"[GCAL] Falling back to Cal.com: {e}")

    # Default: Cal.com v2
    raw    = _get_slots_calcom_v2(date_str)
    result = _apply_booking_window(raw)
    logger.info(f"[CAL] {len(result)}/{len(raw)} slots in window in {(time.perf_counter()-t0)*1000:.0f}ms")
    return result


def _get_slots_calcom_v2(date_str: str) -> list:
    """
    Call Cal.com /v2/slots with the correct v2 parameters.

    Key differences from the old dead v1 endpoint:
    - URL:    /v2/slots  (not /v1/slots)
    - Auth:   Authorization: Bearer <key>  (not ?apiKey=)
    - Params: 'start' / 'end'  (not 'startTime' / 'endTime')
    - Header: cal-api-version: 2024-09-04
    - Slot field: slot['start']  (not slot['time'])
    - Response: data[date_str] is a list of {"start": "<UTC ISO>"}
    """
    creds = get_cal_creds()
    if not creds["api_key"]:
        logger.error("[CAL] CAL_API_KEY not set — cannot fetch slots")
        return []
    if not creds["event_id"]:
        logger.error("[CAL] CAL_EVENT_TYPE_ID not set — cannot fetch slots")
        return []

    # Query the full IST day by spanning from midnight IST to 23:59 IST,
    # expressed in UTC so the API returns the right day's slots.
    # IST is UTC+5:30, so IST 00:00 = UTC 18:30 previous day.
    # Easiest: just pass ISO strings with Z and let Cal.com handle it.
    start_param = f"{date_str}T00:00:00+05:30"  # midnight IST
    end_param   = f"{date_str}T23:59:59+05:30"  # end of day IST

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{CAL_V2_BASE}/slots",
                headers={
                    "Authorization":   f"Bearer {creds['api_key']}",
                    "cal-api-version": "2024-09-04",
                },
                params={
                    "eventTypeId": creds["event_id"],
                    "start":       start_param,
                    "end":         end_param,
                },
            )
        logger.info(f"[CAL] /v2/slots → HTTP {resp.status_code}")
        if resp.status_code != 200:
            logger.error(f"[CAL] Slots error {resp.status_code}: {resp.text[:400]}")
            return []

        body = resp.json()
        # v2 response: {"data": {"YYYY-MM-DD": [{"start": "<UTC ISO>"}]}}
        raw_slots = body.get("data", {})
        # The date key in the response is UTC date; slots for IST day can span
        # two UTC dates. Collect all slots across all returned dates.
        all_slots: list = []
        for _date_key, slot_list in raw_slots.items():
            all_slots.extend(slot_list)

        slots = []
        for s in all_slots:
            utc_str = s.get("start")
            if not utc_str:
                continue
            try:
                # Parse UTC time, convert to IST for display
                dt_utc = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
                dt_ist = dt_utc.astimezone(IST)
                # Only include slots that fall on the requested IST date
                if dt_ist.strftime("%Y-%m-%d") != date_str:
                    continue
                slots.append({
                    "time":  dt_ist.isoformat(),
                    "label": dt_ist.strftime("%-I:%M %p"),
                })
            except Exception as parse_err:
                logger.debug(f"[CAL] Slot parse error: {parse_err} for '{utc_str}'")

        logger.info(f"[CAL] {len(slots)} slots on {date_str} (IST)")
        return slots

    except httpx.TimeoutException:
        logger.error("[CAL] Slots request timed out")
        return []
    except Exception as e:
        logger.error(f"[CAL] get_available_slots error: {e}", exc_info=True)
        return []


def _get_slots_gcal(date_str: str, calendar_id: str, creds_file: str) -> list:
    """
    Fetch busy slots from Google Calendar and compute free windows (#36).
    Requires: google-api-python-client, google-auth
    """
    from googleapiclient.discovery import build
    from google.oauth2 import service_account
    from datetime import timedelta

    creds = service_account.Credentials.from_service_account_file(
        creds_file,
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    service = build("calendar", "v3", credentials=creds)

    start = f"{date_str}T00:00:00+05:30"
    end   = f"{date_str}T23:59:59+05:30"

    result = service.freebusy().query(body={
        "timeMin": start,
        "timeMax": end,
        "items":   [{"id": calendar_id}],
    }).execute()

    busy_slots = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])

    # Generate free 30-min slots between 10:00 AM and 5:00 PM IST.
    # day_end is exclusive (loop is slot < day_end), so set it to 17:30
    # so the 17:00 (5:00 PM) slot is included as the last possible slot.
    day_start = IST.localize(datetime.strptime(f"{date_str} 10:00", "%Y-%m-%d %H:%M"))
    day_end   = IST.localize(datetime.strptime(f"{date_str} 17:30", "%Y-%m-%d %H:%M"))

    busy_ranges = []
    for b in busy_slots:
        bs = datetime.fromisoformat(b["start"]).astimezone(IST)
        be = datetime.fromisoformat(b["end"]).astimezone(IST)
        busy_ranges.append((bs, be))

    free_slots = []
    slot = day_start
    while slot < day_end:
        slot_end = slot + timedelta(minutes=30)
        is_busy = any(bs <= slot < be for bs, be in busy_ranges)
        if not is_busy:
            free_slots.append({
                "time":  slot.isoformat(),
                "label": slot.strftime("%-I:%M %p"),
            })
        slot = slot_end

    logger.info(f"[GCAL] {len(free_slots)} free slots for {date_str}")
    return free_slots


# ─── Create a booking ──────────────────────────────────────────────────────────

def create_booking(
    start_time: str,
    caller_name: str,
    caller_phone: str,
    notes: str = "",
) -> dict:
    """Synchronous wrapper — calls async_create_booking."""
    import asyncio
    try:
        return asyncio.get_event_loop().run_until_complete(
            async_create_booking(start_time, caller_name, caller_phone, notes)
        )
    except RuntimeError:
        return asyncio.run(async_create_booking(start_time, caller_name, caller_phone, notes))


async def async_create_booking(
    start_time: str,
    caller_name: str,
    caller_phone: str,
    notes: str = "",
) -> dict:
    """
    Book a slot — uses Google Calendar if configured, else Cal.com v2.
    start_time: ISO 8601 with IST offset e.g. "2026-06-17T10:00:00+05:30"
    Returns: {"success": bool, "booking_id": str|None, "message": str}
    """
    gcal_id    = os.environ.get("GOOGLE_CALENDAR_ID", "")
    gcal_creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "google_creds.json")

    if gcal_id and os.path.exists(gcal_creds):
        return await _create_booking_gcal(start_time, caller_name, caller_phone, notes, gcal_id, gcal_creds)

    return await _create_booking_calcom_v2(start_time, caller_name, caller_phone, notes)


async def _create_booking_calcom_v2(
    start_time: str, caller_name: str, caller_phone: str, notes: str
) -> dict:
    """
    Cal.com v2 booking.  Uses /v2/bookings with cal-api-version 2024-08-13.
    This endpoint was already correct in the original code.
    """
    creds = get_cal_creds()
    payload = {
        "eventTypeId": creds["event_id"],
        "start": start_time,
        "attendee": {
            "name":        caller_name,
            "email":       f"{caller_phone.replace('+','').replace(' ','')}@voiceagent.placeholder",
            "phoneNumber": caller_phone,
            "timeZone":    "Asia/Kolkata",
            "language":    "en",
        },
        "bookingFieldsResponses": {
            "notes": notes or f"Booked via AI voice agent. Phone: {caller_phone}",
        },
    }
    logger.info(f"[CAL] Creating booking for {caller_name} at {start_time}")
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{CAL_V2_BASE}/bookings",
                headers={
                    "Authorization":   f"Bearer {creds['api_key']}",
                    "cal-api-version": "2024-08-13",
                    "Content-Type":    "application/json",
                },
                json=payload,
            )
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(f"[CAL] Booking response HTTP {resp.status_code} in {elapsed:.0f}ms")
        if resp.status_code not in (200, 201):
            logger.error(f"[CAL] Booking failed {resp.status_code}: {resp.text[:400]}")
            return {"success": False, "booking_id": None, "message": resp.text}
        uid = resp.json().get("data", {}).get("uid", "unknown")
        logger.info(f"[CAL] Booking created: uid={uid}")
        return {"success": True, "booking_id": uid, "message": "Booking confirmed"}
    except httpx.TimeoutException:
        logger.error("[CAL] Booking request timed out")
        return {"success": False, "booking_id": None, "message": "Booking timed out."}
    except Exception as e:
        logger.error(f"[CAL] Booking error: {e}", exc_info=True)
        return {"success": False, "booking_id": None, "message": str(e)}


async def _create_booking_gcal(
    start_time: str,
    caller_name: str,
    caller_phone: str,
    notes: str,
    calendar_id: str,
    creds_file: str,
) -> dict:
    """Create a Google Calendar event (#36)."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        from datetime import timedelta

        creds = service_account.Credentials.from_service_account_file(
            creds_file,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        service = build("calendar", "v3", credentials=creds)

        dt_start = datetime.fromisoformat(start_time)
        dt_end   = dt_start + timedelta(minutes=30)

        event = {
            "summary":     f"Appointment — {caller_name}",
            "description": f"Phone: {caller_phone}\nNotes: {notes}\nBooked via RapidX AI Voice Agent",
            "start":       {"dateTime": dt_start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end":         {"dateTime": dt_end.isoformat(),   "timeZone": "Asia/Kolkata"},
            "attendees":   [{"displayName": caller_name, "comment": caller_phone}],
        }

        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        event_id = created.get("id", "unknown")
        logger.info(f"[GCAL] Event created: id={event_id}")
        return {"success": True, "booking_id": event_id, "message": "Google Calendar event created"}
    except Exception as e:
        logger.error(f"[GCAL] Create booking failed: {e}", exc_info=True)
        return {"success": False, "booking_id": None, "message": str(e)}


# ─── Cancel a booking ──────────────────────────────────────────────────────────

def cancel_booking(booking_id: str, reason: str = "Cancelled by caller") -> dict:
    """Cancel a Cal.com booking by UID via v2 API."""
    creds = get_cal_creds()
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.request(
                "DELETE",
                f"{CAL_V2_BASE}/bookings/{booking_id}/cancel",
                headers={
                    "Authorization":   f"Bearer {creds['api_key']}",
                    "cal-api-version": "2024-08-13",
                    "Content-Type":    "application/json",
                },
                json={"reason": reason},
            )
        resp.raise_for_status()
        logger.info(f"[CAL] Booking cancelled: {booking_id}")
        return {"success": True, "message": "Cancelled successfully"}
    except Exception as e:
        logger.error(f"[CAL] cancel_booking error: {e}")
        return {"success": False, "message": str(e)}
