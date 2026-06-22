import os
import time
import logging
from typing import cast
from supabase import create_client, Client

logger = logging.getLogger("db")

# ─── Columns added by supabase_migration_v2.sql ───────────────────────────────
# If the migration hasn't been run yet, these columns won't exist.
# We detect PGRST204 (schema cache miss) and retry with just base columns.
_ANALYTICS_COLUMNS = {
    "sentiment", "was_booked", "interrupt_count",
    "estimated_cost_usd", "call_date", "call_hour", "call_day_of_week",
}
_BASE_COLUMNS = {"phone_number", "duration_seconds", "transcript", "summary",
                 "recording_url", "caller_name"}

# ─── Retry helper ─────────────────────────────────────────────────────────────
_MAX_RETRIES = 3
_RETRY_DELAYS = [1.0, 2.0, 4.0]   # seconds — covers transient SSL 525 errors


def _is_retryable(err_str: str) -> bool:
    """True if the error is a transient network or SSL failure worth retrying."""
    transient = ("525", "ssl", "timeout", "connection", "network", "502", "503", "504")
    el = err_str.lower()
    return any(k in el for k in transient)


def _is_schema_error(err_str: str) -> bool:
    """True if Supabase returned PGRST204 — column not found in schema cache."""
    return "PGRST204" in err_str or "schema cache" in err_str.lower()


# ─── Client ───────────────────────────────────────────────────────────────────

def get_supabase() -> Client | None:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        logger.error(f"Failed to init Supabase client: {e}")
        return None


# ─── save_call_log ────────────────────────────────────────────────────────────

def save_call_log(
    phone: str,
    duration: int,
    transcript: str,
    summary: str = "",
    recording_url: str = "",
    caller_name: str = "",
    sentiment: str = "unknown",
    estimated_cost_usd: float | None = None,
    call_date: str | None = None,
    call_hour: int | None = None,
    call_day_of_week: str | None = None,
    was_booked: bool = False,
    interrupt_count: int = 0,
) -> dict:
    """
    Insert a call log into Supabase.

    Strategy:
    1. Try with all columns (including analytics columns from migration_v2).
    2. If PGRST204 (column not in schema cache — migration not yet run),
       retry with only the base columns so the call is never silently lost.
    3. Retry up to 3× on transient SSL/network errors with exponential backoff.
    """
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        logger.info(f"Supabase not configured. Local log → {phone} {duration}s")
        return {"success": False, "message": "Supabase not configured"}

    supabase = get_supabase()
    if not supabase:
        return {"success": False, "message": "Supabase client failed"}

    # Build full payload
    full_data: dict = {
        "phone_number":    phone,
        "duration_seconds": duration,
        "transcript":      transcript,
        "summary":         summary,
        "sentiment":       sentiment,
        "was_booked":      was_booked,
        "interrupt_count": interrupt_count,
    }
    if recording_url:               full_data["recording_url"]      = recording_url
    if caller_name:                 full_data["caller_name"]         = caller_name
    if estimated_cost_usd is not None: full_data["estimated_cost_usd"] = estimated_cost_usd
    if call_date:                   full_data["call_date"]           = call_date
    if call_hour is not None:       full_data["call_hour"]           = call_hour
    if call_day_of_week:            full_data["call_day_of_week"]    = call_day_of_week

    # Base-only payload (fallback if migration not run)
    base_data: dict = {k: v for k, v in full_data.items() if k not in _ANALYTICS_COLUMNS}

    def _try_insert(data: dict, label: str) -> dict:
        for attempt in range(_MAX_RETRIES):
            try:
                res = supabase.table("call_logs").insert(data).execute()
                logger.info(f"Saved call log for {phone} ({label})")
                return {"success": True, "data": res.data}
            except Exception as e:
                err = str(e)
                if _is_schema_error(err):
                    # Column missing — propagate so caller can retry with base
                    raise RuntimeError("SCHEMA_ERROR:" + err)
                if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(f"Transient error (attempt {attempt+1}), retrying in {delay}s: {err[:80]}")
                    time.sleep(delay)
                    continue
                logger.error(f"Failed to save call log ({label}): {e}")
                return {"success": False, "message": err}
        return {"success": False, "message": "Max retries exceeded"}

    # Attempt 1: full payload
    try:
        return _try_insert(full_data, "full")
    except RuntimeError as e:
        err = str(e)
        if "SCHEMA_ERROR" in err:
            # Migration not run yet — fall back to base columns only
            logger.warning(
                "Analytics columns missing (run supabase_migration_v2.sql). "
                "Falling back to base columns for this call log."
            )
            return _try_insert(base_data, "base-fallback")
        raise


# ─── fetch_call_logs ──────────────────────────────────────────────────────────

def fetch_call_logs(limit: int = 50) -> list:
    supabase = get_supabase()
    if not supabase:
        return []
    for attempt in range(_MAX_RETRIES):
        try:
            res = (
                supabase.table("call_logs")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data
        except Exception as e:
            if _is_retryable(str(e)) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"Failed to fetch call logs: {e}")
            return []
    return []


# ─── fetch_bookings ───────────────────────────────────────────────────────────

def fetch_bookings() -> list:
    supabase = get_supabase()
    if not supabase:
        return []
    
    call_log_bookings = []
    enquiry_bookings = []

    # ── Primary source: call_logs with "Confirmed" in summary ────────────────
    try:
        res = (
            supabase.table("call_logs")
            .select("id, phone_number, caller_name, summary, created_at")
            .ilike("summary", "%Confirmed%")
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        for r in (res.data or []):
            call_log_bookings.append({
                "id":           r.get("id"),
                "phone_number": r.get("phone_number", ""),
                "caller_name":  r.get("caller_name", ""),
                "summary":      r.get("summary", ""),
                "created_at":   r.get("created_at", ""),
                "source":       "call_log",
            })
    except Exception as e:
        logger.error(f"Failed to fetch bookings from call_logs: {e}")

    # ── Secondary source: enquiries table with booking_confirmed=True ───────
    try:
        enq_res = (
            supabase.table("enquiries")
            .select("id, caller_phone, caller_name, booking_datetime, created_at, property_type, budget, location")
            .eq("booking_confirmed", True)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        for e in (enq_res.data or []):
            phone = e.get("caller_phone", "")
            bdt   = e.get("booking_datetime", "")
            summary = f"Booking Confirmed (Enquiry): {bdt}" if bdt else "Booking Confirmed (Enquiry)"
            enquiry_bookings.append({
                "id":           f"enq_{e.get('id')}",
                "phone_number": phone,
                "caller_name":  e.get("caller_name", ""),
                "summary":      summary,
                "created_at":   e.get("created_at", ""),
                "source":       "enquiry",
                "booking_datetime": bdt,
                "property_type": e.get("property_type", ""),
                "budget":        e.get("budget", ""),
                "location":      e.get("location", ""),
            })
    except Exception as e:
        logger.error(f"Failed to fetch bookings from enquiries: {e}")

    # Helper: phone number normalization
    def _norm_phone(phone: str) -> str:
        if not phone:
            return ""
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return digits

    # Helper: date parser
    from datetime import datetime
    def _parse_iso(dt_str: str) -> datetime:
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            return datetime.min

    merged = []
    used_enquiry_ids = set()

    # Match and merge call log bookings with enquiry bookings
    for cl in call_log_bookings:
        cl_phone_norm = _norm_phone(cl["phone_number"])
        cl_time = _parse_iso(cl["created_at"])
        
        matched_enq = None
        for enq in enquiry_bookings:
            enq_phone_norm = _norm_phone(enq["phone_number"])
            if cl_phone_norm and enq_phone_norm == cl_phone_norm:
                enq_time = _parse_iso(enq["created_at"])
                # Match if created_at is within 2 hours
                if abs((cl_time - enq_time).total_seconds()) < 7200:
                    matched_enq = enq
                    break
        
        if matched_enq:
            # Prefer details from the enquiry (has location, budget, booking_datetime)
            # but preserve Cal.com ID from the call log's summary
            merged.append({
                "id":           matched_enq["id"],
                "phone_number": matched_enq["phone_number"],
                "caller_name":  matched_enq["caller_name"] or cl["caller_name"],
                "summary":      cl["summary"],  # Cal.com booking ID
                "created_at":   matched_enq["created_at"],
                "source":       "enquiry",
                "booking_datetime": matched_enq["booking_datetime"],
                "property_type": matched_enq["property_type"],
                "budget":        matched_enq["budget"],
                "location":      matched_enq["location"],
                "call_log_id":   cl["id"],
            })
            used_enquiry_ids.add(matched_enq["id"])
        else:
            # Standalone call log booking (e.g. legacy/no matching enquiry record)
            # We don't have booking_datetime, so calendar will fall back to created_at
            merged.append(cl)

    # Append any enquiry bookings that were not matched to a call log
    for enq in enquiry_bookings:
        if enq["id"] not in used_enquiry_ids:
            merged.append(enq)

    merged.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return merged


# ─── fetch_stats ──────────────────────────────────────────────────────────────

def fetch_stats() -> dict:
    _empty = {"total_calls": 0, "total_bookings": 0, "avg_duration": 0, "booking_rate": 0}
    supabase = get_supabase()
    if not supabase:
        return _empty
    try:
        rows = cast(list[dict], (supabase.table("call_logs").select("duration_seconds, summary").execute()).data or [])
        total = len(rows)
        bookings = sum(1 for r in rows if "Confirmed" in r.get("summary", ""))
        durations = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
        avg_dur = round(sum(durations) / len(durations)) if durations else 0
        rate = round((bookings / total) * 100) if total else 0
        return {"total_calls": total, "total_bookings": bookings, "avg_duration": avg_dur, "booking_rate": rate}
    except Exception as e:
        logger.error(f"Failed to fetch stats: {e}")
        return _empty


# ─── save_enquiry ─────────────────────────────────────────────────────────────

def save_enquiry(
    caller_name:      str,
    caller_phone:     str,
    property_type:    str = "",
    location:         str = "",
    budget:           str = "",
    purpose:          str = "",
    requirements:     str = "",
    call_id:          str = "",
    booking_confirmed: bool = False,
    booking_datetime:  str  = "",
) -> dict:
    """Insert a property enquiry/lead into the enquiries table."""
    supabase = get_supabase()
    if not supabase:
        logger.info(f"Supabase not configured. Local enquiry log → {caller_phone}")
        return {"success": False, "message": "Supabase not configured"}
    data = {
        "caller_name":      caller_name,
        "caller_phone":     caller_phone,
        "property_type":    property_type,
        "location":         location,
        "budget":           budget,
        "purpose":          purpose,
        "requirements":     requirements,
        "call_id":          call_id,
        "booking_confirmed": booking_confirmed,
    }
    if booking_datetime:
        data["booking_datetime"] = booking_datetime
    for attempt in range(_MAX_RETRIES):
        try:
            res = supabase.table("enquiries").insert(data).execute()
            logger.info(f"[ENQUIRY] Saved for {caller_phone}")
            return {"success": True, "data": res.data}
        except Exception as e:
            err = str(e)
            if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"[ENQUIRY] Failed to save: {e}")
            return {"success": False, "message": err}
    return {"success": False, "message": "Max retries exceeded"}


# ─── update_enquiry_booking ────────────────────────────────────────────────────

def update_enquiry_booking(
    caller_phone:    str,
    booking_datetime: str,
    call_id:         str = "",
) -> dict:
    """Mark an existing enquiry as booking_confirmed=True and set the booking datetime."""
    supabase = get_supabase()
    if not supabase:
        return {"success": False, "message": "Supabase not configured"}
    for attempt in range(_MAX_RETRIES):
        try:
            query = supabase.table("enquiries").update({
                "booking_confirmed": True,
                "booking_datetime": booking_datetime,
            }).eq("caller_phone", caller_phone)
            if call_id:
                query = query.eq("call_id", call_id)
            res = query.execute()
            if res.data:
                logger.info(f"[ENQUIRY] Booking confirmed for {caller_phone} at {booking_datetime}")
                return {"success": True, "data": res.data}
            # No existing enquiry found — INSERT a new one so the record is never lost
            logger.warning(f"[ENQUIRY] No existing enquiry for {caller_phone} — inserting new record")
            ins_res = supabase.table("enquiries").insert({
                "caller_phone":     caller_phone,
                "caller_name":      "",
                "call_id":          call_id or "",
                "booking_confirmed": True,
                "booking_datetime": booking_datetime,
                "requirements":     "Auto-inserted by confirm_booking (save_lead_details not called)",
            }).execute()
            logger.info(f"[ENQUIRY] Inserted booking record for {caller_phone}")
            return {"success": True, "data": ins_res.data, "inserted": True}
        except Exception as e:
            err = str(e)
            if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"[ENQUIRY] Failed to update booking: {e}")
            return {"success": False, "message": err}
    return {"success": False, "message": "Max retries exceeded"}


# ─── fetch_enquiries ──────────────────────────────────────────────────────────

def fetch_enquiries(limit: int = 200) -> list:
    """Fetch all property enquiries ordered by newest first."""
    supabase = get_supabase()
    if not supabase:
        return []
    for attempt in range(_MAX_RETRIES):
        try:
            res = (
                supabase.table("enquiries")
                .select("*")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception as e:
            err = str(e)
            # Re-raise schema errors — table doesn't exist, API should know
            if "PGRST205" in err or "schema cache" in err.lower():
                raise RuntimeError("ENQUIRIES_TABLE_MISSING: " + err)
            if _is_retryable(err) and attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            logger.error(f"[ENQUIRY] Failed to fetch: {e}")
            return []
    return []
