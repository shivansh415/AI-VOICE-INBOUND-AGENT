import os
import json
import logging
import certifi
import pytz
import re
import asyncio
import time
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Annotated, cast

# Fix for macOS SSL certificate verification
os.environ["SSL_CERT_FILE"] = certifi.where()

# ── Sentry error tracking (#21) ───────────────────────────────────────────────
import sentry_sdk
_sentry_dsn = os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    sentry_sdk.init(
        dsn=_sentry_dsn,
        traces_sample_rate=0.1,
        integrations=[AsyncioIntegration()],
        environment=os.environ.get("ENVIRONMENT", "production"),
    )

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

load_dotenv()
logger = logging.getLogger("outbound-agent")
logging.basicConfig(level=logging.INFO)

from livekit import api
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
    llm,
)
from livekit.plugins import openai, sarvam, silero

CONFIG_FILE = "config.json"

# ── Rate limiting (#37) ───────────────────────────────────────────────────────
_call_timestamps: dict = defaultdict(list)
RATE_LIMIT_CALLS  = 5
RATE_LIMIT_WINDOW = 3600  # 1 hour

def is_rate_limited(phone: str) -> bool:
    if phone in ("unknown", "demo"):
        return False
    now = time.time()
    _call_timestamps[phone] = [t for t in _call_timestamps[phone] if now - t < RATE_LIMIT_WINDOW]
    if len(_call_timestamps[phone]) >= RATE_LIMIT_CALLS:
        return True
    _call_timestamps[phone].append(now)
    return False


# ── Config loader (#17 partial — per-client path awareness) ───────────────────
def get_live_config(phone_number: str | None = None):
    """Load config — tries per-client file first, then default config.json."""
    config = {}
    paths = []
    if phone_number and phone_number != "unknown":
        clean = phone_number.replace("+", "").replace(" ", "")
        paths.append(f"configs/{clean}.json")
    paths += ["configs/default.json", CONFIG_FILE]

    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                    logger.info(f"[CONFIG] Loaded: {path}")
                    break
            except Exception as e:
                logger.error(f"[CONFIG] Failed to read {path}: {e}")

    def _val(key, env_key=None, default=""):
        """Config.json if truthy, else .env, else default."""
        v = config.get(key)
        if v:
            return v
        if env_key:
            return os.getenv(env_key, default)
        return default

    resolved = {
        "agent_instructions":       _val("agent_instructions"),
        "stt_min_endpointing_delay":_val("stt_min_endpointing_delay", default=0.05),
        "llm_model":                _val("llm_model", "LLM_MODEL", "gpt-4o-mini"),
        "llm_provider":             _val("llm_provider", "LLM_PROVIDER", "openai"),
        "tts_voice":                _val("tts_voice", "TTS_VOICE", "kavya"),
        "tts_language":             _val("tts_language", "TTS_LANGUAGE", "hi-IN"),
        "tts_provider":             _val("tts_provider", default="sarvam"),
        "stt_provider":             _val("stt_provider", default="sarvam"),
        "stt_language":             _val("stt_language", default="unknown"),
        "lang_preset":              _val("lang_preset", default="multilingual"),
        "max_turns":                _val("max_turns", default=25),
        # Credential keys — fall through to .env when config.json has ""
        "livekit_url":              _val("livekit_url", "LIVEKIT_URL"),
        "livekit_api_key":          _val("livekit_api_key", "LIVEKIT_API_KEY"),
        "livekit_api_secret":       _val("livekit_api_secret", "LIVEKIT_API_SECRET"),
        "openai_api_key":           _val("openai_api_key", "OPENAI_API_KEY"),
        "azure_openai_api_key":     _val("azure_openai_api_key", "AZURE_OPENAI_API_KEY"),
        "azure_openai_endpoint":    _val("azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT"),
        "azure_openai_deployment":  _val("azure_openai_deployment", "AZURE_OPENAI_DEPLOYMENT"),
        "sarvam_api_key":           _val("sarvam_api_key", "SARVAM_API_KEY"),
        "cal_api_key":              _val("cal_api_key", "CAL_API_KEY"),
        "supabase_url":             _val("supabase_url", "SUPABASE_URL"),
        "supabase_key":             _val("supabase_key", "SUPABASE_KEY"),
        "telegram_bot_token":       _val("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id":         _val("telegram_chat_id", "TELEGRAM_CHAT_ID"),
    }

    # Raw config as base (extra keys like vobiz_*, first_line, etc.),
    # resolved values overlaid so .env fallbacks always win over empty strings.
    return {**config, **resolved}


# ── Token counter (#11) ───────────────────────────────────────────────────────
def count_tokens(text: str) -> int:
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        return len(enc.encode(text))
    except Exception:
        return len(text.split())


# ── IST time context ──────────────────────────────────────────────────────────
def get_ist_time_context() -> str:
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    today_str = now.strftime("%A, %B %d, %Y")
    time_str  = now.strftime("%I:%M %p")
    days_lines = []
    for i in range(7):
        day   = now + timedelta(days=i)
        label = "Today" if i == 0 else ("Tomorrow" if i == 1 else day.strftime("%A"))
        days_lines.append(f"  {label}: {day.strftime('%A %d %B %Y')} → ISO {day.strftime('%Y-%m-%d')}")
    days_block = "\n".join(days_lines)
    return (
        f"\n\n[SYSTEM CONTEXT]\n"
        f"Current date & time: {today_str} at {time_str} IST\n"
        f"Resolve ALL relative day references using this table:\n{days_block}\n"
        f"Always use ISO dates when calling save_booking_intent. Appointments in IST (+05:30).]"
    )


# ── Language presets ──────────────────────────────────────────────────────────
LANGUAGE_PRESETS = {
    "hinglish":    {"label": "Hinglish (Hindi+English)", "tts_language": "hi-IN", "tts_voice": "kavya",  "instruction": "Speak in natural Hinglish — mix Hindi and English like educated Indians do. Default to Hindi but use English words when more natural."},
    "hindi":       {"label": "Hindi",                   "tts_language": "hi-IN", "tts_voice": "ritu",   "instruction": "Speak only in pure Hindi. Avoid English words wherever a Hindi equivalent exists."},
    "english":     {"label": "English (India)",         "tts_language": "en-IN", "tts_voice": "dev",    "instruction": "Speak only in Indian English with a warm, professional tone."},
    "tamil":       {"label": "Tamil",                   "tts_language": "ta-IN", "tts_voice": "priya",  "instruction": "Speak only in Tamil. Use standard spoken Tamil for a professional context."},
    "telugu":      {"label": "Telugu",                  "tts_language": "te-IN", "tts_voice": "kavya",  "instruction": "Speak only in Telugu. Use clear, polite spoken Telugu."},
    "gujarati":    {"label": "Gujarati",                "tts_language": "gu-IN", "tts_voice": "rohan",  "instruction": "Speak only in Gujarati. Use polite, professional Gujarati."},
    "bengali":     {"label": "Bengali",                 "tts_language": "bn-IN", "tts_voice": "neha",   "instruction": "Speak only in Bengali (Bangla). Use standard, polite spoken Bengali."},
    "marathi":     {"label": "Marathi",                 "tts_language": "mr-IN", "tts_voice": "shubh",  "instruction": "Speak only in Marathi. Use polite, standard spoken Marathi."},
    "kannada":     {"label": "Kannada",                 "tts_language": "kn-IN", "tts_voice": "rahul",  "instruction": "Speak only in Kannada. Use clear, professional spoken Kannada."},
    "malayalam":   {"label": "Malayalam",               "tts_language": "ml-IN", "tts_voice": "ritu",   "instruction": "Speak only in Malayalam. Use polite, professional spoken Malayalam."},
    "multilingual":{"label": "Multilingual (Auto)",     "tts_language": "hi-IN", "tts_voice": "kavya",  "instruction": "Detect the caller's language from their first message and reply in that SAME language for the entire call. Supported: Hindi, Hinglish, English, Tamil, Telugu, Gujarati, Bengali, Marathi, Kannada, Malayalam. Switch if caller switches."},
}

def get_language_instruction(lang_preset: str) -> str:
    preset = LANGUAGE_PRESETS.get(lang_preset, LANGUAGE_PRESETS["multilingual"])
    return f"\n\n[LANGUAGE DIRECTIVE]\n{preset['instruction']}"


# ── External imports ──────────────────────────────────────────────────────────
import db
from calendar_tools import get_available_slots, create_booking, cancel_booking
from notify import (
    notify_booking_confirmed,
    notify_booking_confirmed_async,
    notify_booking_cancelled,
    notify_call_no_booking,
    notify_call_no_booking_async,
    notify_agent_error,
)


# ══════════════════════════════════════════════════════════════════════════════
# TOOL CONTEXT — All AI-callable functions
# ══════════════════════════════════════════════════════════════════════════════

class AgentTools(llm.ToolContext):

    def __init__(self, caller_phone: str, caller_name: str = ""):
        super().__init__([])
        self.caller_phone        = caller_phone
        self.caller_name         = caller_name
        self.booking_intent: dict | None = None
        self.booking_confirmed   = False   # True only after caller verbally confirms
        self.lead_saved          = False   # True after save_lead_details called
        self.lead_details: dict  = {}      # Stores enquiry info for DB
        self.farewell_asked      = False   # True after agent asked 'koi aur sawal?'
        self.sip_domain          = os.getenv("VOBIZ_SIP_DOMAIN")
        self.ctx_api             = None
        self.room_name           = None
        self._sip_identity       = None

    # ── Tool: Transfer to Human ───────────────────────────────────────────
    @llm.function_tool(description="Transfer this call to a human agent. Use if: caller asks for human, is angry, or query is outside scope.")
    async def transfer_call(self) -> str:
        logger.info("[TOOL] transfer_call triggered")
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER")
        if destination and self.sip_domain and "@" not in destination:
            clean_dest  = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean_dest}@{self.sip_domain}"
        if destination and not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        try:
            if self.ctx_api and self.room_name and destination and self._sip_identity:
                await self.ctx_api.sip.transfer_sip_participant(
                    api.TransferSIPParticipantRequest(
                        room_name=self.room_name,
                        participant_identity=self._sip_identity,
                        transfer_to=destination,
                        play_dialtone=False,
                    )
                )
                return "Transfer initiated successfully."
            return "Unable to transfer right now."
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            return "Unable to transfer right now."

    # ── Tool: End Call ────────────────────────────────────────────────────
    @llm.function_tool(description="Hang up the phone call. RULES: (1) NEVER call this in the same turn as save_booking_intent. (2) ONLY call AFTER: booking is confirmed AND you have asked the caller if they have any more questions AND you have said the goodbye. (3) If booking was saved but not confirmed yet, call confirm_booking first.")
    async def end_call(self) -> str:
        # ── Hard guard: block premature end_call if booking not confirmed ──
        if self.booking_intent and not self.booking_confirmed:
            logger.warning("[END-CALL] BLOCKED — booking saved but not yet confirmed by caller. Forcing confirmation step.")
            name  = self.booking_intent.get("caller_name", "")
            dt    = self.booking_intent.get("start_time", "")
            return (
                f"STOP — do NOT end the call yet. You MUST first verbally confirm the booking with "
                f"the caller. Say: 'Ji {name} ji, aapki appointment {dt} par Mahakal Properties "
                f"office mein schedule ho gayi hai. Kya yeh confirm hai?' "
                f"Only AFTER the caller says yes, call confirm_booking, then call end_call."
            )
        # ── Hard guard: block premature end_call if farewell not done ──
        if self.booking_confirmed and not self.farewell_asked:
            logger.warning("[END-CALL] BLOCKED — booking confirmed but farewell/question-check not done yet.")
            name = self.booking_intent.get("caller_name", "") if self.booking_intent else ""
            return (
                f"STOP — do NOT end the call yet. You MUST first: "
                f"(1) Say your warm goodbye: 'Bahut dhanyavaad {name} ji. Hum aapse appointment ke din milenge. Aapka din shubh rahe.' "
                f"(2) Then ask: 'Kya aapka koi aur sawal hai?' "
                f"(3) Wait for the caller. If they say no/nahi/theek hai, THEN call end_call."
            )
        logger.info("[TOOL] end_call triggered — hanging up.")
        # Wait for TTS to finish speaking the goodbye before disconnecting.
        # Without this delay, the SIP transfer fires instantly and the caller
        # gets cut off mid-sentence.
        await asyncio.sleep(8)
        try:
            if self.ctx_api and self.room_name and self._sip_identity:
                await self.ctx_api.sip.transfer_sip_participant(
                    api.TransferSIPParticipantRequest(
                        room_name=self.room_name,
                        participant_identity=self._sip_identity,
                        transfer_to="tel:+00000000",
                        play_dialtone=False,
                    )
                )
        except Exception as e:
            logger.warning(f"[END-CALL] SIP hangup failed: {e}")
        return "Call ended."

    # ── Tool: Save Booking Intent ─────────────────────────────────────────
    @llm.function_tool(description="Save booking intent. IMPORTANT: Before calling this, you MUST have: caller name, phone, date AND time. Do NOT fabricate a date. Do NOT call end_call in the same response. After calling this, verbally confirm the details to the caller and call confirm_booking only after they say yes.")
    async def save_booking_intent(
        self,
        start_time:  Annotated[str,  "ISO 8601 datetime in IST e.g. '2026-06-22T10:00:00+05:30'. MUST be the EXACT date/time the caller said. ALWAYS append +05:30 (IST offset)."],
        caller_name: Annotated[str,  "Full name of the caller"],
        caller_phone:Annotated[str,  "Phone number of the caller"],
        notes:       Annotated[str,  "Any notes, email, or special requests"] = "",
    ) -> str:
        # ── Force IST timezone on start_time (fix UTC-offset bug) ──
        import re as _re
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _IST = _td(hours=5, minutes=30)
        try:
            # Parse the incoming ISO string
            _raw = start_time.strip()
            # If it ends with Z or +00:00 (UTC), re-interpret the naive time as IST
            if _raw.endswith('Z') or _raw.endswith('+00:00'):
                _naive = _raw.rstrip('Z').replace('+00:00', '')
                # Remove sub-seconds if any
                _naive = _re.sub(r'\.\d+$', '', _naive)
                _parsed = _dt.fromisoformat(_naive)
                # Treat the hour/minute as the caller's intended IST time
                start_time = _parsed.replace(tzinfo=_tz(offset=_IST)).isoformat()
                logger.info(f"[BOOKING] Corrected UTC→IST: {_raw} → {start_time}")
            elif '+' not in _raw[10:] and _raw[-6] not in ('+', '-'):
                # No offset at all — assume IST
                _parsed = _dt.fromisoformat(_raw)
                start_time = _parsed.replace(tzinfo=_tz(offset=_IST)).isoformat()
                logger.info(f"[BOOKING] Added IST offset: {_raw} → {start_time}")
        except Exception as _e:
            logger.warning(f"[BOOKING] Could not parse/correct start_time '{start_time}': {_e}")

        logger.info(f"[TOOL] save_booking_intent: {caller_name} at {start_time}")
        try:
            self.booking_intent = {
                "start_time":   start_time,
                "caller_name":  caller_name,
                "caller_phone": caller_phone,
                "notes":        notes,
            }
            self.booking_confirmed = False  # Reset — must re-confirm with caller
            self.caller_name = caller_name

            # ── Auto-save lead enquiry if save_lead_details was never called ──
            # This ensures enquiry data is always captured even when LLM skips the step.
            if not self.lead_saved:
                logger.warning("[BOOKING] save_lead_details was not called — auto-saving minimal lead from booking intent.")
                try:
                    from db import save_enquiry as _se
                    import asyncio as _aio2
                    _call_id = getattr(self, 'call_id', '') or ''
                    _notes = notes or ""
                    _aio2.get_event_loop().run_in_executor(
                        None, lambda: _se(
                            caller_name=caller_name,
                            caller_phone=caller_phone,
                            requirements=_notes,
                            call_id=_call_id,
                        )
                    )
                    self.lead_saved = True
                    logger.info(f"[BOOKING] Auto-saved minimal lead for {caller_name}")
                except Exception as _le:
                    logger.warning(f"[BOOKING] Auto-lead save failed: {_le}")

            logger.info(f"[BOOKING] Intent saved. Waiting for caller verbal confirmation.")
            return (
                f"Booking saved for {caller_name} at {start_time}. "
                f"NOW you MUST verbally confirm with the caller — say exactly: "
                f"'Ji {caller_name} ji, aapki appointment {start_time} par Mahakal Properties office mein schedule ho gayi hai. Kya yeh confirm hai?' "
                f"Wait for the caller to say yes/haan/theek hai. "
                f"ONLY after they confirm, call confirm_booking. Do NOT call end_call yet."
            )
        except Exception as e:
            logger.error(f"[TOOL] save_booking_intent failed: {e}")
            return "I had trouble saving the booking. Please try again."

    # ── Tool: Confirm Booking ─────────────────────────────────────────────
    @llm.function_tool(description="Call this ONLY after the caller has verbally said yes/confirmed the booking details you just read out. This marks the booking as confirmed and allows end_call to proceed.")
    async def confirm_booking(self) -> str:
        if not self.booking_intent:
            return "No booking to confirm. Please save a booking first using save_booking_intent."
        self.booking_confirmed = True
        logger.info("[BOOKING] Caller confirmed booking — end_call is now permitted.")
        # Also update the enquiry record with booking confirmation
        try:
            from db import update_enquiry_booking
            import asyncio as _aio
            phone   = self.booking_intent.get("caller_phone", "")
            btime   = self.booking_intent.get("start_time", "")
            call_id = getattr(self, 'call_id', '') or ''
            # Pull all lead details collected during the call — these ensure the
            # fuzzy-match in update_enquiry_booking has the best chance of finding
            # the right row, and the fallback INSERT is never an "Unknown" ghost row.
            ld = self.lead_details or {}
            if phone and btime:
                _aio.get_event_loop().run_in_executor(
                    None, lambda: update_enquiry_booking(
                        caller_phone=phone,
                        booking_datetime=btime,
                        call_id=call_id,
                        caller_name=ld.get("caller_name") or self.booking_intent.get("caller_name", ""),
                        property_type=ld.get("property_type", ""),
                        location=ld.get("location", ""),
                        budget=ld.get("budget", ""),
                        purpose=ld.get("purpose", ""),
                        requirements=ld.get("requirements", ""),
                    )
                )
        except Exception as _e:
            logger.warning(f"[BOOKING] Could not update enquiry booking status: {_e}")
        name = self.booking_intent.get("caller_name", "") if self.booking_intent else ""
        self.farewell_asked = True   # Mark that the agent is now in farewell flow
        return (
            f"Booking confirmed by caller. "
            f"NOW follow these exact steps in order — do NOT skip any:\n"
            f"STEP A: Say goodbye warmly: 'Bahut dhanyavaad {name} ji. Hum aapse appointment ke din milenge. Aapka din shubh rahe.'\n"
            f"STEP B: Ask: 'Kya aapka koi aur sawal hai?'\n"
            f"STEP C: Wait for the caller to respond. If they say no/nahi/theek hai/okay — call end_call. If they have a question, answer it first, then call end_call."
        )


    # ── Tool: Save Lead Details (Enquiry) ─────────────────────────────
    @llm.function_tool(description="Save the caller's property enquiry details. Call this ONCE during the conversation after you have understood their requirements (property type, location, budget). Do NOT wait until the end — call this as soon as you have the key details.")
    async def save_lead_details(
        self,
        caller_name:    Annotated[str, "Full name of the caller"],
        caller_phone:   Annotated[str, "Phone number of the caller"],
        property_type:  Annotated[str, "Type: flat / plot / villa / commercial / other"],
        location:       Annotated[str, "Preferred location e.g. Vijay Nagar, Nipania, Super Corridor"],
        budget:         Annotated[str, "Budget as stated by caller e.g. '50 lakh', '1 crore', '80 lakh'"],
        purpose:        Annotated[str, "self-use or investment"],
        requirements:   Annotated[str, "Any additional requirements or notes from the caller"] = "",
    ) -> str:
        logger.info(f"[TOOL] save_lead_details: {caller_name} | {property_type} | budget={budget}")
        try:
            from db import save_enquiry
            self.lead_details = {
                "caller_name":   caller_name,
                "caller_phone":  caller_phone,
                "property_type": property_type,
                "location":      location,
                "budget":        budget,
                "purpose":       purpose,
                "requirements":  requirements,
            }
            self.caller_name = caller_name
            self.lead_saved  = True
            # Capture call_id for later booking linkage
            call_id = getattr(self, 'call_id', '') or ''
            # Save to Supabase asynchronously
            import asyncio as _aio
            _aio.get_event_loop().run_in_executor(
                None, lambda: save_enquiry(
                    caller_name=caller_name, caller_phone=caller_phone,
                    property_type=property_type, location=location,
                    budget=budget, purpose=purpose, requirements=requirements,
                    call_id=call_id,
                )
            )
            logger.info(f"[ENQUIRY] Saved lead for {caller_name}")
            return "Lead details saved. Continue the conversation naturally."
        except Exception as e:
            logger.error(f"[TOOL] save_lead_details failed: {e}")
            return "Noted. Continue the conversation."

    # ── Tool: Check Availability (#13) ────────────────────────────────────
    @llm.function_tool(description="Check available appointment slots for a given date. Call this when user asks about availability.")
    async def check_availability(
        self,
        date: Annotated[str, "Date to check in YYYY-MM-DD format e.g. '2026-06-17'"],
    ) -> str:
        t0 = time.perf_counter()
        logger.info(f"[TOOL] check_availability: date={date}")
        try:
            loop = asyncio.get_event_loop()
            slots = await loop.run_in_executor(None, get_available_slots, date)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(f"[TOOL] check_availability: {len(slots)} slots in {elapsed:.0f}ms")
            if not slots:
                return f"No available slots on {date}. Would you like to check another date?"
            # Return ALL slot labels so the LLM knows the exact available times.
            # IMPORTANT: include explicit instruction so the LLM does not offer any
            # time that is NOT in this list (prevents hallucinated slots like 10 AM
            # when only 9:45 and 10:15 appear).
            labels = [s.get("label") or s.get("time", "")[-8:][:5] for s in slots]
            return (
                f"Available slots on {date} (IST): {', '.join(labels)}. "
                f"IMPORTANT: These are the ONLY times available. "
                f"Do NOT offer or accept any time not in this exact list."
            )
        except Exception as e:
            logger.error(f"[TOOL] check_availability failed: {e}", exc_info=True)
            return "I'm having trouble checking the calendar right now."

    # ── Tool: Business Hours (#31) ────────────────────────────────────────
    @llm.function_tool(description="Check if the business is currently open and what the operating hours are.")
    async def get_business_hours(self) -> str:
        ist  = pytz.timezone("Asia/Kolkata")
        now  = datetime.now(ist)
        hours = {
            0: ("Monday",    "10:00", "19:00"),
            1: ("Tuesday",   "10:00", "19:00"),
            2: ("Wednesday", "10:00", "19:00"),
            3: ("Thursday",  "10:00", "19:00"),
            4: ("Friday",    "10:00", "19:00"),
            5: ("Saturday",  "10:00", "17:00"),
            6: ("Sunday",    None,    None),
        }
        day_name, open_t, close_t = hours[now.weekday()]
        current_time = now.strftime("%H:%M")
        if open_t is None or close_t is None:
            return "We are closed on Sundays. Next opening: Monday 10:00 AM IST."
        if open_t <= current_time <= close_t:
            return f"We are OPEN. Today ({day_name}): {open_t}–{close_t} IST."
        return f"We are CLOSED. Today ({day_name}): {open_t}–{close_t} IST."


# ══════════════════════════════════════════════════════════════════════════════
# AGENT CLASS
# ══════════════════════════════════════════════════════════════════════════════

class OutboundAssistant(Agent):

    def __init__(self, agent_tools: AgentTools, first_line: str = "", live_config: dict | None = None):
        tools = cast(list[llm.Tool | llm.Toolset], llm.find_function_tools(agent_tools))
        self._first_line  = first_line
        self._live_config = live_config or {}
        live_config_loaded = self._live_config

        base_instructions = live_config_loaded.get("agent_instructions", "")
        ist_context       = get_ist_time_context()
        lang_preset       = live_config_loaded.get("lang_preset", "multilingual")
        lang_instruction  = get_language_instruction(lang_preset)
        final_instructions = base_instructions + ist_context + lang_instruction

        # Token counter (#11)
        token_count = count_tokens(final_instructions)
        logger.info(f"[PROMPT] System prompt: {token_count} tokens")
        if token_count > 600:
            logger.warning(f"[PROMPT] Prompt exceeds 600 tokens — consider trimming for latency")

        super().__init__(instructions=final_instructions, tools=tools)

    async def on_enter(self):
        greeting = self._live_config.get(
            "first_line",
            self._first_line or (
                "Namaste! This is Aryan from RapidX AI — we help businesses automate with AI. "
                "May I ask what kind of business you run?"
            )
        )
        logger.info(f"[GREETING] on_enter fired — speaking greeting via TTS directly")
        try:
            # Use session.say() to pipe text DIRECTLY to TTS, bypassing the LLM.
            # generate_reply() wastes a full LLM round-trip (~2-3s) just to echo
            # a fixed string. session.say() goes straight to TTS in ~200-400ms.
            await self.session.say(greeting, allow_interruptions=True)
            logger.info("[GREETING] Greeting spoken successfully")
        except Exception as e:
            logger.error(f"[GREETING] session.say failed: {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

agent_is_speaking = False

async def entrypoint(ctx: JobContext):
    global agent_is_speaking

    # ── Connect ───────────────────────────────────────────────────────────
    await ctx.connect()
    logger.info(f"[ROOM] Connected: {ctx.room.name}")

    # ── Extract caller info ───────────────────────────────────────────────
    phone_number = None
    caller_name  = ""
    caller_phone = "unknown"

    # Try metadata first (outbound dispatch)
    metadata = ctx.job.metadata or ""
    if metadata:
        try:
            meta = json.loads(metadata)
            phone_number = meta.get("phone_number")
        except Exception:
            pass

    # Extract from SIP participants
    for identity, participant in ctx.room.remote_participants.items():
        # Name from caller ID (#32)
        if participant.name and participant.name not in ("", "Caller", "Unknown"):
            caller_name = participant.name
            logger.info(f"[CALLER-ID] Name from SIP: {caller_name}")
        if not phone_number:
            attr = participant.attributes or {}
            phone_number = attr.get("sip.phoneNumber") or attr.get("phoneNumber")
        if not phone_number and "+" in identity:
            import re as _re
            m = _re.search(r"\+\d{7,15}", identity)
            if m:
                phone_number = m.group()

    caller_phone = phone_number or "unknown"

    # ── Rate limiting (#37) ───────────────────────────────────────────────
    if is_rate_limited(caller_phone):
        logger.warning(f"[RATE-LIMIT] Blocked {caller_phone} — too many calls in 1h")
        return

    # ── Load config ───────────────────────────────────────────────────────
    live_config   = get_live_config(caller_phone)
    delay_setting = live_config.get("stt_min_endpointing_delay", 0.05)
    llm_model     = live_config.get("llm_model", "gpt-4o-mini")
    llm_provider  = live_config.get("llm_provider", "openai")
    tts_voice     = live_config.get("tts_voice", "kavya")
    tts_language  = live_config.get("tts_language", "hi-IN")
    tts_provider  = live_config.get("tts_provider", "sarvam")
    stt_provider  = live_config.get("stt_provider", "sarvam")
    stt_language  = live_config.get("stt_language", "unknown")  # auto-detect (#20)
    max_turns     = int(live_config.get("max_turns", 25))

    # ── Log the exact voice that will be used — visible on every call ─────
    logger.info(f"[TTS-VOICE] Will use voice='{tts_voice}' language='{tts_language}' provider='{tts_provider}'")

    # Override OS env vars from UI config
    for key in ["LIVEKIT_URL","LIVEKIT_API_KEY","LIVEKIT_API_SECRET","OPENAI_API_KEY",
                "AZURE_OPENAI_API_KEY","AZURE_OPENAI_ENDPOINT","AZURE_OPENAI_DEPLOYMENT",
                "SARVAM_API_KEY","CAL_API_KEY","TELEGRAM_BOT_TOKEN","SUPABASE_URL","SUPABASE_KEY"]:
        val = live_config.get(key.lower(), "")
        if val:
            os.environ[key] = val

    # ── Caller memory (#15) — fire-and-forget, never block session start ─
    # DNS/Supabase failures used to burn the full 5-second timeout on EVERY
    # call before the session even started, causing the 5-10s delay.
    # Now we start the session immediately and append history in the background
    # if/when it arrives.
    async def _load_and_apply_caller_history(phone: str):
        if phone == "unknown":
            return
        loop = asyncio.get_event_loop()
        def _fetch() -> str:
            try:
                sb = db.get_supabase()
                if not sb:
                    return ""
                result = (
                    sb.table("call_logs")
                      .select("summary, created_at")
                      .eq("phone_number", phone)
                      .order("created_at", desc=True)
                      .limit(1)
                      .execute()
                )
                if result.data:
                    last = cast(dict, result.data[0])
                    return f"\n\n[CALLER HISTORY: Last call {last['created_at'][:10]}. Summary: {last['summary']}]"
                return ""
            except Exception as e:
                logger.warning(f"[MEMORY] Could not load history: {e}")
                return ""
        try:
            history = await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=5.0)
            if history:
                logger.info(f"[MEMORY] Loaded caller history for {phone}")
                # Caller history appended after session start — no blocking effect
                live_config["agent_instructions"] = (
                    live_config.get("agent_instructions", "") + history
                )
        except asyncio.TimeoutError:
            logger.warning("[MEMORY] Supabase history load timed out — continuing without history")

    # Start loading history in background — don't await it here
    asyncio.create_task(_load_and_apply_caller_history(caller_phone))

    # ── Instantiate tools ─────────────────────────────────────────────────
    agent_tools = AgentTools(caller_phone=caller_phone, caller_name=caller_name)
    agent_tools._sip_identity = (
        f"sip_{caller_phone.replace('+','')}" if phone_number else "inbound_caller"
    )
    agent_tools.ctx_api   = ctx.api
    agent_tools.room_name = ctx.room.name

    # ── Build LLM (#8 Groq support) ───────────────────────────────────────
    if llm_provider == "groq":
        agent_llm = openai.LLM.with_groq(
            model=llm_model or "llama-3.3-70b-versatile",
            max_completion_tokens=120,
            temperature=0,   # deterministic = faster token sampling
        )
        logger.info(f"[LLM] Using Groq: {llm_model}")
    elif llm_provider == "claude":
        # Claude Haiku 3.5 via Anthropic API (#27)
        _anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        agent_llm = openai.LLM(
            model=llm_model or "claude-haiku-3-5-latest",
            base_url="https://api.anthropic.com/v1/",
            api_key=_anthropic_key,
            max_completion_tokens=120,
            temperature=0,   # deterministic = faster token sampling
        )
        logger.info(f"[LLM] Using Claude via Anthropic: {llm_model}")
    elif llm_provider == "azure":
        _azure_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        _azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
        _azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", llm_model or "gpt-4.1-mini")
        _azure_api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

        # New Azure AI Services endpoint (services.ai.azure.com/openai/v1) is
        # OpenAI-compatible — use base_url directly instead of with_azure(),
        # which would double-prefix the /openai/ path.
        if "/openai/v1" in _azure_endpoint or "services.ai.azure.com" in _azure_endpoint:
            agent_llm = openai.LLM(
                model=_azure_deployment,
                base_url=_azure_endpoint,
                api_key=_azure_key,
                max_completion_tokens=120,  # cap tokens for voice latency
                temperature=0,              # deterministic = faster token sampling
            )
            logger.info(f"[LLM] Using Azure AI Services (OpenAI-compat): {_azure_endpoint} / {_azure_deployment}")
        else:
            # Classic Azure OpenAI (*.openai.azure.com) — strip any extra path suffix
            if "/api/projects" in _azure_endpoint:
                _azure_endpoint = _azure_endpoint.split("/api/projects")[0]
            agent_llm = openai.LLM.with_azure(
                azure_deployment=_azure_deployment,
                azure_endpoint=_azure_endpoint,
                api_key=_azure_key,
                api_version=_azure_api_version,
                temperature=0,       # deterministic = faster token sampling
                # Note: with_azure() has no max_completion_tokens param.
                # Token cap is enforced on the other branch (openai.LLM + base_url)
                # which handles the actual active endpoint (services.ai.azure.com).
            )
            logger.info(f"[LLM] Using Azure OpenAI: deployment={_azure_deployment}")
    else:
        agent_llm = openai.LLM(
            model=llm_model,
            max_completion_tokens=120,
            temperature=0,  # deterministic = faster token sampling
        )  # cap tokens (#7)
        logger.info(f"[LLM] Using OpenAI: {llm_model}")

    # ── Build STT (#1 16kHz, #20 auto-detect, #9 Deepgram) ──────────────
    if stt_provider == "deepgram":
        try:
            from livekit.plugins import deepgram
            agent_stt = deepgram.STT(
                model="nova-2-general",
                language="multi",        # multilingual mode
                interim_results=False,
            )
            logger.info("[STT] Using Deepgram Nova-2")
        except ImportError:
            logger.warning("[STT] deepgram plugin not installed — falling back to Sarvam")
            agent_stt = sarvam.STT(
                language=stt_language,
                model="saaras:v3",
                mode="transcribe",  # single transcript per utterance
                flush_signal=True,
                sample_rate=16000,
            )
    else:
        agent_stt = sarvam.STT(
            language=stt_language,      # "unknown" = auto-detect (#20)
            model="saaras:v3",
            mode="transcribe",          # GPT-4.1-mini handles Hindi natively
            flush_signal=True,
            sample_rate=16000,          # force 16kHz (#1)
        )
        logger.info("[STT] Using Sarvam Saaras v3 (transcribe mode)")

    # ── Build TTS (#2 24kHz, #10 ElevenLabs) ────────────────────────────
    if tts_provider == "elevenlabs":
        try:
            from livekit.plugins import elevenlabs as _el_plugin
            _el_voice_id = (
                live_config.get("elevenlabs_voice_id")
                or os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
            )
            _el_api_key = os.environ.get("ELEVENLABS_API_KEY", "")
            if not _el_api_key or _el_api_key == "your_elevenlabs_api_key_here":
                raise ValueError("ELEVENLABS_API_KEY not set in .env")

            # ── Pre-flight: verify voice ID exists before the call starts ──
            import aiohttp, asyncio as _aio
            async def _check_el_voice():
                try:
                    async with aiohttp.ClientSession() as _s:
                        async with _s.post(
                            f"https://api.elevenlabs.io/v1/text-to-speech/{_el_voice_id}",
                            headers={"xi-api-key": _el_api_key, "Content-Type": "application/json"},
                            json={"text": " ", "model_id": "eleven_multilingual_v2"},
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as _r:
                            if _r.status == 400:
                                body = await _r.json()
                                if "voice_id_does_not_exist" in str(body):
                                    raise ValueError(f"ElevenLabs voice_id '{_el_voice_id}' does not exist on this account. "
                                                     "Go to elevenlabs.io → Voice Library → add a voice → copy its ID.")
                            return True
                except ValueError:
                    raise
                except Exception:
                    return True  # network issue during preflight — try anyway

            loop = _aio.get_event_loop()
            loop.run_until_complete(_check_el_voice()) if not loop.is_running() else None

            agent_tts = _el_plugin.TTS(
                model="eleven_multilingual_v2",  # best Hindi/multilingual quality
                voice_id=_el_voice_id,
                api_key=_el_api_key,
                voice_settings=_el_plugin.VoiceSettings(
                    stability=0.5,
                    similarity_boost=0.8,
                    style=0.2,
                    use_speaker_boost=True,
                ),
            )
            logger.info(f"[TTS] ✅ Using ElevenLabs Multilingual v2 — voice_id: {_el_voice_id}")
        except Exception as _el_err:
            logger.error(f"[TTS] ❌ ElevenLabs failed: {_el_err}")
            logger.warning("[TTS] Falling back to Sarvam Bulbul v3 for this call.")
            agent_tts = sarvam.TTS(
                target_language_code=tts_language,
                model="bulbul:v3",
                speaker=tts_voice,
                speech_sample_rate=24000,
            )
            logger.info(f"[TTS] Fallback active: Sarvam Bulbul v3 — voice: {tts_voice}")
    else:
        agent_tts = sarvam.TTS(
            target_language_code=tts_language,
            model="bulbul:v3",
            speaker=tts_voice,
            speech_sample_rate=24000,          # force 24kHz (#2)
        )
        logger.info(f"[TTS] Using Sarvam Bulbul v3 — voice: {tts_voice} lang: {tts_language}")

    # ── Sentence chunker — wire into session for lower TTS latency ───────
    # Splitting at sentence boundaries means TTS starts speaking the first
    # sentence while the LLM is still generating the rest, cutting perceived
    # latency by 300–800 ms on longer replies.
    def before_tts_cb(agent_response: str) -> str:
        # Split on sentence-ending punctuation (Hindi danda + Latin)
        sentences = re.split(r'(?<=[।.!?])\s+', agent_response.strip())
        first = sentences[0] if sentences else agent_response
        if len(sentences) > 1:
            logger.debug(f"[TTS-CHUNK] Chunked to first sentence ({len(first)} chars of {len(agent_response)})")
        return first

    # ── Turn counter + auto-close (#29) ──────────────────────────────────
    turn_count    = 0
    interrupt_count = 0  # (#30)
    _turn_start: float = 0.0  # for latency measurement

    # ── Build agent ───────────────────────────────────────────────────────
    agent = OutboundAssistant(
        agent_tools=agent_tools,
        first_line=live_config.get("first_line", ""),
        live_config=live_config,
    )

    # ── Build session (#3 noise cancellation attempted) ───────────────────
    try:
        from livekit.plugins import noise_cancellation as nc
        _noise_cancel = nc.BVC()
        logger.info("[AUDIO] BVC noise cancellation enabled")
    except Exception:
        _noise_cancel = None
        logger.info("[AUDIO] BVC not available — running without noise cancellation")

    room_input = RoomInputOptions(close_on_disconnect=False)
    if _noise_cancel:
        try:
            room_input = RoomInputOptions(close_on_disconnect=False, noise_cancellation=_noise_cancel)
        except Exception:
            room_input = RoomInputOptions(close_on_disconnect=False)

    # ── Upsert active_calls (#38) — fire-and-forget, don't block session ─
    # Run blocking Supabase I/O in a thread executor with timeout so that
    # DNS failures / network errors never stall the async event loop.
    _active_calls_table_ok = True  # flipped to False after first PGRST205 error
    async def upsert_active_call(status: str):
        nonlocal _active_calls_table_ok
        if not _active_calls_table_ok:
            return  # table doesn't exist — stop spamming
        loop = asyncio.get_event_loop()
        def _upsert():
            nonlocal _active_calls_table_ok
            try:
                sb = db.get_supabase()
                if sb:
                    sb.table("active_calls").upsert({
                        "room_id":      ctx.room.name,
                        "phone":        caller_phone,
                        "caller_name":  caller_name,
                        "status":       status,
                        "last_updated": datetime.utcnow().isoformat(),
                    }).execute()
            except Exception as e:
                if "PGRST205" in str(e) or "could not find" in str(e).lower():
                    _active_calls_table_ok = False
                    logger.debug("[ACTIVE-CALL] active_calls table not found — disabling upserts")
                else:
                    logger.warning(f"[ACTIVE-CALL] Supabase upsert failed ({status}): {e}")
        try:
            await asyncio.wait_for(loop.run_in_executor(None, _upsert), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"[ACTIVE-CALL] Supabase upsert timed out ({status})")

    # Fire-and-forget — don't await so it can't delay the greeting
    asyncio.create_task(upsert_active_call("active"))

    session = AgentSession(
        stt=agent_stt,
        llm=agent_llm,
        tts=agent_tts,
        turn_detection="stt",
        min_endpointing_delay=float(delay_setting),  # default 0.05 s (#6)
        allow_interruptions=True,
        # Note: before_tts_cb is not supported in livekit-agents v1.4.
        # Sentence-length is controlled via max_completion_tokens=120 on the LLM.
    )

    await session.start(room=ctx.room, agent=agent, room_input_options=room_input)


    # ── TTS pre-warm (#12) ────────────────────────────────────────────────
    # Guard: prewarm() on some plugins (e.g. Sarvam) returns None, not a
    # coroutine. Awaiting None raises "object NoneType can't be used in
    # 'await' expression". Check with iscoroutine() before awaiting.
    try:
        prewarm_result = None
        if hasattr(session, "tts") and hasattr(session.tts, "prewarm"):
            prewarm_result = session.tts.prewarm()
        elif hasattr(agent_tts, "prewarm"):
            prewarm_result = agent_tts.prewarm()
        if asyncio.iscoroutine(prewarm_result):
            await prewarm_result
            logger.info("[TTS] Pre-warmed successfully")
        elif prewarm_result is not None:
            logger.info("[TTS] Pre-warm returned non-coroutine (sync) — skipping await")
        else:
            logger.debug("[TTS] Plugin has no prewarm or it returned None — skipping")
    except Exception as e:
        logger.warning(f"[TTS] Pre-warm error: {e}")

    logger.info("[AGENT] Session live — waiting for caller audio.")
    call_start_time = datetime.now(pytz.utc)

    # ── Recording → Supabase Storage ─────────────────────────────────────
    # Requires 4 S3 env vars. If any are missing we skip recording and log
    # which variable is absent — the silent KeyError was the original bug
    # that caused some calls to have no Recording button in the UI.
    egress_id = None
    _s3_access  = os.environ.get("SUPABASE_S3_ACCESS_KEY", "")
    _s3_secret  = os.environ.get("SUPABASE_S3_SECRET_KEY", "")
    _s3_endpoint = os.environ.get("SUPABASE_S3_ENDPOINT", "")
    _s3_region  = os.environ.get("SUPABASE_S3_REGION", "ap-south-1")
    _missing_s3 = [k for k, v in {
        "SUPABASE_S3_ACCESS_KEY": _s3_access,
        "SUPABASE_S3_SECRET_KEY": _s3_secret,
        "SUPABASE_S3_ENDPOINT":   _s3_endpoint,
    }.items() if not v]
    if _missing_s3:
        logger.warning(f"[RECORDING] Skipped — missing env vars: {_missing_s3}")
    else:
        rec_api = api.LiveKitAPI(
            url=os.environ["LIVEKIT_URL"],
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
        )
        try:
            egress_resp = await rec_api.egress.start_room_composite_egress(
                api.RoomCompositeEgressRequest(
                    room_name=ctx.room.name,
                    audio_only=True,
                    file_outputs=[api.EncodedFileOutput(
                        file_type=api.EncodedFileType.OGG,
                        filepath=f"recordings/{ctx.room.name}.ogg",
                        s3=api.S3Upload(
                            access_key=_s3_access,
                            secret=_s3_secret,
                            bucket="call-recordings",
                            region=_s3_region,
                            endpoint=_s3_endpoint,
                            force_path_style=True,
                        )
                    )]
                )
            )
            egress_id = egress_resp.egress_id
            logger.info(f"[RECORDING] Started egress: {egress_id}")
        except Exception as e:
            logger.warning(f"[RECORDING] Failed to start recording: {e}")
        finally:
            await rec_api.aclose()   # always close — avoids unclosed connector errors

    # ── Real-time transcript streaming (#33) ─────────────────────────────
    _transcript_table_ok = True  # flipped to False after first insert failure
    async def _log_transcript(role: str, content: str):
        nonlocal _transcript_table_ok
        if not _transcript_table_ok:
            return  # table doesn't exist — stop spamming
        loop = asyncio.get_event_loop()
        def _insert():
            nonlocal _transcript_table_ok
            try:
                sb = db.get_supabase()
                if sb:
                    sb.table("call_transcripts").insert({
                        "call_room_id": ctx.room.name,
                        "phone":        caller_phone,
                        "role":         role,
                        "content":      content,
                    }).execute()
            except Exception as e:
                if "PGRST205" in str(e) or "could not find" in str(e).lower():
                    _transcript_table_ok = False
                    logger.debug("[TRANSCRIPT-STREAM] call_transcripts table not found — disabling live transcripts")
                else:
                    logger.debug(f"[TRANSCRIPT-STREAM] Insert failed: {e}")
        try:
            await asyncio.wait_for(loop.run_in_executor(None, _insert), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug("[TRANSCRIPT-STREAM] Insert timed out")

    # ── Session event handlers ────────────────────────────────────────────
    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev):
        global agent_is_speaking
        nonlocal interrupt_count
        old = getattr(ev, "old_state", "?")
        new = ev.new_state
        logger.info(f"[STATE] {old} → {new}")
        if new == "speaking":
            agent_is_speaking = True
        elif new in ("thinking", "listening"):
            agent_is_speaking = False
        if old == "speaking" and new == "listening":
            interrupt_count += 1
            logger.info(f"[INTERRUPT] Agent interrupted. Total: {interrupt_count}")

    @session.on("conversation_item_added")
    def _on_agent_speech_committed(ev):
        """Log what the agent actually said so we can confirm LLM→TTS is working."""
        try:
            if getattr(ev.item, "role", None) != "assistant":
                return
            content = getattr(ev.item, "text_content", None) or ""
            elapsed = (time.perf_counter() - _turn_start) * 1000 if _turn_start else 0
            logger.info(f"[AGENT-REPLY] ({elapsed:.0f}ms) '{content[:160]}'")
            if content:
                asyncio.create_task(_log_transcript("assistant", content))
        except Exception as e:
            logger.warning(f"[AGENT-REPLY] Could not read committed speech: {e}")

    FILLER_WORDS = {
        "okay.", "okay", "ok", "uh", "hmm", "hm", "yeah", "yes",
        "no", "um", "ah", "oh", "right", "sure", "fine", "good",
        "haan", "han", "theek", "theek hai", "accha", "ji", "ha",
    }

    @session.on("user_input_transcribed")
    def on_user_speech_committed(ev):
        nonlocal turn_count, _turn_start
        global agent_is_speaking

        # Only process final transcripts
        if not ev.is_final:
            return

        transcript = ev.transcript.strip()
        transcript_lower = transcript.lower().rstrip(".")

        if agent_is_speaking:
            logger.debug(f"[FILTER-ECHO] Dropped: '{transcript}'")
            return
        if not transcript or len(transcript) < 3:
            return
        if transcript_lower in FILLER_WORDS:
            logger.debug(f"[FILTER-FILLER] Dropped: '{transcript}'")
            return

        # Mark when this user turn arrived so we can log LLM latency
        _turn_start = time.perf_counter()

        # Real-time transcript stream
        asyncio.create_task(_log_transcript("user", transcript))

        # Turn counter + auto-close (#29)
        turn_count += 1
        logger.info(f"[TRANSCRIPT] Turn {turn_count}/{max_turns}: '{transcript}'")
        if turn_count >= max_turns:
            logger.info(f"[LIMIT] Reached {max_turns} turns — wrapping up")
            session.generate_reply(
                instructions="Politely wrap up: thank the caller, say they can call back anytime, and say a warm goodbye."
            )

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        global agent_is_speaking
        logger.info(f"[HANGUP] Participant disconnected: {participant.identity}")
        agent_is_speaking = False
        asyncio.create_task(unified_shutdown_hook())

    # ══════════════════════════════════════════════════════════════════════
    # POST-CALL SHUTDOWN HOOK
    # ══════════════════════════════════════════════════════════════════════

    async def unified_shutdown_hook():
        logger.info("[SHUTDOWN] Sequence started.")

        duration = int((datetime.now(pytz.utc) - call_start_time).total_seconds())

        # Booking
        booking_status_msg = "No booking"
        if agent_tools.booking_intent:
            from calendar_tools import async_create_booking
            intent = agent_tools.booking_intent
            result = await async_create_booking(
                start_time=intent["start_time"],
                caller_name=intent["caller_name"] or "Unknown Caller",
                caller_phone=intent["caller_phone"],
                notes=intent["notes"],
            )
            if result.get("success"):
                # Use async version so Telegram/WhatsApp send never blocks the event loop
                asyncio.create_task(notify_booking_confirmed_async(
                    caller_name=intent["caller_name"],
                    caller_phone=intent["caller_phone"],
                    booking_time_iso=intent["start_time"],
                    booking_id=result.get("booking_id", ""),
                    notes=intent["notes"],
                    tts_voice=tts_voice,
                    ai_summary="",
                ))
                booking_status_msg = f"Booking Confirmed: {result.get('booking_id')}"
            else:
                booking_status_msg = f"Booking Failed: {result.get('message')}"
        else:
            # Use async version so it never blocks shutdown
            asyncio.create_task(notify_call_no_booking_async(
                caller_name=agent_tools.caller_name,
                caller_phone=agent_tools.caller_phone,
                call_summary="Caller did not schedule during this call.",
                tts_voice=tts_voice,
                duration_seconds=duration,
            ))

        # Build transcript
        transcript_text = ""
        try:
            # Use session.history (authoritative in livekit-agents v1.4)
            messages = session.history.messages()
            lines = []
            for msg in messages:
                role = getattr(msg, "role", None)
                if role not in ("user", "assistant"):
                    continue
                content = msg.text_content or ""
                if not content:
                    raw = getattr(msg, "content", [])
                    if isinstance(raw, list):
                        content = " ".join(str(c) for c in raw if isinstance(c, str))
                    else:
                        content = str(raw)
                lines.append(f"[{str(role).upper()}] {content}")
            transcript_text = "\n".join(lines)
        except Exception as e:
            logger.error(f"[SHUTDOWN] Transcript read failed: {e}")
            transcript_text = "unavailable"

        # Sentiment analysis (#14) — 5s timeout so it never blocks process exit
        sentiment = "unknown"
        if transcript_text and transcript_text != "unavailable":
            try:
                import openai as _oai
                _azure_sentiment_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
                if _azure_sentiment_key:
                    _s_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
                    _sentiment_model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
                    if "/openai/v1" in _s_endpoint or "services.ai.azure.com" in _s_endpoint:
                        _client = _oai.AsyncOpenAI(
                            api_key=_azure_sentiment_key,
                            base_url=_s_endpoint,
                        )
                    else:
                        if "/api/projects" in _s_endpoint:
                            _s_endpoint = _s_endpoint.split("/api/projects")[0]
                        _client = _oai.AsyncAzureOpenAI(
                            api_key=_azure_sentiment_key,
                            azure_endpoint=_s_endpoint,
                            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
                        )
                else:
                    _client = _oai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
                    _sentiment_model = "gpt-4o-mini"
                resp = await asyncio.wait_for(
                    _client.chat.completions.create(
                        model=_sentiment_model, max_tokens=5,
                        messages=[{"role":"user","content":
                            f"Classify this call as one word: positive, neutral, negative, or frustrated.\n\n{transcript_text[:800]}"}]
                    ),
                    timeout=5.0,
                )
                sentiment = resp.choices[0].message.content.strip().lower()
                logger.info(f"[SENTIMENT] {sentiment}")
            except asyncio.TimeoutError:
                logger.warning("[SENTIMENT] Timed out (5s) — skipping")
            except Exception as e:
                logger.warning(f"[SENTIMENT] Failed: {e}")

        # Cost estimation (#34)
        def estimate_cost(dur: int, chars: int) -> float:
            return round(
                (dur / 60) * 0.002 +
                (dur / 60) * 0.006 +
                (chars / 1000) * 0.003 +
                (chars / 4000) * 0.0001,
                5
            )
        estimated_cost = estimate_cost(duration, len(transcript_text))
        logger.info(f"[COST] Estimated: ${estimated_cost}")

        # Analytics timestamps (#19)
        ist = pytz.timezone("Asia/Kolkata")
        call_dt = call_start_time.astimezone(ist)

        # Stop recording — 4s timeout so a LiveKit API hiccup can't block shutdown
        recording_url = ""
        if egress_id:
            stop_api = api.LiveKitAPI(
                url=os.environ["LIVEKIT_URL"],
                api_key=os.environ["LIVEKIT_API_KEY"],
                api_secret=os.environ["LIVEKIT_API_SECRET"],
            )
            try:
                await asyncio.wait_for(
                    stop_api.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id)),
                    timeout=4.0,
                )
                recording_url = (
                    f"{os.environ.get('SUPABASE_URL','')}/storage/v1/object/public/"
                    f"call-recordings/recordings/{ctx.room.name}.ogg"
                )
                logger.info(f"[RECORDING] Stopped. URL: {recording_url}")
            except asyncio.TimeoutError:
                logger.warning("[RECORDING] Stop egress timed out (4s)")
            except Exception as e:
                logger.warning(f"[RECORDING] Stop failed: {e}")
            finally:
                await stop_api.aclose()  # always close — avoids unclosed connector errors


        # Update active_calls to completed (#38) — fire-and-forget
        asyncio.create_task(upsert_active_call("completed"))

        # n8n webhook (#39)
        _n8n_url = os.getenv("N8N_WEBHOOK_URL")
        if _n8n_url:
            try:
                import httpx
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: httpx.post(_n8n_url, json={
                        "event":        "call_completed",
                        "phone":        caller_phone,
                        "caller_name":  agent_tools.caller_name,
                        "duration":     duration,
                        "booked":       bool(agent_tools.booking_intent),
                        "sentiment":    sentiment,
                        "summary":      booking_status_msg,
                        "recording_url":recording_url,
                        "interrupt_count": interrupt_count,
                    }, timeout=5.0)
                )
                logger.info("[N8N] Webhook triggered")
            except Exception as e:
                logger.warning(f"[N8N] Webhook failed: {e}")

        # Save to Supabase — run in thread executor so DNS failures never block
        # the event loop. cap at 8s so the process can exit cleanly.
        from db import save_call_log
        loop = asyncio.get_event_loop()
        def _save():
            save_call_log(
                phone=caller_phone,
                duration=duration,
                transcript=transcript_text,
                summary=booking_status_msg,
                recording_url=recording_url,
                caller_name=agent_tools.caller_name or "",
                sentiment=sentiment,
                estimated_cost_usd=estimated_cost,
                call_date=call_dt.date().isoformat(),
                call_hour=call_dt.hour,
                call_day_of_week=call_dt.strftime("%A"),
                was_booked=bool(agent_tools.booking_intent),
                interrupt_count=interrupt_count,
            )
        try:
            await asyncio.wait_for(loop.run_in_executor(None, _save), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("[SHUTDOWN] Supabase save_call_log timed out (8s) — log not saved")
        except Exception as e:
            logger.error(f"[SHUTDOWN] save_call_log error: {e}")

        # ── Auto-save enquiry if save_lead_details was never called ──────────
        # This happens when the LLM skips the lead-save step and goes straight
        # to booking. We still want a record in the enquiries table.
        if not agent_tools.lead_saved and not agent_tools.booking_confirmed:
            _auto_name  = agent_tools.caller_name or ""
            _auto_phone = caller_phone
            _booking_ok = booking_status_msg.startswith("Booking Confirmed")
            _booking_dt = ""
            if agent_tools.booking_intent and _booking_ok:
                _booking_dt = agent_tools.booking_intent.get("start_time", "")
            from db import save_enquiry as _save_enquiry
            def _auto_save_enquiry():
                _save_enquiry(
                    caller_name=_auto_name or "Unknown",
                    caller_phone=_auto_phone,
                    requirements="Auto-saved: lead details not collected during call",
                    booking_confirmed=_booking_ok,
                    booking_datetime=_booking_dt,
                )
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _auto_save_enquiry), timeout=5.0
                )
                logger.info("[SHUTDOWN] Auto-saved minimal enquiry (save_lead_details not called)")
            except asyncio.TimeoutError:
                logger.warning("[SHUTDOWN] Auto-enquiry save timed out")
            except Exception as _ae:
                logger.warning(f"[SHUTDOWN] Auto-enquiry save failed: {_ae}")

    # NOTE: Do NOT register ctx.add_shutdown_callback(unified_shutdown_hook).
    # The participant_disconnected event above already triggers unified_shutdown_hook
    # via asyncio.create_task(). Registering it here too causes it to run TWICE —
    # the second run blocks on Supabase/Telegram and causes
    # 'process did not exit in time, killing process', which kills the LIVE call.


# ══════════════════════════════════════════════════════════════════════════════
# WORKER ENTRY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="outbound-caller",
    ))
