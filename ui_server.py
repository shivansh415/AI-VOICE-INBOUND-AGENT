import json
import logging
import os
from typing import cast
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ui-server")

app = FastAPI(title="NOVA AI Dashboard")

CONFIG_FILE = "config.json"

def _sanitize_supabase_url(url: str) -> str:
    """Convert Supabase dashboard URLs to proper API URLs.

    e.g. https://supabase.com/dashboard/project/dkrfxofliaefnylflbkw/
      → https://dkrfxofliaefnylflbkw.supabase.co
    """
    if not url:
        return url
    if "supabase.com/dashboard/project/" in url:
        # Extract project ref from dashboard URL
        try:
            parts = url.rstrip("/").split("/")
            project_ref = parts[parts.index("project") + 1]
            return f"https://{project_ref}.supabase.co"
        except (ValueError, IndexError):
            pass
    return url.rstrip("/")

def read_config():
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)

    def get_val(key, env_key, default=""):
        """Return config.json value if truthy, else fall back to .env / default."""
        val = config.get(key)
        if val:                       # non-empty string / non-zero → use it
            return val
        return os.getenv(env_key, default)

    raw_supabase_url = get_val("supabase_url", "SUPABASE_URL", "")
    supabase_url = _sanitize_supabase_url(raw_supabase_url)

    # Build the resolved dict — get_val ensures .env values fill in any
    # empty-string slots in config.json so the UI always shows real values.
    resolved = {
        "first_line": get_val("first_line", "FIRST_LINE", "Namaste! This is Aryan from NOVA AI — we help businesses automate with AI. Hmm, may I ask what kind of business you run?"),
        "agent_instructions": get_val("agent_instructions", "AGENT_INSTRUCTIONS", ""),
        "stt_min_endpointing_delay": float(get_val("stt_min_endpointing_delay", "STT_MIN_ENDPOINTING_DELAY", 0.6)),
        "llm_model": get_val("llm_model", "LLM_MODEL", "gpt-4.1-mini"),
        "llm_provider": get_val("llm_provider", "LLM_PROVIDER", "azure"),
        "tts_provider": get_val("tts_provider", "TTS_PROVIDER", "sarvam"),
        "tts_voice": get_val("tts_voice", "TTS_VOICE", "kavya"),
        "tts_language": get_val("tts_language", "TTS_LANGUAGE", "hi-IN"),
        "elevenlabs_voice_id": get_val("elevenlabs_voice_id", "ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        "livekit_url": get_val("livekit_url", "LIVEKIT_URL", ""),
        "sip_trunk_id": get_val("sip_trunk_id", "SIP_TRUNK_ID", ""),
        "livekit_api_key": get_val("livekit_api_key", "LIVEKIT_API_KEY", ""),
        "livekit_api_secret": get_val("livekit_api_secret", "LIVEKIT_API_SECRET", ""),
        "openai_api_key": get_val("openai_api_key", "OPENAI_API_KEY", ""),
        "sarvam_api_key": get_val("sarvam_api_key", "SARVAM_API_KEY", ""),
        "cal_api_key": get_val("cal_api_key", "CAL_API_KEY", ""),
        "cal_event_type_id": get_val("cal_event_type_id", "CAL_EVENT_TYPE_ID", ""),
        "telegram_bot_token": get_val("telegram_bot_token", "TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": get_val("telegram_chat_id", "TELEGRAM_CHAT_ID", ""),
        "supabase_url": supabase_url,
        "supabase_key": get_val("supabase_key", "SUPABASE_KEY", ""),
        "azure_openai_api_key": get_val("azure_openai_api_key", "AZURE_OPENAI_API_KEY", ""),
        "azure_openai_endpoint": get_val("azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT", ""),
        "azure_openai_deployment": get_val("azure_openai_deployment", "AZURE_OPENAI_DEPLOYMENT", ""),
    }

    # Merge: raw config as base (for extra keys like lang_preset, vobiz_*, etc.),
    # then overlay resolved values so .env fallbacks always win over empty strings.
    merged = {**config, **resolved}
    return merged

def write_config(data):
    # Read raw file config, not the processed one, to avoid overwriting with sanitized values
    raw_config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            raw_config = json.load(f)
    raw_config.update(data)
    with open(CONFIG_FILE, "w") as f:
        json.dump(raw_config, f, indent=4)

# ── API Endpoints ──────────────────────────────────────────────────────────────

@app.get("/api/config")
async def api_get_config():
    return read_config()

@app.post("/api/config")
async def api_post_config(request: Request):
    data = await request.json()
    write_config(data)
    logger.info("Configuration updated via UI.")
    return {"status": "success"}

@app.get("/api/logs")
async def api_get_logs():
    config = read_config()
    os.environ["SUPABASE_URL"] = str(config.get("supabase_url", ""))
    os.environ["SUPABASE_KEY"] = str(config.get("supabase_key", ""))
    import db
    try:
        logs = db.fetch_call_logs(limit=50)
        return logs
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        return []

@app.get("/api/logs/{log_id}/transcript")
async def api_get_transcript(log_id: str):
    config = read_config()
    os.environ["SUPABASE_URL"] = str(config.get("supabase_url", ""))
    os.environ["SUPABASE_KEY"] = str(config.get("supabase_key", ""))
    import db
    try:
        from supabase import create_client
        supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        res = supabase.table("call_logs").select("*").eq("id", log_id).single().execute()
        data = res.data
        text = f"Call Log — {data.get('created_at', '')}\n"
        text += f"Phone: {data.get('phone_number', 'Unknown')}\n"
        text += f"Duration: {data.get('duration_seconds', 0)}s\n"
        text += f"Summary: {data.get('summary', '')}\n\n"
        text += "--- TRANSCRIPT ---\n"
        text += data.get("transcript", "No transcript available.")
        return PlainTextResponse(content=text, media_type="text/plain",
                                 headers={"Content-Disposition": f"attachment; filename=transcript_{log_id}.txt"})
    except Exception as e:
        return PlainTextResponse(content=f"Error: {e}", status_code=500)

@app.get("/api/bookings")
async def api_get_bookings():
    config = read_config()
    os.environ["SUPABASE_URL"] = str(config.get("supabase_url", ""))
    os.environ["SUPABASE_KEY"] = str(config.get("supabase_key", ""))
    import db
    try:
        return db.fetch_bookings()
    except Exception as e:
        logger.error(f"Error fetching bookings: {e}")
        return []

@app.get("/api/enquiries")
async def api_get_enquiries():
    config = read_config()
    os.environ["SUPABASE_URL"] = str(config.get("supabase_url", ""))
    os.environ["SUPABASE_KEY"] = str(config.get("supabase_key", ""))
    import db
    try:
        data = db.fetch_enquiries(limit=500)
        return {"data": data, "setup_required": False}
    except Exception as e:
        err = str(e)
        if "PGRST205" in err or "schema cache" in err.lower():
            return {"data": [], "setup_required": True}
        logger.error(f"Error fetching enquiries: {e}")
        return {"data": [], "setup_required": False}

@app.get("/api/stats")
async def api_get_stats():
    config = read_config()
    os.environ["SUPABASE_URL"] = str(config.get("supabase_url", ""))
    os.environ["SUPABASE_KEY"] = str(config.get("supabase_key", ""))
    import db
    try:
        return db.fetch_stats()
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return {"total_calls": 0, "total_bookings": 0, "avg_duration": 0, "booking_rate": 0}

@app.get("/api/contacts")
async def api_get_contacts():
    """CRM endpoint — groups call_logs by phone number, deduplicates into contacts."""
    config = read_config()
    os.environ["SUPABASE_URL"] = str(config.get("supabase_url", ""))
    os.environ["SUPABASE_KEY"] = str(config.get("supabase_key", ""))
    try:
        from supabase import create_client
        supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        res = supabase.table("call_logs") \
            .select("phone_number, caller_name, summary, created_at") \
            .order("created_at", desc=True) \
            .limit(500) \
            .execute()
        rows = cast(list[dict], res.data or [])

        # Deduplicate by phone number
        contacts: dict = {}
        for r in rows:
            phone = r.get("phone_number") or "unknown"
            if phone not in contacts:
                contacts[phone] = {
                    "phone_number": phone,
                    "caller_name": r.get("caller_name") or "",
                    "total_calls": 0,
                    "last_seen": r.get("created_at"),
                    "is_booked": False,
                }
            c = contacts[phone]
            c["total_calls"] += 1
            # Use the most recent non-empty name
            if not c["caller_name"] and r.get("caller_name"):
                c["caller_name"] = r["caller_name"]
            # Mark booked if any call had a confirmed booking
            if r.get("summary") and "Confirmed" in r.get("summary", ""):
                c["is_booked"] = True

        return sorted(contacts.values(), key=lambda x: x["last_seen"] or "", reverse=True)
    except Exception as e:
        logger.error(f"Error fetching contacts: {e}")
        return []


DEMO_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Voice Demo — NOVA AI</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Inter',sans-serif;background:#0f1117;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;flex-direction:column;gap:24px;padding:24px}
    .card{background:#1c2333;border:1px solid #2a3448;border-radius:20px;padding:40px;max-width:440px;width:100%;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,0.4)}
    h1{font-size:22px;font-weight:700;margin-bottom:6px}
    .sub{color:#8892a4;font-size:13px;margin-bottom:28px}
    .avatar{width:80px;height:80px;border-radius:50%;background:linear-gradient(135deg,#6c63ff,#a855f7);display:flex;align-items:center;justify-content:center;font-size:36px;margin:0 auto 24px}
    .btn{width:100%;padding:14px;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;border:none;transition:all 0.2s}
    .btn-start{background:#6c63ff;color:#fff}
    .btn-start:hover{background:#5a52e0;box-shadow:0 0 24px rgba(108,99,255,0.4)}
    .btn-end{background:#ef4444;color:#fff;display:none}
    .btn-end:hover{background:#dc2626}
    #status{font-size:13px;color:#8892a4;margin-top:16px;min-height:20px}
    .pulse{display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:6px;animation:pulse 1.5s infinite}
    @keyframes pulse{0%,100%{box-shadow:0 0 4px #22c55e}50%{box-shadow:0 0 12px #22c55e}}
    .vol-bar{display:flex;gap:3px;align-items:flex-end;justify-content:center;height:32px;margin-top:12px;display:none}
    .vol-bar span{width:4px;background:#6c63ff;border-radius:2px;transition:height 0.1s}
  </style>
</head>
<body>
  <div class="card">
    <div class="avatar">🎙</div>
    <h1>Talk to Aryan</h1>
    <div class="sub">AI-powered multilingual consultant · NOVA AI</div>
    <button class="btn btn-start" id="startBtn" onclick="startCall()">📞 Start Demo Call</button>
    <button class="btn btn-end" id="endBtn" onclick="endCall()">📵 End Call</button>
    <div id="status">Click to start a live voice demo</div>
    <div class="vol-bar" id="volBar">
      <span id="b1" style="height:8px"></span><span id="b2" style="height:14px"></span>
      <span id="b3" style="height:20px"></span><span id="b4" style="height:14px"></span>
      <span id="b5" style="height:8px"></span>
    </div>
  </div>
  <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
  <script>
    let room;
    async function startCall() {
      document.getElementById('status').textContent = 'Connecting...';
      document.getElementById('startBtn').disabled = true;
      try {
        const res = await fetch('/api/demo-token').then(r => r.json());
        if (res.error) throw new Error(res.error);
        room = new LivekitClient.Room();
        await room.connect(res.url, res.token, {autoSubscribe: true});
        await room.localParticipant.setMicrophoneEnabled(true);
        document.getElementById('startBtn').style.display = 'none';
        document.getElementById('endBtn').style.display = 'block';
        document.getElementById('volBar').style.display = 'flex';
        setStatus('<span class="pulse"></span>Connected — speak now!');
        animateBars();
      } catch(e) {
        setStatus('❌ ' + e.message);
        document.getElementById('startBtn').disabled = false;
      }
    }
    async function endCall() {
      if (room) { await room.disconnect(); room = null; }
      document.getElementById('startBtn').style.display = 'block';
      document.getElementById('startBtn').disabled = false;
      document.getElementById('endBtn').style.display = 'none';
      document.getElementById('volBar').style.display = 'none';
      setStatus('Call ended. Click to start again.');
    }
    function setStatus(html) { document.getElementById('status').innerHTML = html; }
    function animateBars() {
      if (!room) return;
      ['b1','b2','b3','b4','b5'].forEach(id => {
        document.getElementById(id).style.height = (4 + Math.random()*24) + 'px';
      });
      setTimeout(animateBars, 150);
    }
  </script>
</body>
</html>"""


# ── Outbound Calls ────────────────────────────────────────────────────────────

@app.post("/api/call/single")
async def api_call_single(request: Request):
    """Dispatch a single outbound call via LiveKit."""
    data = await request.json()
    phone = (data.get("phone") or "").strip()
    if not phone.startswith("+"):
        return {"status": "error", "message": "Phone number must start with + and country code"}
    config = read_config()
    try:
        import random, json as _json
        from livekit import api as lkapi
        lk = lkapi.LiveKitAPI(
            url=str(config.get("livekit_url") or os.environ.get("LIVEKIT_URL","")),
            api_key=str(config.get("livekit_api_key") or os.environ.get("LIVEKIT_API_KEY","")),
            api_secret=str(config.get("livekit_api_secret") or os.environ.get("LIVEKIT_API_SECRET","")),
        )
        room_name = f"call-{phone.replace('+','')}-{random.randint(1000,9999)}"
        dispatch = await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=_json.dumps({"phone_number": phone}),
            )
        )
        await lk.aclose()
        logger.info(f"Outbound call dispatched to {phone}: {dispatch.id}")
        return {"status": "ok", "dispatch_id": dispatch.id, "room": room_name, "phone": phone}
    except Exception as e:
        logger.error(f"Call dispatch error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/call/bulk")
async def api_call_bulk(request: Request):
    """Dispatch outbound calls to multiple numbers (one per line)."""
    import random, json as _json
    from livekit import api as lkapi
    data = await request.json()
    numbers = [n.strip() for n in (data.get("numbers") or "").splitlines() if n.strip()]
    results = []
    cfg = read_config()
    lk_url    = str(cfg.get("livekit_url")    or os.environ.get("LIVEKIT_URL",""))
    lk_key    = str(cfg.get("livekit_api_key")    or os.environ.get("LIVEKIT_API_KEY",""))
    lk_secret = str(cfg.get("livekit_api_secret") or os.environ.get("LIVEKIT_API_SECRET",""))
    for phone in numbers:
        if not phone.startswith("+"):
            results.append({"phone": phone, "status": "error", "message": "Must start with +"})
            continue
        try:
            lk = lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret)
            room_name = f"call-{phone.replace('+','')}-{random.randint(1000,9999)}"
            dispatch = await lk.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(
                    agent_name="outbound-caller",
                    room=room_name,
                    metadata=_json.dumps({"phone_number": phone}),
                )
            )
            await lk.aclose()
            results.append({"phone": phone, "status": "ok", "dispatch_id": dispatch.id})
            logger.info(f"Bulk outbound dispatched to {phone}: {dispatch.id}")
        except Exception as e:
            results.append({"phone": phone, "status": "error", "message": str(e)})
    return {"results": results, "total": len(results)}

# ── Demo Link ─────────────────────────────────────────────────────────────────

@app.get("/api/demo-token")
async def api_demo_token():
    """Generate a LiveKit room + access token for browser-based demo call."""
    config = read_config()
    try:
        from livekit.api import AccessToken, VideoGrants
        from datetime import timedelta
        import time, random
        room_name = f"demo-{random.randint(10000,99999)}"
        api_key    = str(config.get("livekit_api_key") or os.environ.get("LIVEKIT_API_KEY",""))
        api_secret = str(config.get("livekit_api_secret") or os.environ.get("LIVEKIT_API_SECRET",""))
        livekit_url = str(config.get("livekit_url") or os.environ.get("LIVEKIT_URL",""))

        token = AccessToken(api_key, api_secret) \
            .with_identity("demo-user") \
            .with_name("Demo Caller") \
            .with_grants(VideoGrants(room_join=True, room=room_name)) \
            .with_ttl(timedelta(seconds=3600)) \
            .to_jwt()

        # Also dispatch the agent into the room
        import json as _json
        from livekit import api as lkapi
        lk = lkapi.LiveKitAPI(url=livekit_url, api_key=api_key, api_secret=api_secret)
        await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=_json.dumps({"phone_number": "demo", "is_demo": True}),
            )
        )
        await lk.aclose()
        return {"token": token, "room": room_name, "url": livekit_url}
    except Exception as e:
        logger.error(f"Demo token error: {e}")
        return {"error": str(e)}

@app.get("/demo", response_class=HTMLResponse)
async def get_demo_page():
    """Browser-based demo call page using LiveKit JS SDK."""
    return HTMLResponse(content=DEMO_PAGE_HTML)


# ── Prometheus Metrics (#40) ──────────────────────────────────────────────────
try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST, REGISTRY
    from fastapi.responses import Response as _Resp

    def _safe_metric(metric_cls, name, description, **kwargs):
        """Create a metric, unregistering any prior collector with the same name first.

        prometheus_client registers multiple derived names per metric (e.g.
        Counter 'voice_calls_total' → 'voice_calls', 'voice_calls_total',
        'voice_calls_created'). On uvicorn dev-mode reload the child process
        inherits the parent's registry, so we must remove the old collector
        before creating a fresh one.
        """
        if name in REGISTRY._names_to_collectors:
            existing = REGISTRY._names_to_collectors[name]
            try:
                REGISTRY.unregister(existing)
            except Exception:
                pass
        return metric_cls(name, description, **kwargs)

    _voice_calls_total   = _safe_metric(Counter, "voice_calls_total",   "Total calls handled by the agent")
    _voice_calls_booked  = _safe_metric(Counter, "voice_calls_booked_total", "Calls that resulted in a booking")
    _voice_call_duration = _safe_metric(Histogram, "voice_call_duration_seconds", "Call duration in seconds",
                                      buckets=[10, 30, 60, 120, 300, 600, 1200])
    _voice_calls_active  = _safe_metric(Gauge, "voice_calls_active", "Currently active calls")

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        """Prometheus metrics scrape endpoint."""
        return _Resp(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/internal/record-call", include_in_schema=False)
    async def record_call_metric(request: Request):
        """Called by agent.py at shutdown to update Prometheus counters."""
        data = await request.json()
        _voice_calls_total.inc()
        if data.get("booked"):
            _voice_calls_booked.inc()
        if data.get("duration"):
            _voice_call_duration.observe(data["duration"])
        return {"ok": True}

    logger.info("[METRICS] Prometheus metrics enabled at /metrics")

except ImportError:
    logger.warning("[METRICS] prometheus_client not installed — /metrics disabled")

# ── Main Dashboard HTML ────────────────────────────────────────────────────────


@app.get("/health")
def health_check():
    """Health check endpoint for Coolify monitoring (#22)."""
    return {
        "status": "ok",
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        "service": "NOVA BY SHIVANSH-ai-voice-agent",
    }

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    config = read_config()

    def sel(key, val):
        return "selected" if config.get(key) == val else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Voice Agent — Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #0f1117;
      --sidebar: #161b27;
      --card: #1c2333;
      --border: #2a3448;
      --accent: #6c63ff;
      --accent-glow: rgba(108,99,255,0.18);
      --text: #e2e8f0;
      --muted: #8892a4;
      --green: #22c55e;
      --red: #ef4444;
      --yellow: #f59e0b;
      --sidebar-w: 240px;
    }}
    body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }}

    /* ── Sidebar ── */
    #sidebar {{
      width: var(--sidebar-w); min-width: var(--sidebar-w);
      background: var(--sidebar); border-right: 1px solid var(--border);
      display: flex; flex-direction: column; padding: 24px 0;
      position: relative; z-index: 10;
    }}
    .sidebar-brand {{
      padding: 0 20px 24px;
      border-bottom: 1px solid var(--border);
      display: flex; align-items: center; gap: 10px;
    }}
    .sidebar-brand .logo {{
      width: 32px; height: 32px; background: var(--accent);
      border-radius: 8px; display: flex; align-items: center; justify-content: center;
      font-size: 16px;
    }}
    .sidebar-brand .brand-text {{ font-weight: 700; font-size: 14px; line-height: 1.2; }}
    .sidebar-brand .brand-sub {{ font-size: 10px; color: var(--muted); }}
    .sidebar-nav {{ padding: 16px 0; flex: 1; }}
    .nav-section {{ padding: 8px 16px 4px; font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }}
    .nav-item {{
      display: flex; align-items: center; gap: 10px;
      padding: 10px 20px; cursor: pointer; font-size: 13.5px; font-weight: 500;
      color: var(--muted); border-left: 3px solid transparent;
      transition: all 0.15s; user-select: none;
    }}
    .nav-item:hover {{ color: var(--text); background: rgba(255,255,255,0.04); }}
    .nav-item.active {{ color: var(--accent); border-left-color: var(--accent); background: var(--accent-glow); }}
    .nav-item .icon {{ font-size: 16px; width: 20px; text-align: center; }}
    .sidebar-footer {{
      padding: 16px 20px;
      border-top: 1px solid var(--border);
      font-size: 11px; color: var(--muted);
    }}
    .status-dot {{
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      background: var(--green); margin-right: 6px; box-shadow: 0 0 6px var(--green);
    }}

    /* ── Main ── */
    #main {{ flex: 1; overflow-y: auto; background: var(--bg); }}
    .page {{ display: none; padding: 32px 36px; min-height: 100%; }}
    .page.active {{ display: block; }}
    .page-header {{ margin-bottom: 28px; }}
    .page-title {{ font-size: 22px; font-weight: 700; }}
    .page-sub {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}

    /* ── Cards ── */
    .card {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 20px;
    }}
    .stat-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 28px; }}
    .stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
    .stat-label {{ font-size: 11px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }}
    .stat-value {{ font-size: 28px; font-weight: 700; margin-top: 8px; }}
    .stat-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

    /* ── Table ── */
    .table-wrap {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    thead th {{ padding: 12px 16px; text-align: left; font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; background: rgba(255,255,255,0.03); border-bottom: 1px solid var(--border); }}
    tbody td {{ padding: 13px 16px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }}
    tbody tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover {{ background: rgba(255,255,255,0.025); }}
    .badge {{ display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
    .badge-green {{ background: rgba(34,197,94,0.12); color: var(--green); }}
    .badge-gray {{ background: rgba(255,255,255,0.07); color: var(--muted); }}
    .badge-yellow {{ background: rgba(245,158,11,0.12); color: var(--yellow); }}

    /* ── Forms ── */
    label {{ display: block; font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; }}
    input[type=text], input[type=password], input[type=number], select, textarea {{
      width: 100%; background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 10px 12px; color: var(--text); font-family: inherit;
      font-size: 13.5px; outline: none; transition: border-color 0.15s;
    }}
    input:focus, select:focus, textarea:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }}
    textarea {{ font-family: 'JetBrains Mono', 'Fira Code', monospace; resize: vertical; }}
    .form-group {{ margin-bottom: 20px; }}
    .form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .hint {{ font-size: 11.5px; color: var(--muted); margin-top: 5px; }}
    .section-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 20px; }}
    .section-title {{ font-size: 14px; font-weight: 600; margin-bottom: 18px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }}

    /* ── Buttons ── */
    .btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 9px 18px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; transition: all 0.15s; }}
    .btn-primary {{ background: var(--accent); color: #fff; }}
    .btn-primary:hover {{ background: #5a52e0; box-shadow: 0 0 16px var(--accent-glow); }}
    .btn-ghost {{ background: transparent; border: 1px solid var(--border); color: var(--muted); }}
    .btn-ghost:hover {{ border-color: var(--accent); color: var(--accent); }}
    .btn-sm {{ padding: 5px 12px; font-size: 12px; }}
    .save-bar {{
      position: sticky; bottom: 0; left: 0; right: 0;
      background: rgba(22,27,39,0.95); backdrop-filter: blur(12px);
      border-top: 1px solid var(--border);
      padding: 14px 36px; display: flex; align-items: center; justify-content: space-between; z-index: 20;
    }}
    .save-status {{ font-size: 13px; font-weight: 500; color: var(--green); opacity: 0; transition: opacity 0.3s; }}

    /* ── Calendar ── */
    .cal-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }}
    .cal-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }}
    .cal-day-name {{ text-align: center; font-size: 11px; color: var(--muted); font-weight: 600; padding: 8px 0; text-transform: uppercase; letter-spacing: 0.06em; }}
    .cal-cell {{
      min-height: 80px; background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 10px; cursor: pointer; transition: all 0.18s; position: relative;
    }}
    .cal-cell:hover {{ border-color: var(--accent); background: var(--accent-glow); transform: scale(1.03); box-shadow: 0 4px 20px rgba(108,99,255,0.15); }}
    .cal-cell.today {{ border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-glow); }}
    .cal-cell.other-month {{ opacity: 0.3; }}
    .cal-num {{ font-size: 13px; font-weight: 700; }}
    .cal-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--accent); margin-top: 6px; box-shadow: 0 0 6px var(--accent); }}
    .cal-booking-count {{ font-size: 10px; color: var(--accent); font-weight: 600; margin-top: 3px; }}
    .day-panel {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-top: 20px; display: none; }}
    .day-panel.show {{ display: block; animation: fadeIn 0.2s ease; }}
    .booking-item {{ padding: 14px; background: var(--bg); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 10px; transition: border-color 0.15s; }}
    .booking-item:hover {{ border-color: var(--accent); }}
    .booking-item:last-child {{ margin-bottom: 0; }}

    /* ── Modal ── */
    .modal-overlay {{
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.7); backdrop-filter: blur(6px);
      z-index: 1000; align-items: center; justify-content: center;
    }}
    .modal-overlay.open {{ display: flex; animation: fadeIn 0.2s ease; }}
    .modal-box {{
      background: var(--card); border: 1px solid var(--border);
      border-radius: 16px; padding: 28px; min-width: 480px; max-width: 600px; width: 90%;
      box-shadow: 0 24px 60px rgba(0,0,0,0.5);
      animation: slideUp 0.25s ease;
    }}
    .modal-title {{ font-size: 18px; font-weight: 700; margin-bottom: 6px; }}
    .modal-sub {{ font-size: 12px; color: var(--muted); margin-bottom: 20px; }}
    .modal-close {{
      position: absolute; top: 20px; right: 24px;
      background: none; border: none; color: var(--muted);
      font-size: 20px; cursor: pointer; line-height: 1;
    }}
    .modal-close:hover {{ color: var(--text); }}
    @keyframes fadeIn {{ from {{ opacity:0 }} to {{ opacity:1 }} }}
    @keyframes slideUp {{ from {{ transform:translateY(20px); opacity:0 }} to {{ transform:translateY(0); opacity:1 }} }}

    /* ── Premium extras ── */
    .stat-card {{ transition: transform 0.15s, box-shadow 0.15s; }}
    .stat-card:hover {{ transform: translateY(-3px); box-shadow: 0 8px 30px rgba(108,99,255,0.12); }}
    .stat-accent {{ color: var(--accent); }}
    .pulse {{ animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100% {{ box-shadow: 0 0 6px var(--green); }} 50% {{ box-shadow: 0 0 14px var(--green); }} }}
  </style>
</head>
<body>

<!-- ── Day Detail Modal ── -->
<div class="modal-overlay" id="day-modal" onclick="if(event.target===this)closeDayModal()">
  <div class="modal-box" style="position:relative;">
    <button class="modal-close" onclick="closeDayModal()">✕</button>
    <div class="modal-title" id="modal-date-title">Bookings</div>
    <div class="modal-sub" id="modal-date-sub"></div>
    <div id="modal-bookings-body"></div>
  </div>
</div>

<!-- ── Sidebar ── -->
<nav id="sidebar">
  <div class="sidebar-brand">
    <div class="logo">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="12" cy="12" r="10" fill="rgba(255,255,255,0.12)"/>
        <path d="M8 12c0-2.21 1.79-4 4-4s4 1.79 4 4" stroke="white" stroke-width="1.8" stroke-linecap="round"/>
        <circle cx="12" cy="15" r="2" fill="white"/>
        <path d="M6 18c1.5-1.5 3.5-2.5 6-2.5s4.5 1 6 2.5" stroke="white" stroke-width="1.4" stroke-linecap="round" opacity="0.6"/>
      </svg>
    </div>
    <div>
      <div class="brand-text">Voice Agent</div>
      <div class="brand-sub">NOVA AI</div>
    </div>
  </div>
  <div class="sidebar-nav">
    <div class="nav-section">Overview</div>
    <div class="nav-item active" onclick="goTo('dashboard', this)"><span class="icon">📊</span> Dashboard</div>
    <div class="nav-item" onclick="goTo('calendar', this); loadCalendar();"><span class="icon">📅</span> Calendar</div>
    <div class="nav-section" style="margin-top:12px;">Configuration</div>
    <div class="nav-item" onclick="goTo('agent', this)"><span class="icon">🤖</span> Agent Settings</div>
    <div class="nav-item" onclick="goTo('models', this)"><span class="icon">🎙️</span> Models & Voice</div>
    <div class="nav-item" onclick="goTo('credentials', this)"><span class="icon">🔑</span> API Credentials</div>
    <div class="nav-section" style="margin-top:12px;">Data</div>
    <div class="nav-item" onclick="goTo('logs', this); loadLogs();"><span class="icon">📞</span> Call Logs</div>
    <div class="nav-item" onclick="goTo('enquiries', this); loadEnquiries();"><span class="icon">📋</span> Enquiries</div>
    <div class="nav-item" onclick="goTo('crm', this); loadCRM();"><span class="icon">👥</span> CRM Contacts</div>
    <div class="nav-section" style="margin-top:12px;">Calling</div>
    <div class="nav-item" onclick="goTo('outbound', this)"><span class="icon">📲</span> Outbound Calls</div>
    <div class="nav-item" onclick="goTo('languages', this); initLanguagePage();"><span class="icon">🌐</span> Language Presets</div>
    <div class="nav-item" onclick="goTo('demo', this); initDemo();"><span class="icon">✨</span> Demo Link</div>
  </div>
  <div class="sidebar-footer">
    <span class="status-dot pulse"></span>Agent Online
  </div>
</nav>

<!-- ── Main Content ── -->
<div id="main">

  <!-- ── Dashboard ── -->
  <div id="page-dashboard" class="page active">
    <div class="page-header">
      <div class="page-title">Dashboard</div>
      <div class="page-sub">Real-time overview of your AI voice agent performance</div>
    </div>
    <div class="stat-grid" id="stat-grid">
      <div class="stat-card"><div class="stat-label">Total Calls</div><div class="stat-value" id="stat-calls">—</div><div class="stat-sub">All time</div></div>
      <div class="stat-card"><div class="stat-label">Bookings Made</div><div class="stat-value" id="stat-bookings">—</div><div class="stat-sub">Confirmed appointments</div></div>
      <div class="stat-card"><div class="stat-label">Avg Duration</div><div class="stat-value" id="stat-duration">—</div><div class="stat-sub">Seconds per call</div></div>
      <div class="stat-card"><div class="stat-label">Booking Rate</div><div class="stat-value" id="stat-rate">—</div><div class="stat-sub">Calls that converted</div></div>
    </div>
    <div class="section-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
        <div class="section-title" style="border:none;padding:0;margin:0;">Recent Calls</div>
        <button class="btn btn-ghost btn-sm" onclick="loadDashboard()">↻ Refresh</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Date</th><th>Phone</th><th>Duration</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="dash-table-body"><tr><td colspan="5" style="text-align:center;padding:24px;color:var(--muted);">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── Calendar ── -->
  <div id="page-calendar" class="page">
    <div class="page-header">
      <div class="page-title">Booking Calendar</div>
      <div class="page-sub">View confirmed appointments by date</div>
    </div>
    <div class="section-card">
      <div class="cal-header">
        <button class="btn btn-ghost btn-sm" onclick="changeMonth(-1)">← Prev</button>
        <div style="font-size:16px;font-weight:700;" id="cal-month-label">Month Year</div>
        <button class="btn btn-ghost btn-sm" onclick="changeMonth(1)">Next →</button>
      </div>
      <div class="cal-grid" id="cal-grid"></div>
      <div class="day-panel" id="day-panel">
        <div style="font-size:14px;font-weight:700;margin-bottom:12px;" id="day-panel-title">Selected Day</div>
        <div id="day-panel-body"></div>
      </div>
    </div>
  </div>

  <!-- ── Agent Settings ── -->
  <div id="page-agent" class="page">
    <div class="page-header">
      <div class="page-title">Agent Settings</div>
      <div class="page-sub">Configure AI personality, opening line, and sensitivity</div>
    </div>
    <div class="section-card">
      <div class="section-title">Opening Greeting</div>
      <div class="form-group">
        <label>First Line (What the agent says when a call connects)</label>
        <input type="text" id="first_line" value="{config.get('first_line', '')}" placeholder="Namaste! This is Aryan from NOVA AI...">
        <div class="hint">This is the very first thing the agent says. Keep it concise and warm.</div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">System Prompt</div>
      <div class="form-group">
        <label>Master System Prompt</label>
        <textarea id="agent_instructions" rows="16" placeholder="Enter the AI's full personality and instructions...">{config.get('agent_instructions', '')}</textarea>
        <div class="hint">Date and time context are injected automatically. Do not hardcode today's date.</div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">Listening Sensitivity</div>
      <div class="form-group" style="max-width:220px;">
        <label>Endpointing Delay (seconds)</label>
        <input type="number" id="stt_min_endpointing_delay" step="0.05" min="0.1" max="3.0" value="{config.get('stt_min_endpointing_delay', 0.6)}">
        <div class="hint">Seconds the AI waits after silence before responding. Default: 0.6</div>
      </div>
    </div>
    <div class="save-bar">
      <span class="save-status" id="save-status-agent">✅ Saved!</span>
      <button class="btn btn-primary" onclick="saveConfig('agent')">💾 Save Agent Settings</button>
    </div>
  </div>

  <!-- ── Models & Voice ── -->
  <div id="page-models" class="page">
    <div class="page-header">
      <div class="page-title">Models & Voice</div>
      <div class="page-sub">Select the LLM brain and TTS voice persona</div>
    </div>
    <div class="section-card">
      <div class="section-title">Language Model (LLM)</div>
      <div class="form-row" style="max-width:720px;">
        <div class="form-group">
          <label>LLM Provider</label>
          <select id="llm_provider">
            <option value="azure" {sel('llm_provider','azure')}>Azure OpenAI</option>
            <option value="openai" {sel('llm_provider','openai')}>OpenAI (Direct)</option>
            <option value="groq" {sel('llm_provider','groq')}>Groq</option>
            <option value="claude" {sel('llm_provider','claude')}>Claude (Anthropic)</option>
          </select>
          <div class="hint">Select Azure OpenAI to use your Azure credits.</div>
        </div>
        <div class="form-group">
          <label>Model / Deployment Name</label>
          <select id="llm_model">
            <option value="gpt-4.1-mini" {sel('llm_model','gpt-4.1-mini')}>gpt-4.1-mini — Fast &amp; Latest (Azure Default)</option>
            <option value="gpt-4o-mini" {sel('llm_model','gpt-4o-mini')}>gpt-4o-mini — Fast &amp; Cheap</option>
            <option value="gpt-4o" {sel('llm_model','gpt-4o')}>gpt-4o — Balanced</option>
            <option value="gpt-4.1" {sel('llm_model','gpt-4.1')}>gpt-4.1 — Latest (Recommended)</option>
            <option value="gpt-4.5-preview" {sel('llm_model','gpt-4.5-preview')}>gpt-4.5-preview — Most Capable</option>
            <option value="o4-mini" {sel('llm_model','o4-mini')}>o4-mini — Reasoning, Fast</option>
            <option value="o3" {sel('llm_model','o3')}>o3 — Reasoning, Best</option>
            <option value="gpt-4-turbo" {sel('llm_model','gpt-4-turbo')}>gpt-4-turbo — Legacy</option>
            <option value="gpt-3.5-turbo" {sel('llm_model','gpt-3.5-turbo')}>gpt-3.5-turbo — Cheapest</option>
          </select>
          <div class="hint">For Azure, this must match your deployment name exactly.</div>
        </div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">Voice Synthesis</div>

      <!-- TTS Provider Switcher -->
      <div class="form-row" style="max-width:720px;margin-bottom:18px;">
        <div class="form-group">
          <label>TTS Provider</label>
          <select id="tts_provider" onchange="onTTSProviderChange(this.value)">
            <option value="sarvam" {sel('tts_provider','sarvam')}>🇮🇳 Sarvam bulbul:v3 — Best for Indian languages</option>
            <option value="elevenlabs" {sel('tts_provider','elevenlabs')}>⚡ ElevenLabs Flash — Ultra-realistic English voice</option>
          </select>
          <div class="hint">Sarvam = native Indian accent. ElevenLabs = most human-sounding English.</div>
        </div>
      </div>

      <!-- Sarvam Options (shown when Sarvam selected) -->
      <div id="sarvam-options" class="form-row" style="max-width:720px;">
        <div class="form-group">
          <label>Speaker Voice</label>
          <select id="tts_voice">
            <option value="kavya"  {sel('tts_voice','kavya')} >Kavya  — Female, Friendly</option>
            <option value="ritu"   {sel('tts_voice','ritu')}  >Ritu   — Female, Soft</option>
            <option value="priya"  {sel('tts_voice','priya')} >Priya  — Female, Warm</option>
            <option value="shreya" {sel('tts_voice','shreya')}>Shreya — Female, Clear</option>
            <option value="neha"   {sel('tts_voice','neha')}  >Neha   — Female, Energetic</option>
            <option value="rohan"  {sel('tts_voice','rohan')} >Rohan  — Male, Balanced</option>
            <option value="shubh"  {sel('tts_voice','shubh')} >Shubh  — Male, Formal</option>
            <option value="rahul"  {sel('tts_voice','rahul')} >Rahul  — Male, Deep</option>
            <option value="amit"   {sel('tts_voice','amit')}  >Amit   — Male, Casual</option>
            <option value="dev"    {sel('tts_voice','dev')}   >Dev    — Male, Professional</option>
          </select>
        </div>
        <div class="form-group">
          <label>Language</label>
          <select id="tts_language">
            <option value="hi-IN" {sel('tts_language','hi-IN')}>Hindi (hi-IN)</option>
            <option value="en-IN" {sel('tts_language','en-IN')}>English India (en-IN)</option>
            <option value="ta-IN" {sel('tts_language','ta-IN')}>Tamil (ta-IN)</option>
            <option value="te-IN" {sel('tts_language','te-IN')}>Telugu (te-IN)</option>
            <option value="kn-IN" {sel('tts_language','kn-IN')}>Kannada (kn-IN)</option>
            <option value="ml-IN" {sel('tts_language','ml-IN')}>Malayalam (ml-IN)</option>
            <option value="mr-IN" {sel('tts_language','mr-IN')}>Marathi (mr-IN)</option>
            <option value="gu-IN" {sel('tts_language','gu-IN')}>Gujarati (gu-IN)</option>
            <option value="bn-IN" {sel('tts_language','bn-IN')}>Bengali (bn-IN)</option>
          </select>
        </div>
      </div>

      <!-- ElevenLabs Options (shown when ElevenLabs selected) -->
      <div id="elevenlabs-options" class="form-row" style="max-width:720px;display:none;">
        <div class="form-group" style="flex:1;">
          <label>ElevenLabs Voice</label>
          <select id="elevenlabs_voice_id">
            <option value="FGY2WhTYpPnrIDTdsKH5" {sel('elevenlabs_voice_id','FGY2WhTYpPnrIDTdsKH5')}>⭐ Laura — Warm Female, Best for Hindi ✅ Free</option>
            <option value="EXAVITQu4vr4xnSDxMaL" {sel('elevenlabs_voice_id','EXAVITQu4vr4xnSDxMaL')}>Sarah — Soft, Calm Female ✅ Free</option>
            <option value="IKne3meq5aSn9XLyUdCD" {sel('elevenlabs_voice_id','IKne3meq5aSn9XLyUdCD')}>Charlie — British Male ✅ Free</option>
            <option value="JBFqnCBsd6RMkjVDRZzb" {sel('elevenlabs_voice_id','JBFqnCBsd6RMkjVDRZzb')}>George — Deep, Warm Male ✅ Free</option>
            <option value="N2lVS1w4EtoT3dr4eOWO" {sel('elevenlabs_voice_id','N2lVS1w4EtoT3dr4eOWO')}>Callum — Deep Male ✅ Free</option>
            <option value="pNInz6obpgDQGcFmaJgB" {sel('elevenlabs_voice_id','pNInz6obpgDQGcFmaJgB')}>Adam — Narration Male ✅ Free</option>
          </select>
          <div class="hint">⚠️ Only pre-made voices work on the free plan. Library/cloned voices require a paid subscription.</div>
        </div>
        <div class="form-group" style="flex:1;">
          <label>Custom Voice ID (optional)</label>
          <input type="text" id="elevenlabs_voice_id_custom"
            placeholder="Paste your ElevenLabs voice ID here"
            style="font-family:monospace;font-size:12px;"
            oninput="if(this.value.trim())document.getElementById('elevenlabs_voice_id').value=this.value.trim()">
          <div class="hint">Override with any voice ID from your ElevenLabs library.</div>
        </div>
      </div>

      <!-- ElevenLabs info banner -->
      <div id="el-info-banner" style="display:none;margin-top:14px;max-width:720px;padding:12px 16px;background:rgba(139,92,246,0.1);border:1px solid rgba(139,92,246,0.3);border-radius:10px;font-size:13px;color:#a78bfa;">
        ⚡ <strong>ElevenLabs active</strong> — Free tier: 10,000 chars/month (~8 min speech).
        Make sure <code style="background:rgba(255,255,255,0.1);padding:1px 5px;border-radius:4px;">ELEVENLABS_API_KEY</code> is set in your <code style="background:rgba(255,255,255,0.1);padding:1px 5px;border-radius:4px;">.env</code> file.
        <a href="https://elevenlabs.io" target="_blank" style="color:#c4b5fd;text-decoration:underline;">Get free key →</a>
      </div>
    </div>
    <div class="save-bar">
      <span class="save-status" id="save-status-models">✅ Saved!</span>
      <button class="btn btn-primary" onclick="saveConfig('models')">💾 Save Model Settings</button>
    </div>
  </div>

  <!-- ── API Credentials ── -->
  <!-- CRM Contacts Page -->
  <div id="page-crm" class="page">
    <div class="page-header">
      <div class="page-title">👥 CRM Contacts</div>
      <div class="page-sub">Every caller recorded automatically — name, phone, call history</div>
    </div>
    <div class="section-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div class="section-title" style="margin:0;">All Contacts</div>
        <button class="btn btn-ghost btn-sm" onclick="loadCRM()">&#x21bb; Refresh</button>
      </div>
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="border-bottom:1px solid var(--border);">
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Name</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Phone</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Total Calls</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Last Seen</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:500;">Status</th>
            </tr>
          </thead>
          <tbody id="crm-tbody">
            <tr><td colspan="5" style="text-align:center;padding:32px;color:var(--muted);">Loading contacts...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── Enquiries Page ── -->
  <div id="page-enquiries" class="page">
    <div class="page-header">
      <div class="page-title">📋 Enquiries</div>
      <div class="page-sub">All property enquiries captured from AI calls — name, phone, requirement, budget</div>
    </div>
    <div class="section-card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:10px;">
        <div class="section-title" style="margin:0;">All Enquiries <span id="enq-count" style="font-size:13px;color:var(--muted);font-weight:400;"></span></div>
        <div style="display:flex;gap:10px;">
          <button class="btn btn-ghost btn-sm" onclick="loadEnquiries()">&#x21bb; Refresh</button>
          <button class="btn btn-primary btn-sm" onclick="downloadEnquiriesCSV()" style="background:linear-gradient(135deg,#10b981,#059669);border:none;">⬇️ Download CSV (Google Sheets)</button>
        </div>
      </div>
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="border-bottom:2px solid var(--border);">
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;white-space:nowrap;">📅 Date & Time</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">👤 Name</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">📞 Phone</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">🏠 Property Type</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">📍 Location</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">💰 Budget</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">🎯 Purpose</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">📝 Requirements</th>
              <th style="padding:10px 12px;text-align:left;color:var(--muted);font-weight:600;">📆 Booking</th>
            </tr>
          </thead>
          <tbody id="enq-tbody">
            <tr><td colspan="9" style="text-align:center;padding:48px;color:var(--muted);">Loading enquiries...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div id="page-languages" class="page">
    <div class="page-header">
      <div class="page-title">🌐 Language Presets</div>
      <div class="page-sub">One-click language configuration — saves immediately and takes effect on the next call</div>
    </div>
    <div class="section-card">
      <div class="section-title">Select a Language Mode</div>
      <div id="lang-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;"></div>
    </div>
    <div class="section-card" style="margin-top:0;">
      <div class="section-title">About Multilingual Mode</div>
      <p style="font-size:13px;color:var(--muted);line-height:1.7;">
        In <strong style="color:var(--text);">Multilingual (Auto)</strong> mode the agent listens to the caller's first message and 
        automatically replies in the same language for the rest of the call. 
        Ideal for showcasing the agent across different audiences.<br><br>
        Language changes take effect on the <strong style="color:var(--accent);">next incoming call</strong>. 
        The TTS voice and language code are updated automatically.
      </p>
    </div>
  </div>

  <!-- ── Outbound Calls Page ── -->
  <div id="page-outbound" class="page">
    <div class="page-header">
      <div class="page-title">📲 Outbound Calls</div>
      <div class="page-sub">Dispatch the AI agent to call any number instantly</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
      <div class="section-card">
        <div class="section-title">Single Call</div>
        <div class="form-group">
          <label>Phone Number (with country code)</label>
          <input type="text" id="call-single-num" placeholder="+91XXXXXXXXXX" style="font-family:monospace;">
          <div class="hint">Must start with + and country code e.g. +91</div>
        </div>
        <button class="btn btn-primary" onclick="makeSingleCall()" style="width:100%;">📞 Call Now</button>
        <div id="single-call-status" style="margin-top:12px;font-size:13px;"></div>
      </div>
      <div class="section-card">
        <div class="section-title">Bulk Call</div>
        <div class="form-group">
          <label>Phone Numbers (one per line)</label>
          <textarea id="call-bulk-nums" rows="6" placeholder="+91XXXXXXXXXX&#10;+91YYYYYYYYYY&#10;+44ZZZZZZZZZ"></textarea>
          <div class="hint">Each line is a separate call dispatched simultaneously</div>
        </div>
        <button class="btn btn-primary" onclick="makeBulkCall()" style="width:100%;">🚀 Call All Numbers</button>
        <div id="bulk-call-status" style="margin-top:12px;font-size:13px;"></div>
      </div>
    </div>
    <div class="section-card" id="call-results-card" style="display:none;">
      <div class="section-title">Call Results</div>
      <div id="call-results-body"></div>
    </div>
  </div>

  <!-- ── Demo Link Page ── -->
  <div id="page-demo" class="page">
    <div class="page-header">
      <div class="page-title">✨ Demo Link</div>
      <div class="page-sub">Generate a shareable browser link to let anyone test the AI agent live</div>
    </div>
    <div class="section-card" style="max-width:640px;">
      <div class="section-title">Browser Demo Call</div>
      <p style="font-size:13px;color:var(--muted);margin-bottom:20px;line-height:1.7;">
        Click <strong style="color:var(--text);">Generate Demo Link</strong> to create a unique session. 
        Share the link with anyone — they can talk to the AI agent directly from their browser, no app needed.
        Each session is valid for <strong style="color:var(--accent);">60 minutes</strong>.
      </p>
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
        <button class="btn btn-primary" onclick="generateDemo()">✨ Generate Demo Link</button>
        <button class="btn btn-ghost" id="copy-demo-btn" onclick="copyDemoLink()" style="display:none;">📋 Copy Link</button>
        <a id="open-demo-btn" href="#" target="_blank" class="btn btn-ghost" style="display:none;">↗ Open Demo</a>
      </div>
      <div id="demo-link-box" style="margin-top:16px;padding:12px 16px;background:var(--bg);border:1px solid var(--border);border-radius:8px;font-family:monospace;font-size:13px;color:var(--accent);display:none;word-break:break-all;"></div>
      <div id="demo-status" style="margin-top:10px;font-size:13px;color:var(--muted);"></div>
    </div>
    <div class="section-card" style="max-width:640px;margin-top:0;">
      <div class="section-title">Embedded Preview</div>
      <iframe id="demo-iframe" src="" style="width:100%;height:520px;border:none;border-radius:12px;background:#0f1117;display:none;"></iframe>
      <div style="font-size:12px;color:var(--muted);margin-top:8px;">The demo runs inside your dashboard. Use the generated link to share with others.</div>
    </div>
  </div>

    <div id="page-credentials" class="page">
    <div class="page-header">
      <div class="page-title">API Credentials</div>
      <div class="page-sub">Credentials here override .env values at runtime. Never share this page.</div>
    </div>
    <div class="section-card">
      <div class="section-title">LiveKit</div>
      <div class="form-row">
        <div class="form-group"><label>LiveKit URL</label><input type="text" id="livekit_url" value="{config.get('livekit_url', '')}"></div>
        <div class="form-group"><label>SIP Trunk ID</label><input type="text" id="sip_trunk_id" value="{config.get('sip_trunk_id', '')}"></div>
        <div class="form-group"><label>API Key</label><input type="password" id="livekit_api_key" value="{config.get('livekit_api_key', '')}"></div>
        <div class="form-group"><label>API Secret</label><input type="password" id="livekit_api_secret" value="{config.get('livekit_api_secret', '')}"></div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">AI Providers</div>
      <div class="form-row">
        <div class="form-group"><label>OpenAI API Key (optional if using Azure)</label><input type="password" id="openai_api_key" value="{config.get('openai_api_key', '')}"></div>
        <div class="form-group"><label>Sarvam API Key</label><input type="password" id="sarvam_api_key" value="{config.get('sarvam_api_key', '')}"></div>
      </div>
      <div class="form-row" style="margin-top:4px;">
        <div class="form-group"><label>Azure OpenAI API Key</label><input type="password" id="azure_openai_api_key" value="{config.get('azure_openai_api_key', '')}"></div>
        <div class="form-group"><label>Azure OpenAI Endpoint</label><input type="text" id="azure_openai_endpoint" value="{config.get('azure_openai_endpoint', '')}" placeholder="https://your-resource.openai.azure.com/"></div>
      </div>
      <div class="form-row" style="margin-top:4px;">
        <div class="form-group"><label>Azure Deployment Name</label><input type="text" id="azure_openai_deployment" value="{config.get('azure_openai_deployment', '')}" placeholder="gpt-4.1-mini"></div>
        <div class="form-group"></div>
      </div>
    </div>
    <div class="section-card">
      <div class="section-title">Integrations</div>
      <div class="form-row">
        <div class="form-group"><label>Cal.com API Key</label><input type="password" id="cal_api_key" value="{config.get('cal_api_key', '')}"></div>
        <div class="form-group"><label>Cal.com Event Type ID</label><input type="text" id="cal_event_type_id" value="{config.get('cal_event_type_id', '')}"></div>
        <div class="form-group"><label>Telegram Bot Token</label><input type="password" id="telegram_bot_token" value="{config.get('telegram_bot_token', '')}"></div>
        <div class="form-group"><label>Telegram Chat ID</label><input type="text" id="telegram_chat_id" value="{config.get('telegram_chat_id', '')}"></div>
        <div class="form-group"><label>Supabase URL</label><input type="text" id="supabase_url" value="{config.get('supabase_url', '')}"></div>
        <div class="form-group"><label>Supabase Anon Key</label><input type="password" id="supabase_key" value="{config.get('supabase_key', '')}"></div>
      </div>
    </div>
    <div class="save-bar">
      <span class="save-status" id="save-status-credentials">✅ Saved!</span>
      <button class="btn btn-primary" onclick="saveConfig('credentials')">💾 Save Credentials</button>
    </div>
  </div>

  <!-- ── Call Logs ── -->
  <div id="page-logs" class="page">
    <div class="page-header">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <div>
          <div class="page-title">Call Logs</div>
          <div class="page-sub">Full history of all incoming calls and transcripts</div>
        </div>
        <button class="btn btn-ghost" onclick="loadLogs()">↻ Refresh</button>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date & Time</th>
            <th>Phone</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Summary</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="logs-table-body"><tr><td colspan="6" style="text-align:center;padding:32px;color:var(--muted);">Click Refresh to load call logs</td></tr></tbody>
      </table>
    </div>
  </div>

</div><!-- /main -->

<script>
// ── Navigation ──────────────────────────────────────────────────────────────
function goTo(pageId, el) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + pageId).classList.add('active');
  el.classList.add('active');
}}

// ── Stats & Dashboard ───────────────────────────────────────────────────────
async function loadDashboard() {{
  try {{
    const [stats, logs] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/logs').then(r => r.json())
    ]);
    document.getElementById('stat-calls').textContent = stats.total_calls ?? '—';
    document.getElementById('stat-bookings').textContent = stats.total_bookings ?? '—';
    document.getElementById('stat-duration').textContent = stats.avg_duration ? stats.avg_duration + 's' : '—';
    document.getElementById('stat-rate').textContent = stats.booking_rate ? stats.booking_rate + '%' : '—';

    const tbody = document.getElementById('dash-table-body');
    if (!logs || logs.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--muted);">No calls yet. Make a test call!</td></tr>';
      return;
    }}
    tbody.innerHTML = logs.slice(0, 10).map(log => `
      <tr>
        <td style="color:var(--muted)">${{new Date(log.created_at).toLocaleString()}}</td>
        <td style="font-weight:600">${{log.phone_number || 'Unknown'}}</td>
        <td>${{log.duration_seconds || 0}}s</td>
        <td>${{badgeFor(log.summary)}}</td>
        <td>
          ${{log.id ? `<a style="color:var(--accent);font-size:12px;text-decoration:none;" href="/api/logs/${{log.id}}/transcript" download="transcript_${{log.id}}.txt">⬇ Download</a>` : ''}}
        </td>
      </tr>`).join('');
  }} catch(e) {{
    document.getElementById('dash-table-body').innerHTML = '<tr><td colspan="5" style="text-align:center;padding:24px;color:var(--muted);">Could not load data — check Supabase credentials.</td></tr>';
  }}
}}

function badgeFor(summary) {{
  if (!summary) return '<span class="badge badge-gray">Ended</span>';
  if (summary.toLowerCase().includes('confirm')) return '<span class="badge badge-green">✓ Booked</span>';
  if (summary.toLowerCase().includes('cancel')) return '<span class="badge badge-yellow">✗ Cancelled</span>';
  return '<span class="badge badge-gray">Completed</span>';
}}

// ── Call Logs ───────────────────────────────────────────────────────────────
async function loadLogs() {{
  const tbody = document.getElementById('logs-table-body');
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--muted);">Loading...</td></tr>';
  try {{
    const logs = await fetch('/api/logs').then(r => r.json());
    if (!logs || logs.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--muted);">No call logs found.</td></tr>';
      return;
    }}
    tbody.innerHTML = logs.map(log => `
      <tr>
        <td style="color:var(--muted);white-space:nowrap">${{new Date(log.created_at).toLocaleString()}}</td>
        <td style="font-weight:600">${{log.phone_number || 'Unknown'}}</td>
        <td>${{log.duration_seconds || 0}}s</td>
        <td>${{badgeFor(log.summary)}}</td>
        <td style="color:var(--muted);font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{log.summary || ''}}">${{log.summary || '—'}}</td>
        <td>
          ${{log.id ? `<a class="btn btn-ghost btn-sm" style="text-decoration:none;" href="/api/logs/${{log.id}}/transcript" download="transcript_${{log.id}}.txt">⬇ Transcript</a>` : '—'}}
          ${{log.recording_url ? `<a class="btn btn-ghost btn-sm" style="text-decoration:none;margin-left:4px;" href="${{log.recording_url}}" target="_blank">🎧 Recording</a>` : ''}}
        </td>
      </tr>`).join('');
  }} catch(e) {{
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:24px;color:#ef4444;">Error loading logs. Check Supabase credentials.</td></tr>';
  }}
}}

// ── Calendar ────────────────────────────────────────────────────────────────
let calYear = new Date().getFullYear();
let calMonth = new Date().getMonth();
let allBookings = [];

async function loadCalendar() {{
  try {{ allBookings = await fetch('/api/bookings').then(r => r.json()); }} catch(e) {{ allBookings = []; }}
  renderCalendar();
}}

function changeMonth(dir) {{ calMonth += dir; if (calMonth > 11) {{ calMonth = 0; calYear++; }} else if (calMonth < 0) {{ calMonth = 11; calYear--; }} renderCalendar(); }}

function renderCalendar() {{
  const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  document.getElementById('cal-month-label').textContent = `${{months[calMonth]}} ${{calYear}}`;
  const grid = document.getElementById('cal-grid');
  const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const today = new Date();

  // Build booking map by date string YYYY-MM-DD.
  // IMPORTANT: use booking_datetime (the actual appointment date) as the key,
  // NOT created_at (which is when the call/record was saved and may be a
  // different day — e.g. a booking made on June 21 for June 22 would
  // incorrectly appear on June 21 if we used created_at).
  // Fall back to created_at only when booking_datetime is absent.
  const bookMap = {{}};
  allBookings.forEach(b => {{
    // Prefer booking_datetime; it is already in IST (+05:30).
    // Fall back to created_at if no appointment time is recorded.
    const rawDate = b.booking_datetime || b.created_at;
    if (!rawDate) return;
    const dt = new Date(rawDate);
    // Build the date string in IST (UTC+5:30) so the day boundary is correct.
    const dtIST = new Date(dt.getTime() + (5.5 * 60 * 60 * 1000));
    const d = `${{dtIST.getUTCFullYear()}}-${{String(dtIST.getUTCMonth()+1).padStart(2,'0')}}-${{String(dtIST.getUTCDate()).padStart(2,'0')}}`;
    bookMap[d] = bookMap[d] || []; bookMap[d].push(b);
  }});

  let html = days.map(d => `<div class="cal-day-name">${{d}}</div>`).join('');

  const first = new Date(calYear, calMonth, 1);
  const last = new Date(calYear, calMonth + 1, 0);
  const startPad = first.getDay();

  // Prev month padding
  for (let i = 0; i < startPad; i++) {{
    const d = new Date(calYear, calMonth, -startPad + i + 1);
    html += `<div class="cal-cell other-month"><div class="cal-num">${{d.getDate()}}</div></div>`;
  }}

  for (let day = 1; day <= last.getDate(); day++) {{
    const dateStr = `${{calYear}}-${{String(calMonth+1).padStart(2,'0')}}-${{String(day).padStart(2,'0')}}`;
    const bks = bookMap[dateStr] || [];
    const isToday = today.getFullYear()===calYear && today.getMonth()===calMonth && today.getDate()===day;
    html += `<div class="cal-cell${{isToday?' today':''}}" onclick="showDay('${{dateStr}}', ${{JSON.stringify(bks).replace(/'/g,"&apos;")}})">
      <div class="cal-num">${{day}}</div>
      ${{bks.length ? `<div class="cal-dot"></div><div class="cal-booking-count">${{bks.length}} booking${{bks.length>1?'s':''}}</div>` : ''}}
    </div>`;
  }}

  // Next month padding
  const endPad = 6 - last.getDay();
  for (let i = 1; i <= endPad; i++) {{
    html += `<div class="cal-cell other-month"><div class="cal-num">${{i}}</div></div>`;
  }}

  grid.innerHTML = html;
  document.getElementById('day-panel').classList.remove('show');
}}

function showDay(dateStr, bookings) {{
  // Update old inline panel too
  const panel = document.getElementById('day-panel');
  if (panel) {{
    panel.classList.add('show');
    document.getElementById('day-panel-title').textContent = `Bookings on ${{dateStr}}`;
  }}
  // Open modal overlay
  openDayModal(dateStr, bookings);
}}

function openDayModal(dateStr, bookings) {{
  const modal = document.getElementById('day-modal');
  const dateObj = new Date(dateStr + 'T00:00:00');
  const formatted = dateObj.toLocaleDateString('en-IN', {{weekday:'long', year:'numeric', month:'long', day:'numeric'}});
  document.getElementById('modal-date-title').textContent = formatted;
  document.getElementById('modal-date-sub').textContent =
    bookings.length ? `${{bookings.length}} booking${{bookings.length>1?'s':''}} on this day` : 'No bookings on this day';

  if (!bookings || bookings.length === 0) {{
    document.getElementById('modal-bookings-body').innerHTML =
      '<div style="text-align:center;padding:32px;color:var(--muted);font-size:14px;">📅 No bookings on this day.</div>';
  }} else {{
    document.getElementById('modal-bookings-body').innerHTML = bookings.map(b => `
      <div class="booking-item">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <div style="font-weight:700;font-size:14px;">📞 ${{b.phone_number || 'Unknown'}}</div>
          <span class="badge badge-green">✅ Booked</span>
        </div>
        <div style="font-size:12px;color:var(--muted);margin-top:6px;">
          🕐 Appointment: ${{b.booking_datetime
            ? new Date(b.booking_datetime).toLocaleString('en-IN', {{timeZone:'Asia/Kolkata', hour:'2-digit', minute:'2-digit', day:'numeric', month:'short'}})
            : new Date(b.created_at).toLocaleTimeString('en-IN', {{hour:'2-digit',minute:'2-digit'}})
          }}
        </div>
        ${{b.summary ? `<div style="font-size:12px;color:var(--text);margin-top:6px;padding:8px;background:rgba(255,255,255,0.04);border-radius:6px;">💬 ${{b.summary}}</div>` : ''}}
      </div>`).join('');
  }}
  modal.classList.add('open');
}}

function closeDayModal() {{
  document.getElementById('day-modal').classList.remove('open');
}}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeDayModal(); }});

// ── CRM ────────────────────────────────────────────────────────────
async function loadCRM() {{
  const tbody = document.getElementById('crm-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:32px;color:var(--muted);">Loading contacts...</td></tr>';
  try {{
    const contacts = await fetch('/api/contacts').then(r => r.json());
    if (!contacts.length) {{
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--muted);">No contacts yet. They will appear here automatically after calls.</td></tr>';
      return;
    }}
    tbody.innerHTML = contacts.map(c => `
      <tr style="border-bottom:1px solid var(--border);transition:background 0.12s;" onmouseover="this.style.background='rgba(255,255,255,0.025)'" onmouseout="this.style.background=''">
        <td style="padding:14px 16px;font-weight:600;">${{c.caller_name || '<span style="color:var(--muted);font-weight:400;">Unknown</span>'}}</td>
        <td style="padding:14px 16px;font-family:monospace;font-size:13px;">${{c.phone_number || '—'}}</td>
        <td style="padding:14px 16px;text-align:center;"><span style="background:rgba(108,99,255,0.12);color:var(--accent);padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700;">${{c.total_calls}}</span></td>
        <td style="padding:14px 16px;color:var(--muted);font-size:12px;">${{c.last_seen ? new Date(c.last_seen).toLocaleString('en-IN') : '—'}}</td>
        <td style="padding:14px 16px;">${{c.is_booked
          ? '<span class="badge badge-green">✅ Booked</span>'
          : '<span class="badge badge-gray">📵 No booking</span>'}}</td>
      </tr>`).join('');
  }} catch(e) {{
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:24px;color:#ef4444;">Error loading contacts. Check Supabase credentials.</td></tr>';
  }}
}}

// ── Enquiries ───────────────────────────────────────────────────
let _enquiriesData = [];

async function loadEnquiries() {{
  const tbody = document.getElementById('enq-tbody');
  const countEl = document.getElementById('enq-count');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:48px;color:var(--muted);">Loading...</td></tr>';
  try {{
    const resp = await fetch('/api/enquiries').then(r => r.json());
    if (resp && resp.setup_required) {{
      if (countEl) countEl.textContent = '(setup needed)';
      tbody.innerHTML = '<tr><td colspan="9" style="padding:40px;text-align:center;">'
        + '<div style="font-size:36px;margin-bottom:16px;">&#x1F6E0;</div>'
        + '<div style="font-size:16px;font-weight:700;margin-bottom:8px;">One-time Setup Required</div>'
        + '<div style="font-size:13px;color:var(--muted);margin-bottom:20px;line-height:1.7;">'
        + 'Go to <a href="https://app.supabase.com" target="_blank" style="color:var(--accent);">app.supabase.com</a>'
        + ' &rarr; SQL Editor &rarr; New Query &rarr; paste and run the SQL from the file'
        + ' <code style="background:rgba(255,255,255,0.1);padding:2px 6px;border-radius:4px;">supabase_migration_enquiries.sql</code>'
        + ' in your project folder.</div>'
        + '<div style="font-size:12px;color:var(--muted);">Then click Refresh above.</div>'
        + '</td></tr>';
      return;
    }}
    _enquiriesData = (resp && resp.data) ? resp.data : (Array.isArray(resp) ? resp : []);
    if (!_enquiriesData.length) {{
      if (countEl) countEl.textContent = '';
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:64px;color:var(--muted);">'
        + '<div style="font-size:32px;margin-bottom:12px;">&#x1F4CB;</div>'
        + '<div style="font-size:15px;font-weight:600;margin-bottom:6px;">No Enquiries Yet</div>'
        + '<div style="font-size:13px;">They appear automatically after AI calls capture property requirements.</div>'
        + '</td></tr>';
      return;
    }}
    if (countEl) countEl.textContent = '(' + _enquiriesData.length + ' total)';
    const purposeBadge = function(p) {{
      if (!p) return '<span style="color:var(--muted);">-</span>';
      var color = p.toLowerCase().indexOf('invest') >= 0 ? '#f59e0b' : '#10b981';
      return '<span style="background:' + color + '22;color:' + color + ';padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;">' + p + '</span>';
    }};
    const propBadge = function(t) {{
      if (!t) return '<span style="color:var(--muted);">-</span>';
      var colors = {{flat:'#6366f1',plot:'#f59e0b',villa:'#10b981',commercial:'#ef4444',other:'#8b5cf6'}};
      var col = colors[t.toLowerCase()] || '#8b5cf6';
      return '<span style="background:' + col + '22;color:' + col + ';padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;">' + t + '</span>';
    }};
    const bookingBadge = function(confirmed, bdt) {{
      if (!confirmed) return '<span style="background:rgba(100,100,100,0.15);color:#9ca3af;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;">&#x2715; No</span>';
      var bdtStr = bdt ? new Date(bdt).toLocaleString('en-IN', {{timeZone:'Asia/Kolkata', day:'2-digit', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit'}}) : '';
      return '<span style="background:#10b98122;color:#10b981;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;">&#x2713; Yes</span>'
        + (bdtStr ? '<div style="font-size:10px;color:#10b981;margin-top:3px;white-space:nowrap;">' + bdtStr + '</div>' : '');
    }};
    tbody.innerHTML = _enquiriesData.map(function(e) {{
      var dt = e.created_at ? new Date(e.created_at).toLocaleString('en-IN', {{timeZone:'Asia/Kolkata'}}) : '-';
      return '<tr style="border-bottom:1px solid var(--border);">'
        + '<td style="padding:10px 12px;font-size:12px;color:var(--muted);white-space:nowrap;">' + dt + '</td>'
        + '<td style="padding:10px 12px;font-weight:600;">' + (e.caller_name || 'Unknown') + '</td>'
        + '<td style="padding:10px 12px;font-family:monospace;font-size:12px;">' + (e.caller_phone || '-') + '</td>'
        + '<td style="padding:10px 12px;">' + propBadge(e.property_type) + '</td>'
        + '<td style="padding:10px 12px;font-size:13px;">' + (e.location || '-') + '</td>'
        + '<td style="padding:10px 12px;font-weight:700;color:#10b981;">' + (e.budget || '-') + '</td>'
        + '<td style="padding:10px 12px;">' + purposeBadge(e.purpose) + '</td>'
        + '<td style="padding:10px 12px;font-size:12px;color:var(--muted);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + (e.requirements||'') + '">' + (e.requirements || '-') + '</td>'
        + '<td style="padding:10px 12px;min-width:110px;">' + bookingBadge(e.booking_confirmed, e.booking_datetime) + '</td>'
        + '</tr>';
    }}).join('');
  }} catch(ex) {{
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#ef4444;">Could not connect. Check server.</td></tr>';
  }}
}}

function downloadEnquiriesCSV() {{
  if (!_enquiriesData || !_enquiriesData.length) {{
    alert('No enquiry data. Go to Enquiries page first.');
    return;
  }}
  var NL = String.fromCharCode(13) + String.fromCharCode(10);
  var BOM = String.fromCharCode(65279);
  var headers = ['Date & Time (IST)','Name','Phone','Property Type','Location','Budget','Purpose','Requirements','Booking','Booking Date & Time (IST)'];
  var rows = _enquiriesData.map(function(e) {{
    var dt = e.created_at ? new Date(e.created_at).toLocaleString('en-IN',{{timeZone:'Asia/Kolkata'}}) : '';
    var bookingYN = e.booking_confirmed ? 'Yes' : 'No';
    var bookingDT = e.booking_datetime ? new Date(e.booking_datetime).toLocaleString('en-IN',{{timeZone:'Asia/Kolkata'}}) : '';
    return [dt, e.caller_name||'', e.caller_phone||'', e.property_type||'', e.location||'', e.budget||'', e.purpose||'', e.requirements||'', bookingYN, bookingDT]
      .map(function(v) {{ return '"' + String(v).replace(/"/g,'""') + '"'; }}).join(',');
  }});
  var csv = BOM + [headers.join(',')].concat(rows).join(NL);
  var blob = new Blob([csv], {{type:'text/csv;charset=utf-8;'}});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = 'mahakal_enquiries_' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
  URL.revokeObjectURL(url);
}}

// ── Save Config ─────────────────────────────────────────────────────────────
async function saveConfig(section) {{
  const get = id => {{ const el = document.getElementById(id); return el ? el.value : null; }};

  const payload = {{}};

  if (section === 'agent') {{
    Object.assign(payload, {{
      first_line: get('first_line'),
      agent_instructions: get('agent_instructions'),
      stt_min_endpointing_delay: parseFloat(get('stt_min_endpointing_delay')),
    }});
  }} else if (section === 'models') {{
    // Read ElevenLabs voice — prefer custom input if filled
    const elCustom = (document.getElementById('elevenlabs_voice_id_custom') || {{}}).value || '';
    const elVoiceId = elCustom.trim() || get('elevenlabs_voice_id') || '21m00Tcm4TlvDq8ikWAM';
    Object.assign(payload, {{
      llm_model: get('llm_model'),
      llm_provider: get('llm_provider'),
      tts_provider: get('tts_provider'),
      tts_voice: get('tts_voice'),
      tts_language: get('tts_language'),
      elevenlabs_voice_id: elVoiceId,
    }});
  }} else if (section === 'credentials') {{
    Object.assign(payload, {{
      livekit_url: get('livekit_url'), sip_trunk_id: get('sip_trunk_id'),
      livekit_api_key: get('livekit_api_key'), livekit_api_secret: get('livekit_api_secret'),
      openai_api_key: get('openai_api_key'), sarvam_api_key: get('sarvam_api_key'),
      azure_openai_api_key: get('azure_openai_api_key'),
      azure_openai_endpoint: get('azure_openai_endpoint'),
      azure_openai_deployment: get('azure_openai_deployment'),
      cal_api_key: get('cal_api_key'), cal_event_type_id: get('cal_event_type_id'),
      telegram_bot_token: get('telegram_bot_token'), telegram_chat_id: get('telegram_chat_id'),
      supabase_url: get('supabase_url'), supabase_key: get('supabase_key'),
    }});
  }}

  const res = await fetch('/api/config', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }});

  const statusEl = document.getElementById('save-status-' + section);
  if (res.ok) {{
    statusEl.style.opacity = '1';
    setTimeout(() => {{ statusEl.style.opacity = '0'; }}, 2500);
  }} else {{
    alert('Failed to save. Check server logs.');
  }}
}}


// ── Language Presets ─────────────────────────────────────────────────────────
const LANG_PRESETS = {{
  hinglish:    {{ flag:'🇮🇳', label:'Hinglish',                sub:'Hindi + English mix',        color:'#6c63ff' }},
  hindi:       {{ flag:'🇮🇳', label:'Hindi',                   sub:'Pure Hindi',                  color:'#a855f7' }},
  english:     {{ flag:'🇬🇧', label:'English (India)',          sub:'Indian English',              color:'#3b82f6' }},
  tamil:       {{ flag:'🇮🇳', label:'Tamil',                   sub:'தமிழ்',                       color:'#f59e0b' }},
  telugu:      {{ flag:'🇮🇳', label:'Telugu',                  sub:'తెలుగు',                      color:'#10b981' }},
  gujarati:    {{ flag:'🇮🇳', label:'Gujarati',                sub:'ગુજરાતી',                     color:'#ef4444' }},
  bengali:     {{ flag:'🇮🇳', label:'Bengali',                 sub:'বাংলা',                       color:'#f97316' }},
  marathi:     {{ flag:'🇮🇳', label:'Marathi',                 sub:'मराठी',                       color:'#14b8a6' }},
  kannada:     {{ flag:'🇮🇳', label:'Kannada',                 sub:'ಕನ್ನಡ',                       color:'#8b5cf6' }},
  malayalam:   {{ flag:'🇮🇳', label:'Malayalam',               sub:'മലയാളം',                      color:'#ec4899' }},
  multilingual:{{ flag:'🌍', label:'Multilingual (Auto)',       sub:"Detects caller's language",   color:'#22c55e' }},
}};

let currentLangPreset = 'hinglish';

async function initLanguagePage() {{
  try {{
    const cfg = await fetch('/api/config').then(r=>r.json());
    currentLangPreset = cfg.lang_preset || 'hinglish';
  }} catch(e) {{}}
  renderLangGrid();
  // Apply TTS provider visibility on page load
  const providerEl = document.getElementById('tts_provider');
  if (providerEl) onTTSProviderChange(providerEl.value);
}}

// ── TTS Provider Switcher ─────────────────────────────────────────────────────
function onTTSProviderChange(provider) {{
  const sarvam  = document.getElementById('sarvam-options');
  const el      = document.getElementById('elevenlabs-options');
  const banner  = document.getElementById('el-info-banner');
  const title   = document.querySelector('.section-card .section-title');

  if (provider === 'elevenlabs') {{
    if (sarvam) sarvam.style.display = 'none';
    if (el)     el.style.display = 'flex';
    if (banner) banner.style.display = 'block';
  }} else {{
    if (sarvam) sarvam.style.display = 'flex';
    if (el)     el.style.display = 'none';
    if (banner) banner.style.display = 'none';
  }}
}}

function renderLangGrid() {{
  const grid = document.getElementById('lang-grid');
  if (!grid) return;
  grid.innerHTML = Object.entries(LANG_PRESETS).map(([id, p]) => `
    <div onclick="selectLangPreset('${{id}}')" style="
      background:${{id===currentLangPreset ? 'rgba(108,99,255,0.15)' : 'var(--bg)'}};
      border:2px solid ${{id===currentLangPreset ? p.color : 'var(--border)'}};
      border-radius:12px;padding:18px;cursor:pointer;transition:all 0.15s;
      ${{id===currentLangPreset ? 'box-shadow:0 0 16px rgba(108,99,255,0.2)' : ''}}
    " onmouseover="this.style.borderColor='${{p.color}}'" onmouseout="this.style.borderColor='${{id===currentLangPreset?p.color:\'var(--border)\'}}'">
      <div style="font-size:28px;margin-bottom:8px;">${{p.flag}}</div>
      <div style="font-weight:700;font-size:14px;color:${{id===currentLangPreset?p.color:'var(--text)'}}">${{p.label}}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:3px;">${{p.sub}}</div>
      ${{id===currentLangPreset ? '<div style="font-size:10px;color:#22c55e;margin-top:6px;font-weight:600;">✓ ACTIVE</div>' : ''}}
    </div>`).join('');
}}

async function selectLangPreset(id) {{
  const p = LANG_PRESETS[id];
  if (!p) return;
  currentLangPreset = id;
  renderLangGrid();
  // Save lang_preset and tts_language ONLY — do NOT overwrite the user's
  // manually-chosen tts_voice in Models & Voice. Voice stays whatever the
  // user last set there.
  try {{
    const langs = {{ hinglish:'hi-IN', hindi:'hi-IN', english:'en-IN', tamil:'ta-IN', telugu:'te-IN', gujarati:'gu-IN', bengali:'bn-IN', marathi:'mr-IN', kannada:'kn-IN', malayalam:'ml-IN', multilingual:'hi-IN' }};
    await fetch('/api/config', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ lang_preset: id, tts_language: langs[id] }})
    }});
    // Also update the tts_language dropdown on Models & Voice page if visible
    const langEl = document.getElementById('tts_language');
    if (langEl && langs[id]) langEl.value = langs[id];
    const toast = document.createElement('div');
    toast.style.cssText='position:fixed;bottom:24px;right:24px;background:#22c55e;color:#fff;padding:12px 20px;border-radius:10px;font-size:13px;font-weight:600;z-index:9999;animation:slideUp 0.3s ease';
    toast.textContent = `✅ ${{p.label}} preset activated! Voice unchanged.`;
    document.body.appendChild(toast);
    setTimeout(()=>toast.remove(), 2500);
  }} catch(e) {{ alert('Failed to save: ' + e); }}
}}

// ── Outbound Calls ─────────────────────────────────────────────────────────── 
async function makeSingleCall() {{
  const phone = document.getElementById('call-single-num').value.trim();
  if (!phone) return;
  const el = document.getElementById('single-call-status');
  el.textContent = '⏳ Dispatching...';
  el.style.color = 'var(--muted)';
  try {{
    const res = await fetch('/api/call/single', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{phone}})
    }}).then(r=>r.json());
    if (res.status === 'ok') {{
      el.innerHTML = `✅ Call dispatched! Dispatch ID: <code>${{res.dispatch_id}}</code>`;
      el.style.color = 'var(--green)';
    }} else {{
      el.textContent = '❌ ' + res.message;
      el.style.color = 'var(--red)';
    }}
  }} catch(e) {{
    el.textContent = '❌ Error: ' + e;
    el.style.color = 'var(--red)';
  }}
}}

async function makeBulkCall() {{
  const nums = document.getElementById('call-bulk-nums').value.trim();
  if (!nums) return;
  const el = document.getElementById('bulk-call-status');
  el.textContent = '⏳ Dispatching all numbers...';
  try {{
    const res = await fetch('/api/call/bulk', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{numbers: nums}})
    }}).then(r=>r.json());
    const results = res.results || [];
    document.getElementById('call-results-card').style.display = 'block';
    document.getElementById('call-results-body').innerHTML = results.map(r => `
      <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);">
        <span style="font-family:monospace;">${{r.phone}}</span>
        <span class="badge ${{r.status==='ok'?'badge-green':'badge-gray'}}">${{r.status==='ok'?'✅ Sent':'❌ '+r.message}}</span>
      </div>`).join('');
    el.textContent = `✅ ${{results.filter(r=>r.status==='ok').length}}/${{results.length}} calls dispatched`;
    el.style.color = 'var(--green)';
  }} catch(e) {{
    el.textContent = '❌ Error: ' + e;
    el.style.color = 'var(--red)';
  }}
}}

// ── Demo Link ─────────────────────────────────────────────────────────────────
let demoUrl = '';
function initDemo() {{
  // no-op until user clicks generate
}}
async function generateDemo() {{
  const statusEl = document.getElementById('demo-status');
  statusEl.textContent = '⏳ Generating session...';
  try {{
    const origin = window.location.origin;
    demoUrl = origin + '/demo';
    document.getElementById('demo-link-box').textContent = demoUrl;
    document.getElementById('demo-link-box').style.display = 'block';
    document.getElementById('copy-demo-btn').style.display = 'inline-flex';
    document.getElementById('open-demo-btn').style.display = 'inline-flex';
    document.getElementById('open-demo-btn').href = demoUrl;
    document.getElementById('demo-iframe').src = demoUrl;
    document.getElementById('demo-iframe').style.display = 'block';
    statusEl.textContent = 'Session ready — share the link or use the preview below';
  }} catch(e) {{
    statusEl.textContent = '❌ ' + e;
  }}
}}
function copyDemoLink() {{
  navigator.clipboard.writeText(demoUrl);
  document.getElementById('copy-demo-btn').textContent = '✅ Copied!';
  setTimeout(()=>document.getElementById('copy-demo-btn').textContent='📋 Copy Link', 2000);
}}

// ── Boot ────────────────────────────────────────────────────────────────────
loadDashboard();
</script>
</body>
</html>"""

    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ui_server:app", host="0.0.0.0", port=8000, reload=True)
