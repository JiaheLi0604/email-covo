"""
dashboard.py
------------
Web dashboard for Cratus Email Automation.

Brian opens a browser URL and can:
  - View and edit config settings (schedule, flow, subject)
  - View all deal threads and their current state
  - Add new information to a thread
  - Trigger a manual run
  - See today's scheduled sessions and recent run history

Run locally:
    pip install flask
    python dashboard.py
    Open http://localhost:5000

Deploy to Railway/Render:
    Set PORT environment variable (handled automatically by cloud platforms).
"""

import json
import os
import random
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from functools import wraps

import msal
from flask import Flask, Response, jsonify, request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
THREADS_DIR = BASE_DIR / "threads"
RUN_LOG_FILE = BASE_DIR / "run_log.json"

# ---------------------------------------------------------------------------
# Auth constants (mirrors outlook_service.py)
# ---------------------------------------------------------------------------
_MSAL_CLIENT_ID = "3637aa14-8277-4ef6-b28c-fc82f40908f2"
_MSAL_AUTHORITY = "https://login.microsoftonline.com/common"
_MSAL_SCOPES    = ["Mail.ReadWrite", "Mail.Send", "User.Read"]
# Set TOKEN_DIR env var to a Railway Volume mount path (e.g. /data) so
# tokens survive redeployment.  Falls back to the project directory.
TOKEN_DIR = Path(os.environ.get("TOKEN_DIR", str(BASE_DIR)))

app = Flask(__name__)


@app.before_request
def _require_password():
    """HTTP Basic Auth gate.  Set DASHBOARD_PASSWORD env var to enable."""
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    if not pw:
        return  # no password configured → allow all
    auth = request.authorization
    if not auth or auth.password != pw:
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Cratus Dashboard"'},
        )

# ---------------------------------------------------------------------------
# In-memory run state (reset on process restart)
# ---------------------------------------------------------------------------

_run_state: dict  = {"running": False, "started_at": None, "error": None}
_auth_flows: dict = {}   # bot_id → {status, user_code, verification_uri, ...}

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

def _save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def _list_thread_ids() -> list:
    if not THREADS_DIR.exists():
        return []
    return sorted(p.stem for p in THREADS_DIR.glob("*.json"))

def _load_thread(tid: str) -> dict:
    with open(THREADS_DIR / f"{tid}.json", encoding="utf-8") as f:
        return json.load(f)

def _save_thread(tid: str, data: dict) -> None:
    with open(THREADS_DIR / f"{tid}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _load_run_log() -> list:
    if not RUN_LOG_FILE.exists():
        return []
    with open(RUN_LOG_FILE, encoding="utf-8") as f:
        return json.load(f)

def _append_run_log(entry: dict) -> None:
    log = _load_run_log()
    log.insert(0, entry)
    with open(RUN_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log[:50], f, indent=2)

def _compute_today_schedule(schedule_cfg: dict) -> list:
    """Replicate scheduler.py's date-seeded RNG to show today's planned times."""
    tz_str = schedule_cfg.get("timezone", "America/New_York")
    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)
    today = now.date()

    if today.weekday() >= 5:          # weekend
        return []

    seed = int(today.strftime("%Y%m%d"))
    rng = random.Random(seed)

    runs_cfg = schedule_cfg.get("runs_per_day", {})
    n = rng.randint(int(runs_cfg.get("min", 2)), int(runs_cfg.get("max", 3)))

    window = schedule_cfg.get("window", {})
    ws_h, ws_m = map(int, window.get("start", "10:00").split(":"))
    we_h, we_m = map(int, window.get("end",   "17:00").split(":"))
    start_min = ws_h * 60 + ws_m
    end_min   = we_h * 60 + we_m
    min_gap   = int(schedule_cfg.get("min_gap_minutes", 90))

    slots: list = []
    for _ in range(n):
        for _ in range(200):
            c = rng.randint(start_min, end_min - 1)
            if all(abs(c - s) >= min_gap for s in slots):
                slots.append(c)
                break

    slots.sort()
    now_min = now.hour * 60 + now.minute

    result = []
    for m in slots:
        h, mi = divmod(m, 60)
        ampm = "am" if h < 12 else "pm"
        h12  = h % 12 or 12
        result.append({
            "time": f"{h12}:{mi:02d}{ampm} ET",
            "past": m < now_min,
        })
    return result

# ---------------------------------------------------------------------------
# API — config
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_get_config():
    cfg = _load_config()
    sch = cfg.get("schedule", {})
    rr  = cfg.get("flow_selection", {}).get("random_rounds", {})
    return jsonify({
        "subject":          cfg.get("subject", ""),
        "schedule_enabled": sch.get("enabled", True),
        "runs_min":         sch.get("runs_per_day", {}).get("min", 2),
        "runs_max":         sch.get("runs_per_day", {}).get("max", 3),
        "window_start":     sch.get("window", {}).get("start", "10:00"),
        "window_end":       sch.get("window", {}).get("end",   "17:00"),
        "min_gap":          sch.get("min_gap_minutes", 90),
        "rounds_min":       rr.get("min", 3),
        "rounds_max":       rr.get("max", 6),
        "active_threads":   cfg.get("active_threads", []),
    })

@app.route("/api/config", methods=["POST"])
def api_save_config():
    d   = request.get_json() or {}
    cfg = _load_config()

    if "subject" in d:
        cfg["subject"] = d["subject"]

    sch = cfg.setdefault("schedule", {})
    if "schedule_enabled" in d:
        sch["enabled"] = bool(d["schedule_enabled"])
    if "runs_min" in d or "runs_max" in d:
        rp = sch.setdefault("runs_per_day", {})
        if "runs_min" in d: rp["min"] = int(d["runs_min"])
        if "runs_max" in d: rp["max"] = int(d["runs_max"])
    if "window_start" in d or "window_end" in d:
        w = sch.setdefault("window", {})
        if "window_start" in d: w["start"] = d["window_start"]
        if "window_end"   in d: w["end"]   = d["window_end"]
    if "min_gap" in d:
        sch["min_gap_minutes"] = int(d["min_gap"])

    rr = cfg.setdefault("flow_selection", {}).setdefault("random_rounds", {})
    if "rounds_min" in d: rr["min"] = int(d["rounds_min"])
    if "rounds_max" in d: rr["max"] = int(d["rounds_max"])

    _save_config(cfg)
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# API — threads
# ---------------------------------------------------------------------------

@app.route("/api/threads")
def api_threads():
    cfg = _load_config()
    active_set = set(cfg.get("active_threads", []))
    out = []
    for tid in _list_thread_ids():
        try:
            t = _load_thread(tid)
            out.append({
                "id":             tid,
                "name":           t.get("thread_id", tid).replace("_", " ").title(),
                "stage":          t.get("stage", ""),
                "run_count":      len(t.get("history", [])),
                "open_questions": sum(1 for q in t.get("open_questions", []) if not q.get("resolved")),
                "open_actions":   sum(1 for a in t.get("action_items",   []) if a.get("status") == "open"),
                "last_run_summary":  t.get("last_run_summary", ""),
                "discussion_focus":  t.get("discussion_focus", ""),
                "is_active":      tid in active_set,
            })
        except Exception:
            pass
    return jsonify(out)

@app.route("/api/threads/<tid>")
def api_thread_detail(tid):
    try:
        return jsonify(_load_thread(tid))
    except FileNotFoundError:
        return jsonify({"error": "not found"}), 404

@app.route("/api/threads/<tid>/new_info", methods=["POST"])
def api_add_info(tid):
    info = (request.get_json() or {}).get("info", "").strip()
    if not info:
        return jsonify({"error": "empty"}), 400
    t = _load_thread(tid)
    t.setdefault("new_information", []).append(info)
    _save_thread(tid, t)
    return jsonify({"ok": True})

@app.route("/api/threads/<tid>", methods=["DELETE"])
def api_delete_thread(tid):
    path = THREADS_DIR / f"{tid}.json"
    if path.exists():
        path.unlink()
    cfg = _load_config()
    cfg["active_threads"] = [t for t in cfg.get("active_threads", []) if t != tid]
    _save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/threads/active", methods=["POST"])
def api_set_active():
    tids = (request.get_json() or {}).get("active_threads", [])
    cfg = _load_config()
    cfg["active_threads"] = tids
    _save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/threads/new", methods=["POST"])
def api_new_thread():
    import anthropic as _ant
    d    = request.get_json() or {}
    name = d.get("name", "").strip()
    content = d.get("content", "").strip()
    if not name or not content:
        return jsonify({"error": "name and content required"}), 400

    tid = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if (THREADS_DIR / f"{tid}.json").exists():
        return jsonify({"error": f"Thread '{tid}' already exists"}), 409

    prompt = f"""You are analyzing a real estate deal listing. Extract structured information and return a JSON object with EXACTLY these fields:

{{
  "thread_id": "{tid}",
  "stage": "initial_review",
  "discussion_focus": "<one sentence describing the main focus>",
  "last_run_summary": "",
  "known_facts": ["<fact1>", "<fact2>", ...],
  "open_questions": [{{"question": "<question>", "resolved": false}}, ...],
  "action_items": [{{"item": "<action>", "owner": "<Li|Brian|Joey Wan>", "status": "open"}}, ...],
  "decisions": [],
  "new_information": [],
  "history": []
}}

Extract 5-15 known facts, 3-8 open questions, 2-5 action items. Return ONLY valid JSON.

CONTENT:
{content[:4000]}"""

    client = _ant.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        thread_data = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Parse error: {e}"}), 500

    THREADS_DIR.mkdir(exist_ok=True)
    _save_thread(tid, thread_data)

    cfg = _load_config()
    if tid not in cfg.get("active_threads", []):
        cfg.setdefault("active_threads", []).append(tid)
        _save_config(cfg)

    return jsonify({"ok": True, "id": tid})

@app.route("/api/threads/<tid>", methods=["PATCH"])
def api_patch_thread(tid):
    d = request.get_json() or {}
    try:
        t = _load_thread(tid)
    except FileNotFoundError:
        return jsonify({"error": "not found"}), 404
    for field in ["known_facts", "open_questions", "action_items", "decisions", "new_information"]:
        if field in d:
            t[field] = d[field]
    _save_thread(tid, t)
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# API — schedule
# ---------------------------------------------------------------------------

@app.route("/api/schedule/today")
def api_today_schedule():
    cfg = _load_config()
    sch = cfg.get("schedule", {})
    if not sch.get("enabled", False):
        return jsonify([])
    return jsonify(_compute_today_schedule(sch))

# ---------------------------------------------------------------------------
# API — run
# ---------------------------------------------------------------------------

@app.route("/api/run", methods=["POST"])
def api_run():
    if _run_state["running"]:
        return jsonify({"error": "A run is already in progress"}), 409

    d = request.get_json() or {}
    thread_id_override = d.get("thread_id") or None
    topic_override     = (d.get("topic") or "").strip() or None

    def _do():
        _run_state["running"]    = True
        _run_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _run_state["error"]      = None
        turns  = 0
        status = "failed"
        try:
            from main import load_config as lc, run_conversation, _auto_update_thread
            cfg        = lc()
            transcript = run_conversation(
                cfg,
                thread_id_override=thread_id_override,
                topic_override=topic_override,
            )
            if transcript:
                turns  = len(transcript)
                status = "completed"
                if cfg.get("conversation_mode") == "deal":
                    _auto_update_thread(
                        cfg, transcript,
                        thread_id_override=thread_id_override,
                        topic_override=topic_override,
                    )
        except Exception as e:
            _run_state["error"] = str(e)
            status = "error"
        finally:
            _run_state["running"] = False
        _append_run_log({
            "started_at":  _run_state["started_at"],
            "turns":       turns,
            "status":      status,
            "error":       _run_state.get("error"),
            "thread_id":   thread_id_override or "auto",
            "topic":       topic_override or "",
        })

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/run/status")
def api_run_status():
    return jsonify(_run_state)

@app.route("/api/runlog")
def api_runlog():
    return jsonify(_load_run_log()[:10])


# ---------------------------------------------------------------------------
# API — Rollback / restore
# ---------------------------------------------------------------------------

@app.route("/api/threads/<tid>/rollback/<int:index>", methods=["POST"])
def api_rollback(tid, index):
    """Soft-discard history[index:] and restore thread state to before that run."""
    if _run_state["running"]:
        return jsonify({"error": "等当前 run 跑完再操作"}), 409
    try:
        t = _load_thread(tid)
    except FileNotFoundError:
        return jsonify({"error": "not found"}), 404

    history = t.get("history", [])
    if index < 0 or index >= len(history):
        return jsonify({"error": "invalid index"}), 400

    snapshot = history[index].get("snapshot_before")
    if not snapshot:
        return jsonify({"error": "No snapshot available for this entry (run predates this feature)"}), 400

    # Save current full state as rollback_backup so restore is possible
    t["_rollback_backup"] = {
        "known_facts":      list(t.get("known_facts", [])),
        "open_questions":   [q.copy() for q in t.get("open_questions", [])],
        "action_items":     [a.copy() for a in t.get("action_items", [])],
        "decisions":        list(t.get("decisions", [])),
        "new_information":  list(t.get("new_information", [])),
        "last_run_summary": t.get("last_run_summary", ""),
        "discussion_focus": t.get("discussion_focus", ""),
        "discarded_from":   index,
    }

    # Restore thread state to pre-run snapshot
    for field in ["known_facts", "open_questions", "action_items", "decisions",
                  "new_information", "last_run_summary", "discussion_focus"]:
        if field in snapshot:
            t[field] = snapshot[field]

    # Soft-discard this and all subsequent history entries
    for i in range(index, len(history)):
        history[i]["discarded"] = True

    _save_thread(tid, t)
    return jsonify({"ok": True})


@app.route("/api/threads/<tid>/restore", methods=["POST"])
def api_restore(tid):
    """Undo a rollback: restore thread state from _rollback_backup and un-discard entries."""
    if _run_state["running"]:
        return jsonify({"error": "等当前 run 跑完再操作"}), 409
    try:
        t = _load_thread(tid)
    except FileNotFoundError:
        return jsonify({"error": "not found"}), 404

    backup = t.get("_rollback_backup")
    if not backup:
        return jsonify({"error": "No rollback backup found for this thread"}), 400

    discarded_from = backup.get("discarded_from", 0)

    # Restore state
    for field in ["known_facts", "open_questions", "action_items", "decisions",
                  "new_information", "last_run_summary", "discussion_focus"]:
        if field in backup:
            t[field] = backup[field]

    # Un-discard entries
    for i in range(discarded_from, len(t.get("history", []))):
        t["history"][i].pop("discarded", None)

    # Clear the backup
    t.pop("_rollback_backup", None)

    _save_thread(tid, t)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — Outlook auth
# ---------------------------------------------------------------------------

def _check_token_status(bot_id: str) -> str:
    """Returns 'connected', 'expired', or 'unauthorized'."""
    token_path = TOKEN_DIR / f"token_{bot_id}.json"
    if not token_path.exists():
        return "unauthorized"
    try:
        cache = msal.SerializableTokenCache()
        cache.deserialize(token_path.read_text(encoding="utf-8"))
        app_obj = msal.PublicClientApplication(
            _MSAL_CLIENT_ID, authority=_MSAL_AUTHORITY, token_cache=cache
        )
        accounts = app_obj.get_accounts()
        if not accounts:
            return "unauthorized"
        result = app_obj.acquire_token_silent(_MSAL_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return "connected"
        return "expired"
    except Exception:
        return "expired"


@app.route("/api/auth/<bot_id>/status")
def api_auth_status(bot_id):
    if bot_id not in ("bot_a", "bot_b", "bot_c"):
        return jsonify({"error": "invalid bot"}), 400
    flow   = _auth_flows.get(bot_id, {})
    return jsonify({
        "token_status": _check_token_status(bot_id),
        "flow_status":  flow.get("status", "idle"),
    })


@app.route("/api/auth/<bot_id>/start", methods=["POST"])
def api_auth_start(bot_id):
    if bot_id not in ("bot_a", "bot_b", "bot_c"):
        return jsonify({"error": "invalid bot"}), 400

    # Return existing code if a flow is already running
    existing = _auth_flows.get(bot_id, {})
    if existing.get("status") == "polling":
        return jsonify({
            "user_code":        existing["user_code"],
            "verification_uri": existing["verification_uri"],
            "message":          existing.get("message", ""),
        })

    cache   = msal.SerializableTokenCache()
    app_obj = msal.PublicClientApplication(
        _MSAL_CLIENT_ID, authority=_MSAL_AUTHORITY, token_cache=cache
    )
    flow = app_obj.initiate_device_flow(scopes=_MSAL_SCOPES)

    if "user_code" not in flow:
        return jsonify({
            "error": "Failed to start device code flow. Make sure 'Allow public client flows' is ON in Azure."
        }), 500

    token_path = TOKEN_DIR / f"token_{bot_id}.json"

    _auth_flows[bot_id] = {
        "status":           "polling",
        "user_code":        flow["user_code"],
        "verification_uri": flow.get("verification_uri", "https://microsoft.com/devicelogin"),
        "message":          flow.get("message", ""),
    }

    def _poll():
        try:
            result = app_obj.acquire_token_by_device_flow(flow)
            if "access_token" in result:
                TOKEN_DIR.mkdir(parents=True, exist_ok=True)
                token_path.write_text(cache.serialize(), encoding="utf-8")
                _auth_flows[bot_id]["status"] = "connected"
            else:
                _auth_flows[bot_id]["status"] = "failed"
        except Exception as exc:
            _auth_flows[bot_id]["status"] = "failed"
            _auth_flows[bot_id]["error"]  = str(exc)

    threading.Thread(target=_poll, daemon=True).start()

    return jsonify({
        "user_code":        flow["user_code"],
        "verification_uri": flow.get("verification_uri", "https://microsoft.com/devicelogin"),
        "message":          flow.get("message", ""),
        "expires_in":       flow.get("expires_in", 900),
    })


# ---------------------------------------------------------------------------
# API — bots / prompts
# ---------------------------------------------------------------------------

@app.route("/api/bots")
def api_bots():
    cfg = _load_config()
    bots = []
    for key in ["bot_a", "bot_b", "bot_c"]:
        if key in cfg:
            b = cfg[key]
            bots.append({
                "id": key,
                "name": b.get("name", ""),
                "role": b.get("role", ""),
                "style": b.get("style", ""),
                "template": b.get("template", key),
                "email": b.get("email", ""),
            })
    return jsonify(bots)

@app.route("/api/bots/<bot_id>", methods=["POST"])
def api_save_bot(bot_id):
    if bot_id not in ["bot_a", "bot_b", "bot_c"]:
        return jsonify({"error": "invalid bot"}), 400
    d = request.get_json() or {}
    cfg = _load_config()
    if bot_id not in cfg:
        return jsonify({"error": "bot not found"}), 404
    if "role"  in d: cfg[bot_id]["role"]  = d["role"]
    if "style" in d: cfg[bot_id]["style"] = d["style"]
    if "email" in d: cfg[bot_id]["email"] = d["email"]
    _save_config(cfg)
    return jsonify({"ok": True})

@app.route("/api/prompts/<bot_id>")
def api_get_prompt(bot_id):
    if bot_id not in ["bot_a", "bot_b", "bot_c"]:
        return jsonify({"error": "invalid bot"}), 400
    cfg = _load_config()
    template = cfg.get(bot_id, {}).get("template", bot_id)
    prompt_file = BASE_DIR / "prompts" / f"{template}.md"
    try:
        content = prompt_file.read_text(encoding="utf-8")
        return jsonify({"content": content})
    except FileNotFoundError:
        return jsonify({"content": ""})

@app.route("/api/prompts/<bot_id>", methods=["POST"])
def api_save_prompt(bot_id):
    if bot_id not in ["bot_a", "bot_b", "bot_c"]:
        return jsonify({"error": "invalid bot"}), 400
    content = (request.get_json() or {}).get("content", "")
    cfg = _load_config()
    template = cfg.get(bot_id, {}).get("template", bot_id)
    prompt_file = BASE_DIR / "prompts" / f"{template}.md"
    prompt_file.parent.mkdir(exist_ok=True)
    prompt_file.write_text(content, encoding="utf-8")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — Chat (embedded Claude chatbot)
# ---------------------------------------------------------------------------

def _execute_chat_tool(name: str, inp: dict) -> dict:
    """Execute a tool call from the embedded chatbot."""
    try:
        if name == "list_threads":
            cfg = _load_config()
            active_set = set(cfg.get("active_threads", []))
            threads = []
            for tid in _list_thread_ids():
                try:
                    t = _load_thread(tid)
                    history = [h for h in t.get("history", []) if not h.get("discarded")]
                    threads.append({
                        "id": tid,
                        "name": t.get("thread_id", tid).replace("_", " ").title(),
                        "stage": t.get("stage", ""),
                        "active": tid in active_set,
                        "discussion_focus": t.get("discussion_focus", ""),
                        "last_run_summary": t.get("last_run_summary", ""),
                        "run_count": len(history),
                        "open_questions": sum(1 for q in t.get("open_questions", []) if not q.get("resolved")),
                        "open_actions": sum(1 for a in t.get("action_items", []) if a.get("status") == "open"),
                        "known_facts_count": len(t.get("known_facts", [])),
                    })
                except Exception:
                    pass
            return {"threads": threads}

        elif name == "get_thread":
            tid = inp.get("thread_id", "")
            t = _load_thread(tid)
            t_clean = dict(t)
            t_clean["history"] = [
                {k: v for k, v in h.items() if k not in ("transcript", "snapshot_before")}
                for h in t.get("history", []) if not h.get("discarded")
            ]
            return {"thread": t_clean}

        elif name == "patch_thread":
            tid = inp.get("thread_id", "")
            op  = inp.get("operation", "")
            val = inp.get("value", "")
            idx = inp.get("index")
            owner = inp.get("owner", "Li")

            t = _load_thread(tid)
            changed = False
            action_summary = ""

            if op == "add_fact":
                t.setdefault("known_facts", []).append(val)
                changed = True; action_summary = f"Added fact to {tid}: '{val}'"
            elif op == "remove_fact":
                facts = t.get("known_facts", [])
                if idx is not None and 0 <= idx < len(facts):
                    removed = facts.pop(idx); t["known_facts"] = facts
                    changed = True; action_summary = f"Removed fact from {tid}: '{removed}'"
            elif op == "add_question":
                t.setdefault("open_questions", []).append({"question": val, "resolved": False})
                changed = True; action_summary = f"Added question to {tid}: '{val}'"
            elif op == "resolve_question":
                qs = t.get("open_questions", [])
                if idx is not None and 0 <= idx < len(qs):
                    qs[idx]["resolved"] = True
                    changed = True; action_summary = f"Resolved question #{idx} in {tid}"
            elif op == "unresolve_question":
                qs = t.get("open_questions", [])
                if idx is not None and 0 <= idx < len(qs):
                    qs[idx]["resolved"] = False
                    changed = True; action_summary = f"Re-opened question #{idx} in {tid}"
            elif op == "add_action":
                t.setdefault("action_items", []).append({"item": val, "owner": owner, "status": "open"})
                changed = True; action_summary = f"Added action to {tid}: '{val}' → {owner}"
            elif op == "complete_action":
                actions = t.get("action_items", [])
                if idx is not None and 0 <= idx < len(actions):
                    actions[idx]["status"] = "done"
                    changed = True; action_summary = f"Completed action #{idx} in {tid}"
            elif op == "reopen_action":
                actions = t.get("action_items", [])
                if idx is not None and 0 <= idx < len(actions):
                    actions[idx]["status"] = "open"
                    changed = True; action_summary = f"Re-opened action #{idx} in {tid}"
            elif op == "remove_action":
                actions = t.get("action_items", [])
                if idx is not None and 0 <= idx < len(actions):
                    removed = actions.pop(idx); t["action_items"] = actions
                    changed = True; action_summary = f"Removed action from {tid}: '{removed.get('item', '')}'"
            elif op == "add_decision":
                t.setdefault("decisions", []).append(val)
                changed = True; action_summary = f"Added decision to {tid}: '{val}'"
            elif op == "remove_decision":
                decisions = t.get("decisions", [])
                if idx is not None and 0 <= idx < len(decisions):
                    removed = decisions.pop(idx); t["decisions"] = decisions
                    changed = True; action_summary = f"Removed decision from {tid}: '{removed}'"
            elif op == "add_new_info":
                t.setdefault("new_information", []).append(val)
                changed = True; action_summary = f"Added new info to {tid}: '{val}'"
            elif op == "set_discussion_focus":
                t["discussion_focus"] = val
                changed = True; action_summary = f"Updated focus for {tid}: '{val}'"

            if changed:
                _save_thread(tid, t)
            return {"ok": True, "changed": changed, "action_summary": action_summary}

        else:
            return {"error": f"Unknown tool: {name}", "changed": False}

    except Exception as e:
        return {"error": str(e), "changed": False}


@app.route("/api/chat", methods=["POST"])
def api_chat():
    import anthropic as _ant
    d = request.get_json() or {}
    messages = d.get("messages", [])
    if not messages:
        return jsonify({"error": "no messages"}), 400

    # Build system prompt with live thread summaries
    cfg = _load_config()
    active_set = set(cfg.get("active_threads", []))
    thread_lines = []
    for tid in _list_thread_ids():
        try:
            t = _load_thread(tid)
            status = "ACTIVE" if tid in active_set else "inactive"
            focus = t.get("discussion_focus", t.get("stage", ""))
            open_q = sum(1 for q in t.get("open_questions", []) if not q.get("resolved"))
            open_a = sum(1 for a in t.get("action_items", []) if a.get("status") == "open")
            thread_lines.append(f"- {tid} [{status}]: {focus} | {open_q} open Qs, {open_a} open actions")
        except Exception:
            pass

    system = (
        "You are an AI assistant embedded in Cratus Asset Management's deal pipeline dashboard.\n"
        "You help the team understand and manage their real estate deal threads.\n\n"
        "CURRENT THREADS:\n" + ("\n".join(thread_lines) if thread_lines else "No threads yet.") + "\n\n"
        "You can answer questions about any thread and modify thread data using tools.\n"
        "When you make changes, briefly confirm what was done. Be concise and professional.\n"
        f"Today: {datetime.now().strftime('%Y-%m-%d')}"
    )

    tools = [
        {
            "name": "list_threads",
            "description": "Get a summary of all deal threads including status, open questions, and action counts.",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "get_thread",
            "description": "Get full details of a specific deal thread (facts, questions, actions, decisions, history).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string", "description": "The thread ID (e.g. washington_rv_park)"}
                },
                "required": ["thread_id"]
            }
        },
        {
            "name": "patch_thread",
            "description": (
                "Modify a deal thread. Operations: add_fact, remove_fact (needs index), "
                "add_question, resolve_question (needs index), unresolve_question (needs index), "
                "add_action (needs owner), complete_action (needs index), reopen_action (needs index), "
                "remove_action (needs index), add_decision, remove_decision (needs index), "
                "add_new_info, set_discussion_focus."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "string"},
                    "operation": {
                        "type": "string",
                        "enum": [
                            "add_fact", "remove_fact", "add_question", "resolve_question",
                            "unresolve_question", "add_action", "complete_action", "reopen_action",
                            "remove_action", "add_decision", "remove_decision", "add_new_info",
                            "set_discussion_focus"
                        ]
                    },
                    "value": {"type": "string", "description": "Text for add operations or set_discussion_focus"},
                    "index": {"type": "integer", "description": "Zero-based index for remove/resolve/complete operations"},
                    "owner": {"type": "string", "description": "Owner name for add_action (Li, Brian, or Joey Wan)"}
                },
                "required": ["thread_id", "operation"]
            }
        }
    ]

    try:
        client = _ant.Anthropic()
        loop_messages = list(messages)
        final_text = ""
        actions_taken = []

        for _ in range(6):  # max 6 tool-call rounds
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system,
                tools=tools,
                messages=loop_messages
            )

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        final_text = block.text
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                # Convert SDK content blocks to dicts for the message history
                asst_content = []
                for block in response.content:
                    if block.type == "tool_use":
                        asst_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                    elif hasattr(block, "text"):
                        asst_content.append({"type": "text", "text": block.text})
                loop_messages.append({"role": "assistant", "content": asst_content})

                for block in response.content:
                    if block.type == "tool_use":
                        result = _execute_chat_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })
                        if result.get("changed"):
                            actions_taken.append(result.get("action_summary", ""))
                loop_messages.append({"role": "user", "content": tool_results})
            else:
                for block in response.content:
                    if hasattr(block, "text"):
                        final_text = block.text
                break

        return jsonify({"reply": final_text, "actions_taken": actions_taken})

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "detail": traceback.format_exc()[-500:]}), 500


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cratus — Email Automation</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;color:#1a1a1a;background:#f5f5f3;display:flex;flex-direction:column}
button{font-family:inherit;cursor:pointer}
input,select,textarea{font-family:inherit;font-size:13px}

/* === Top bar === */
.topbar{height:48px;background:#fff;border-bottom:1px solid #e5e5e3;display:flex;align-items:center;padding:0 16px;gap:14px;flex-shrink:0;z-index:10}
.logo{font-weight:600;font-size:14px;letter-spacing:-.01em}
.tb-div{width:1px;height:18px;background:#e5e5e3}
.stat-pill{display:flex;align-items:center;gap:5px;font-size:12px;color:#555}
.stat-dot{width:6px;height:6px;border-radius:50%;background:#22c55e}
.stat-dot.off{background:#d1d5db}
.tb-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.saved-msg{font-size:12px;color:#22c55e;opacity:0;transition:opacity .3s}
.saved-msg.show{opacity:1}

/* === Workspace === */
.workspace{flex:1;display:flex;overflow:hidden}

/* === Left sidebar === */
.sidebar-left{width:248px;background:#fff;border-right:1px solid #e5e5e3;overflow-y:auto;flex-shrink:0;display:flex;flex-direction:column}
.sec-hdr{display:flex;justify-content:space-between;align-items:center;padding:11px 16px;cursor:pointer;border-bottom:1px solid #f0f0ee;user-select:none}
.sec-hdr:hover{background:#fafafa}
.sec-title{font-size:11px;font-weight:600;color:#999;letter-spacing:.06em;text-transform:uppercase}
.sec-chev{font-size:10px;color:#ccc;transition:transform .2s;margin-left:4px}
.sec-chev.open{transform:rotate(90deg)}
.sec-body{padding:14px 16px;border-bottom:1px solid #f0f0ee;display:none}
.sec-body.open{display:block}

/* Form elements */
.field-row{display:flex;align-items:center;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f8f8f6}
.field-row:last-child{border-bottom:none}
.field-lbl{font-size:12px;color:#555;flex-shrink:0}
.field-row input[type=text],.field-row input[type=number],.field-row input[type=time]{border:1px solid #e0e0de;border-radius:5px;padding:3px 6px;font-size:12px;color:#1a1a1a;background:#fff;outline:none;width:96px}
.toggle{position:relative;display:inline-block;width:30px;height:17px}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#ddd;border-radius:17px;transition:.2s;cursor:pointer}
.slider:before{content:'';position:absolute;width:11px;height:11px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
input:checked+.slider{background:#1a1a1a}
input:checked+.slider:before{transform:translateX(13px)}
.save-row{display:flex;justify-content:flex-end;margin-top:10px}
.btn{border:1px solid #ddd;border-radius:6px;padding:5px 12px;font-size:12px;background:#fff;color:#1a1a1a;transition:background .15s}
.btn:hover{background:#f5f5f3}
.btn-primary{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
.btn-primary:hover{background:#333}
.btn-sm{padding:3px 8px;font-size:11px;border-radius:5px}
.btn-danger{color:#dc2626;border-color:#fca5a5}

/* Agent rows */
.agent-row{padding:10px 0;border-bottom:1px solid #f0f0ee}
.agent-row:last-child{border-bottom:none}
.agent-name{font-weight:600;font-size:13px;margin-bottom:1px;display:flex;align-items:center;gap:6px}
.agent-email{font-size:11px;color:#aaa;margin-bottom:4px}
.agent-sub{font-size:11px;color:#888;margin-top:6px;margin-bottom:2px}

/* Auth badges */
.auth-badge{font-size:10px;padding:1px 5px;border-radius:8px;font-weight:500}
.auth-connected{background:#dcfce7;color:#166534}
.auth-expired{background:#fef3c7;color:#92400e}
.auth-unauth{background:#fee2e2;color:#991b1b}
.auth-pending{background:#dbeafe;color:#1e40af}

/* Thread mgmt in left sidebar */
.tmgmt-row{display:flex;align-items:center;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f8f8f6}
.tmgmt-row:last-child{border-bottom:none}
.tmgmt-name{font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;margin:0 6px}

/* Sched mini */
.sched-mini-row{display:flex;align-items:center;gap:5px;padding:2px 0}
.sched-dot-m{width:5px;height:5px;border-radius:50%;flex-shrink:0}

/* === Chat center === */
.chat-main{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#f5f5f3;min-width:0}
.chat-hdr{padding:10px 20px;font-size:12px;color:#aaa;border-bottom:1px solid #e5e5e3;background:#fff;flex-shrink:0}
.chat-messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
.msg-user{align-self:flex-end;background:#1a1a1a;color:#fff;padding:9px 14px;border-radius:16px 16px 4px 16px;max-width:72%;font-size:13px;line-height:1.5}
.msg-ai{align-self:flex-start;background:#fff;border:1px solid #e5e5e3;padding:12px 16px;border-radius:4px 16px 16px 16px;max-width:84%;font-size:13px;line-height:1.65;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.msg-ai b,.msg-ai strong{font-weight:600}
.msg-loading{align-self:flex-start;padding:10px 14px;color:#bbb;font-size:13px;display:flex;align-items:center;gap:4px}
.dot-pulse{display:inline-flex;gap:3px}
.dot-pulse span{width:5px;height:5px;border-radius:50%;background:#bbb;animation:dp 1.2s infinite}
.dot-pulse span:nth-child(2){animation-delay:.2s}
.dot-pulse span:nth-child(3){animation-delay:.4s}
@keyframes dp{0%,80%,100%{opacity:.2}40%{opacity:1}}
.chat-input-wrap{flex-shrink:0;padding:12px 16px;background:#fff;border-top:1px solid #e5e5e3}
.chat-input-row{display:flex;gap:8px;align-items:flex-end}
.chat-ta{flex:1;border:1px solid #ddd;border-radius:10px;padding:8px 12px;font-size:13px;line-height:1.5;resize:none;outline:none;max-height:120px;transition:border .15s;background:#fff}
.chat-ta:focus{border-color:#999}
.chat-send{width:34px;height:34px;border-radius:50%;background:#1a1a1a;color:#fff;border:none;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:15px;transition:background .15s}
.chat-send:hover{background:#333}
.chat-send:disabled{background:#ccc;cursor:not-allowed}

/* === Right sidebar === */
.sidebar-right{width:272px;background:#fff;border-left:1px solid #e5e5e3;flex-shrink:0;display:flex;flex-direction:column;overflow:hidden}
.right-threads{flex:1;overflow-y:auto}
.right-title{font-size:11px;font-weight:600;color:#999;letter-spacing:.06em;text-transform:uppercase;padding:12px 16px 6px}
.thread-card{padding:10px 14px;border-bottom:1px solid #f0f0ee;cursor:pointer;transition:background .1s;display:flex;align-items:flex-start;gap:8px}
.thread-card:hover{background:#fafafa}
.tc-dot{width:7px;height:7px;border-radius:50%;background:#d1d5db;flex-shrink:0;margin-top:4px}
.tc-dot.active{background:#22c55e}
.tc-name{font-weight:600;font-size:12px;color:#1a1a1a;line-height:1.3}
.tc-focus{font-size:11px;color:#888;margin-top:2px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tc-badges{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
.tc-badge{font-size:10px;padding:1px 6px;border-radius:10px;font-weight:500}
.tc-badge-q{background:#fef3c7;color:#92400e}
.tc-badge-a{background:#dbeafe;color:#1e40af}
.tc-badge-r{background:#f3f4f6;color:#6b7280}
.no-threads{padding:16px;font-size:12px;color:#aaa;text-align:center}

/* Run section */
.run-section{padding:12px 14px;border-top:1px solid #e5e5e3;flex-shrink:0}
.run-lbl{font-size:11px;font-weight:600;color:#999;letter-spacing:.06em;text-transform:uppercase;margin-bottom:7px}
.run-sel{width:100%;border:1px solid #ddd;border-radius:6px;padding:5px 7px;font-size:12px;margin-bottom:5px;background:#fff;outline:none}
.run-topic{width:100%;border:1px solid #ddd;border-radius:6px;padding:5px 7px;font-size:12px;margin-bottom:7px;background:#fff;outline:none}
.run-topic::placeholder{color:#bbb}
.btn-run{width:100%;padding:8px;font-size:13px;font-weight:600;background:#1a1a1a;color:#fff;border:none;border-radius:7px;transition:background .15s}
.btn-run:hover{background:#333}
.btn-run:disabled{background:#aaa;cursor:not-allowed}
.run-ind{font-size:11px;color:#666;margin-top:5px;display:none;text-align:center}
.run-ind.show{display:block}

/* === Overlay panels === */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:200;display:none;align-items:flex-start;justify-content:flex-end}
.overlay.open{display:flex}
.panel{background:#fff;width:560px;height:100vh;overflow-y:auto;padding:24px;box-shadow:-4px 0 24px rgba(0,0,0,.08)}
.panel-close{float:right;font-size:20px;border:none;background:none;color:#888;cursor:pointer;padding:0 4px}
.panel-title{font-size:17px;font-weight:600;margin-bottom:4px}
.panel-sub{font-size:12px;color:#888;margin-bottom:16px}

/* Detail panel internals */
.dp-section{margin-bottom:18px}
.dp-sec-title{font-size:11px;font-weight:600;color:#999;letter-spacing:.06em;text-transform:uppercase;margin-bottom:7px}
.focus-box{background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:10px 12px;font-size:13px;color:#1e3a5f;margin-bottom:12px;line-height:1.5}
.edit-row{display:flex;align-items:flex-start;gap:8px;padding:6px 8px;border-radius:6px;background:#f9f9f7;margin-bottom:4px}
.edit-row:hover{background:#f0f0ee}
.edit-text{flex:1;font-size:13px;color:#333;line-height:1.5;min-width:0;word-break:break-word}
.item-done .edit-text{text-decoration:line-through;color:#aaa}
.icon-btn{border:none;background:none;cursor:pointer;font-size:14px;color:#bbb;padding:0 2px;line-height:1;flex-shrink:0;margin-top:1px}
.icon-btn:hover{color:#555}
.icon-btn-g:hover{color:#16a34a}
.icon-btn-b:hover{color:#2563eb}
.dp-divider{height:1px;background:#f0f0ee;margin:14px 0}
.new-info-area{width:100%;border:1px solid #ddd;border-radius:6px;padding:8px 10px;font-size:13px;resize:vertical;min-height:70px;outline:none}
.add-row{display:flex;gap:6px;margin-top:7px}
.add-input{flex:1;border:1px solid #ddd;border-radius:6px;padding:5px 8px;font-size:12px;outline:none}

/* History entry */
.hist-entry{padding:8px 10px;border-radius:6px;border:1px solid #e5e5e3;margin-bottom:6px}
.hist-entry.discarded{background:#f9f9f7;border-color:#f0f0ee;opacity:.55}
</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <span class="logo">Cratus</span>
  <div class="tb-div"></div>
  <div class="stat-pill">
    <span class="stat-dot" id="schedDot"></span>
    <span id="schedLabel">Loading...</span>
  </div>
  <div class="tb-div"></div>
  <div class="stat-pill">📅 <span id="statToday">—</span> today</div>
  <div class="stat-pill">⏱ Next: <span id="statNext">—</span></div>
  <div class="tb-right">
    <span class="saved-msg" id="savedMsg">Saved</span>
  </div>
</div>

<!-- Workspace -->
<div class="workspace">

<!-- Left sidebar -->
<div class="sidebar-left">

  <div>
    <div class="sec-hdr" onclick="toggleSec('Schedule')">
      <span class="sec-title">Schedule & Flow</span>
      <span class="sec-chev" id="chevSchedule">▶</span>
    </div>
    <div class="sec-body" id="bodySchedule">
      <div class="field-row">
        <span class="field-lbl">Enabled</span>
        <label class="toggle"><input type="checkbox" id="cfgEnabled"><span class="slider"></span></label>
      </div>
      <div class="field-row">
        <span class="field-lbl">Subject</span>
        <input type="text" id="cfgSubject" style="width:128px">
      </div>
      <div class="field-row">
        <span class="field-lbl">Window (ET)</span>
        <div style="display:flex;gap:2px;align-items:center">
          <input type="time" id="cfgWinStart" style="width:74px">
          <span style="color:#bbb;font-size:11px;padding:0 2px">–</span>
          <input type="time" id="cfgWinEnd" style="width:74px">
        </div>
      </div>
      <div class="field-row">
        <span class="field-lbl">Runs/day</span>
        <div style="display:flex;gap:2px;align-items:center">
          <input type="number" id="cfgRunsMin" min="1" max="10" style="width:40px">
          <span style="color:#bbb;font-size:11px;padding:0 2px">–</span>
          <input type="number" id="cfgRunsMax" min="1" max="10" style="width:40px">
        </div>
      </div>
      <div class="field-row">
        <span class="field-lbl">Min gap (min)</span>
        <input type="number" id="cfgGap" min="10" max="480" style="width:56px">
      </div>
      <div class="field-row">
        <span class="field-lbl">Rounds</span>
        <div style="display:flex;gap:2px;align-items:center">
          <input type="number" id="cfgRoundsMin" min="1" max="20" style="width:40px">
          <span style="color:#bbb;font-size:11px;padding:0 2px">–</span>
          <input type="number" id="cfgRoundsMax" min="1" max="20" style="width:40px">
        </div>
      </div>
      <div class="save-row"><button class="btn btn-primary btn-sm" onclick="saveConfig()">Save</button></div>
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid #f0f0ee">
        <div style="font-size:11px;color:#aaa;margin-bottom:5px">Today's slots</div>
        <div id="schedMini"><span style="font-size:11px;color:#bbb">Loading...</span></div>
      </div>
      <div style="margin-top:10px;padding-top:10px;border-top:1px solid #f0f0ee">
        <div style="font-size:11px;color:#aaa;margin-bottom:5px">Recent runs</div>
        <div id="runLogMini"><span style="font-size:11px;color:#bbb">Loading...</span></div>
      </div>
    </div>
  </div>

  <div>
    <div class="sec-hdr" onclick="toggleSec('Agents')">
      <span class="sec-title">Agents</span>
      <span class="sec-chev" id="chevAgents">▶</span>
    </div>
    <div class="sec-body" id="bodyAgents">
      <div id="agentsList"><span style="color:#bbb;font-size:12px">Loading...</span></div>
    </div>
  </div>

  <div>
    <div class="sec-hdr" onclick="toggleSec('Threads')">
      <span class="sec-title">Threads</span>
      <span class="sec-chev" id="chevThreads">▶</span>
    </div>
    <div class="sec-body" id="bodyThreads">
      <button class="btn btn-primary btn-sm" onclick="openNewThread()" style="width:100%;margin-bottom:10px">+ New thread</button>
      <div id="threadsMgmt"><span style="color:#bbb;font-size:12px">Loading...</span></div>
    </div>
  </div>

</div>

<!-- Chat center -->
<div class="chat-main">
  <div class="chat-hdr">
    Ask anything about your deals, or tell me what to change — I can read and update all thread data.
  </div>
  <div class="chat-messages" id="chatMessages"></div>
  <div class="chat-input-wrap">
    <div class="chat-input-row">
      <textarea class="chat-ta" id="chatInput" rows="1"
        placeholder="What are the open questions on Washington RV Park? / Add a fact to [thread]: …"
        onkeydown="chatKeydown(event)" oninput="autoResize(this)"></textarea>
      <button class="chat-send" id="chatSendBtn" onclick="sendChat()">↑</button>
    </div>
  </div>
</div>

<!-- Right sidebar -->
<div class="sidebar-right">
  <div class="right-threads">
    <div class="right-title">Deal threads</div>
    <div id="threadCards"><div class="no-threads">Loading...</div></div>
  </div>
  <div class="run-section">
    <div class="run-lbl">Run now</div>
    <select class="run-sel" id="runThread"><option value="">— select thread —</option></select>
    <input class="run-topic" type="text" id="runTopic" placeholder="Topic override (optional)">
    <button class="btn-run" id="runBtn" onclick="triggerRun()">▶ Run now</button>
    <div class="run-ind" id="runInd">⏳ Running...</div>
  </div>
</div>

</div>

<!-- Auth overlay -->
<div class="overlay" id="authOverlay" onclick="closeAuthOverlay(event)">
  <div class="panel" style="width:440px;height:auto;min-height:280px">
    <button class="panel-close" onclick="closeAuth()">✕</button>
    <h2 class="panel-title" id="authTitle">Authorize Outlook</h2>
    <p class="panel-sub">Sign in to Microsoft so this bot can send and receive emails.</p>
    <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:16px;margin-bottom:12px">
      <p style="font-size:13px;font-weight:600;margin-bottom:6px">Step 1</p>
      <p style="font-size:13px;margin-bottom:12px">Open <a href="https://microsoft.com/devicelogin" target="_blank" style="color:#2563eb;font-weight:600">microsoft.com/devicelogin</a></p>
      <p style="font-size:13px;font-weight:600;margin-bottom:4px">Step 2 — Enter this code:</p>
      <div style="font-size:26px;font-weight:700;letter-spacing:4px;color:#1a1a1a;font-family:monospace;margin:8px 0;padding:12px;background:#fff;border:2px solid #e5e5e3;border-radius:6px;text-align:center" id="authCode">—</div>
    </div>
    <div style="font-size:13px;color:#555;margin-bottom:4px" id="authStatusMsg">⏳ Starting...</div>
    <div style="font-size:11px;color:#aaa" id="authExpiry"></div>
  </div>
</div>

<!-- New thread overlay -->
<div class="overlay" id="newThreadOverlay" onclick="closeNewThreadOverlay(event)">
  <div class="panel" id="newThreadPanel">
    <button class="panel-close" onclick="closeNewThread()">✕</button>
    <h2 class="panel-title">New deal thread</h2>
    <p class="panel-sub">Paste any listing info — Claude will auto-structure it.</p>
    <div class="field-row" style="margin-bottom:10px;border:none">
      <span class="field-lbl">Thread name</span>
      <input type="text" id="newThreadName" placeholder="e.g. Sequim RV Park" style="width:240px">
    </div>
    <textarea id="newThreadContent" style="width:100%;min-height:260px;border:1px solid #ddd;border-radius:6px;padding:10px 12px;font-size:13px;line-height:1.6;resize:vertical;outline:none" placeholder="Paste listing content, broker notes, financials..."></textarea>
    <div id="newThreadStatus" style="font-size:12px;color:#888;margin-top:8px;min-height:18px"></div>
    <div style="text-align:right;margin-top:10px">
      <button class="btn btn-primary" id="newThreadBtn" onclick="createThread()">Create thread</button>
    </div>
  </div>
</div>

<!-- Prompt editor overlay -->
<div class="overlay" id="promptOverlay" onclick="closePromptOverlay(event)">
  <div class="panel" id="promptPanel">
    <button class="panel-close" onclick="closePromptPanel()">✕</button>
    <div id="promptContent"></div>
  </div>
</div>

<!-- Thread detail overlay -->
<div class="overlay" id="detailOverlay" onclick="closeDetailOverlay(event)">
  <div class="panel" id="detailPanel">
    <button class="panel-close" onclick="closeDetailPanel()">✕</button>
    <div id="detailContent"></div>
  </div>
</div>

<script>
// State
let _chatHistory = [];
let _chatPending = false;
let _runPolling  = null;
let _td = null, _tdId = null;
let _editingBotId = null;
let _authBotId = null, _authPolling = null;

// Boot
async function boot() {
  await Promise.all([loadConfig(), loadThreadCards(), loadSchedMini(), loadRunLogMini(), loadBots()]);
  await loadAuthStatus();
  pollRunStatus();
  appendAiMsg("Hi! I can answer questions about your deal threads and make changes for you.\n\nTry:\n• \"What are the open questions on Washington RV Park?\"\n• \"Add a fact to [thread]: the NOI is $180k\"\n• \"Mark action 2 as done on [thread]\"");
  setInterval(() => { loadSchedMini(); loadRunLogMini(); loadThreadCards(); pollRunStatus(); loadAuthStatus(); }, 30000);
}

// Config
async function loadConfig() {
  const d = await fetch('/api/config').then(r => r.json());
  document.getElementById('cfgEnabled').checked = d.schedule_enabled;
  document.getElementById('cfgSubject').value = d.subject;
  document.getElementById('cfgWinStart').value = d.window_start;
  document.getElementById('cfgWinEnd').value = d.window_end;
  document.getElementById('cfgRunsMin').value = d.runs_min;
  document.getElementById('cfgRunsMax').value = d.runs_max;
  document.getElementById('cfgGap').value = d.min_gap;
  document.getElementById('cfgRoundsMin').value = d.rounds_min;
  document.getElementById('cfgRoundsMax').value = d.rounds_max;
  updateTopbar(d.schedule_enabled);
}
async function saveConfig() {
  const p = {
    schedule_enabled: document.getElementById('cfgEnabled').checked,
    subject: document.getElementById('cfgSubject').value,
    window_start: document.getElementById('cfgWinStart').value,
    window_end: document.getElementById('cfgWinEnd').value,
    runs_min: parseInt(document.getElementById('cfgRunsMin').value),
    runs_max: parseInt(document.getElementById('cfgRunsMax').value),
    min_gap: parseInt(document.getElementById('cfgGap').value),
    rounds_min: parseInt(document.getElementById('cfgRoundsMin').value),
    rounds_max: parseInt(document.getElementById('cfgRoundsMax').value),
  };
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(p)});
  showSaved('Saved'); updateTopbar(p.schedule_enabled); loadSchedMini();
}
function updateTopbar(enabled) {
  document.getElementById('schedDot').className = 'stat-dot' + (enabled ? '' : ' off');
  document.getElementById('schedLabel').textContent = enabled ? 'Scheduler ON' : 'Scheduler OFF';
}

// Schedule mini
async function loadSchedMini() {
  const slots = await fetch('/api/schedule/today').then(r => r.json());
  const el = document.getElementById('schedMini');
  document.getElementById('statToday').textContent = slots.length || '—';
  const next = slots.find(s => !s.past);
  document.getElementById('statNext').textContent = next ? next.time : (slots.length ? 'Done' : '—');
  if (!slots.length) { el.innerHTML = '<span style="font-size:11px;color:#bbb">None today</span>'; return; }
  el.innerHTML = slots.map(s => `<div class="sched-mini-row">
    <span class="sched-dot-m" style="background:${s.past?'#d1d5db':'#22c55e'}"></span>
    <span style="font-size:11px;${s.past?'color:#bbb;text-decoration:line-through':'color:#444'}">${s.time}</span>
  </div>`).join('');
}

// Run log mini
async function loadRunLogMini() {
  const log = await fetch('/api/runlog').then(r => r.json());
  const el = document.getElementById('runLogMini');
  if (!log.length) { el.innerHTML = '<span style="font-size:11px;color:#bbb">No runs yet</span>'; return; }
  el.innerHTML = log.slice(0,4).map(r => {
    const dt = r.started_at ? new Date(r.started_at).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '—';
    const col = r.status==='completed'?'#16a34a':r.status==='error'?'#dc2626':'#aaa';
    return `<div style="display:flex;justify-content:space-between;padding:2px 0;font-size:11px"><span style="color:#555">${dt}</span><span style="color:${col};font-weight:500">${r.status}</span></div>`;
  }).join('');
}

// Thread cards (right sidebar)
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

async function loadThreadCards() {
  const threads = await fetch('/api/threads').then(r => r.json());
  const el = document.getElementById('threadCards');
  if (!threads.length) {
    el.innerHTML = '<div class="no-threads">No threads yet.<br><button class="btn btn-primary btn-sm" onclick="openNewThread()" style="margin-top:8px">+ New thread</button></div>';
  } else {
    el.innerHTML = threads.map(t => `
      <div class="thread-card" onclick="openDetail('${t.id}')">
        <span class="tc-dot ${t.is_active?'active':''}"></span>
        <div style="min-width:0;flex:1">
          <div class="tc-name">${esc(t.name)}</div>
          <div class="tc-focus">${esc(t.discussion_focus || t.stage || '—')}</div>
          <div class="tc-badges">
            <span class="tc-badge tc-badge-r">Run ${t.run_count}</span>
            ${t.open_questions?`<span class="tc-badge tc-badge-q">${t.open_questions} Q</span>`:''}
            ${t.open_actions?`<span class="tc-badge tc-badge-a">${t.open_actions} ✓</span>`:''}
          </div>
        </div>
      </div>`).join('');
  }
  const sel = document.getElementById('runThread');
  const prev = sel.value;
  const active = threads.filter(t => t.is_active);
  sel.innerHTML = '<option value="">— select thread —</option>' +
    active.map(t => `<option value="${t.id}" ${t.id===prev?'selected':''}>${esc(t.name)}</option>`).join('');
  if (!sel.value && active.length) sel.value = active[0].id;
  const mgmt = document.getElementById('threadsMgmt');
  mgmt.innerHTML = threads.length ? threads.map(t => `
    <div class="tmgmt-row">
      <label class="toggle" style="flex-shrink:0">
        <input type="checkbox" ${t.is_active?'checked':''} onchange="toggleActive('${t.id}',this.checked)">
        <span class="slider"></span>
      </label>
      <span class="tmgmt-name">${esc(t.name)}</span>
      <button class="btn btn-sm" onclick="openDetail('${t.id}')" style="flex-shrink:0">View</button>
    </div>`).join('') : '<span style="color:#bbb;font-size:12px">No threads</span>';
}

// Sidebar toggle
function toggleSec(name) {
  document.getElementById('body'+name).classList.toggle('open');
  document.getElementById('chev'+name).classList.toggle('open');
}

// Chat
function fmtAi(text){return esc(text).replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>').replace(/\n/g,'<br>')}
function appendUserMsg(text){const el=document.createElement('div');el.className='msg-user';el.textContent=text;document.getElementById('chatMessages').appendChild(el);scrollChat()}
function appendAiMsg(text){const el=document.createElement('div');el.className='msg-ai';el.innerHTML=fmtAi(text);document.getElementById('chatMessages').appendChild(el);scrollChat();return el}
function showChatLoading(){const el=document.createElement('div');el.className='msg-loading';el.id='chatLoading';el.innerHTML='<div class="dot-pulse"><span></span><span></span><span></span></div>';document.getElementById('chatMessages').appendChild(el);scrollChat()}
function removeChatLoading(){const el=document.getElementById('chatLoading');if(el)el.remove()}
function scrollChat(){const el=document.getElementById('chatMessages');el.scrollTop=el.scrollHeight}
function autoResize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}
function chatKeydown(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || _chatPending) return;
  input.value=''; input.style.height='auto';
  _chatPending=true; document.getElementById('chatSendBtn').disabled=true;
  appendUserMsg(text);
  _chatHistory.push({role:'user',content:text});
  showChatLoading();
  try {
    const res = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:_chatHistory})});
    const data = await res.json();
    removeChatLoading();
    if (!res.ok) {
      appendAiMsg('❌ '+(data.error||'Something went wrong.'));
    } else {
      const reply = data.reply || '(no response)';
      appendAiMsg(reply);
      _chatHistory.push({role:'assistant',content:reply});
      if(_chatHistory.length>40)_chatHistory=_chatHistory.slice(-40);
      if (data.actions_taken && data.actions_taken.length) {
        loadThreadCards();
        if (_tdId) { _td=await fetch('/api/threads/'+_tdId).then(r=>r.json()); renderDetail(); }
      }
    }
  } catch(e) { removeChatLoading(); appendAiMsg('❌ Network error.'); }
  _chatPending=false; document.getElementById('chatSendBtn').disabled=false;
  document.getElementById('chatInput').focus();
}

// Run now
async function triggerRun() {
  const tid = document.getElementById('runThread').value;
  const topic = document.getElementById('runTopic').value.trim();
  if (!tid) { alert('Please select a thread.'); return; }
  const res = await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({thread_id:tid,topic:topic||null})});
  if (!res.ok) { alert((await res.json()).error); return; }
  document.getElementById('runBtn').disabled=true;
  document.getElementById('runInd').classList.add('show');
  startRunPoll();
}
function startRunPoll(){if(_runPolling)clearInterval(_runPolling);_runPolling=setInterval(pollRunStatus,3000)}
async function pollRunStatus() {
  const s = await fetch('/api/run/status').then(r=>r.json());
  const btn=document.getElementById('runBtn'), ind=document.getElementById('runInd');
  if (s.running) { btn.disabled=true; ind.classList.add('show'); }
  else { btn.disabled=false; ind.classList.remove('show'); if(_runPolling){clearInterval(_runPolling);_runPolling=null;} loadRunLogMini(); loadThreadCards(); }
}

// Bots
async function loadBots() {
  const bots = await fetch('/api/bots').then(r=>r.json());
  const el = document.getElementById('agentsList');
  if (!bots.length) { el.innerHTML='<span style="color:#bbb;font-size:12px">No agents</span>'; return; }
  el.innerHTML = bots.map(b=>`
    <div class="agent-row" id="botPanel_${b.id}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div class="agent-name">${esc(b.name)} <span class="auth-badge auth-unauth" id="authBadge_${b.id}">—</span></div>
          <div class="agent-email">${esc(b.email)}</div>
        </div>
        <button class="btn btn-sm" id="authBtn_${b.id}" onclick="startAuth('${b.id}','${esc(b.name)}')" style="font-size:10px">Auth</button>
      </div>
      <div class="agent-sub">Email</div>
      <input type="text" id="email_${b.id}" value="${esc(b.email)}" style="width:100%;border:1px solid #e0;border-radius:5px;padding:3px 6px;font-size:12px;outline:none">
      <div class="agent-sub">Role</div>
      <input type="text" id="role_${b.id}" value="${esc(b.role)}" style="width:100%;border:1px solid #e0;border-radius:5px;padding:3px 6px;font-size:12px;outline:none">
      <div class="agent-sub">Style</div>
      <input type="text" id="style_${b.id}" value="${esc(b.style)}" style="width:100%;border:1px solid #e0;border-radius:5px;padding:3px 6px;font-size:12px;outline:none">
      <div style="display:flex;justify-content:space-between;margin-top:8px">
        <button class="btn btn-sm" onclick="openPromptEditor('${b.id}','${esc(b.name)}')" style="font-size:10px">Edit prompt</button>
        <button class="btn btn-primary btn-sm" onclick="saveBot('${b.id}')" style="font-size:10px">Save</button>
      </div>
    </div>`).join('');
}
async function saveBot(botId){const email=document.getElementById('email_'+botId).value;const role=document.getElementById('role_'+botId).value;const style=document.getElementById('style_'+botId).value;await fetch('/api/bots/'+botId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,role,style})});showSaved('Saved')}
async function openPromptEditor(botId,botName){_editingBotId=botId;const {content}=await fetch('/api/prompts/'+botId).then(r=>r.json());document.getElementById('promptContent').innerHTML=`<h2 class="panel-title">${esc(botName)} — Prompt</h2><p class="panel-sub">Full prompt template.</p><textarea id="promptTextarea" style="width:100%;min-height:420px;border:1px solid #ddd;border-radius:6px;padding:10px;font-size:12px;font-family:monospace;line-height:1.6;resize:vertical;outline:none">${esc(content)}</textarea><div style="text-align:right;margin-top:10px"><button class="btn btn-primary" onclick="savePrompt()">Save prompt</button></div>`;document.getElementById('promptOverlay').classList.add('open')}
async function savePrompt(){const c=document.getElementById('promptTextarea').value;await fetch('/api/prompts/'+_editingBotId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c})});showSaved('Prompt saved')}
function closePromptPanel(){document.getElementById('promptOverlay').classList.remove('open')}
function closePromptOverlay(e){if(e.target===document.getElementById('promptOverlay'))closePromptPanel()}

// Thread detail panel
async function openDetail(tid) {
  _tdId=tid; _td=await fetch('/api/threads/'+tid).then(r=>r.json());
  renderDetail(); document.getElementById('detailOverlay').classList.add('open');
}

function renderDetail() {
  const t=_td, tid=_tdId, el=document.getElementById('detailContent');
  const factsHtml=(t.known_facts||[]).length
    ?(t.known_facts).map((f,i)=>`<div class="edit-row"><span class="edit-text">${esc(f)}</span><button class="icon-btn" onclick="deleteFact(${i})">×</button></div>`).join('')
    :'<p style="color:#aaa;font-size:13px">None</p>';
  const qHtml=(t.open_questions||[]).length
    ?(t.open_questions).map((q,i)=>`<div class="edit-row ${q.resolved?'item-done':''}"><span class="edit-text">${esc(q.question)}</span><div style="display:flex;gap:3px;flex-shrink:0"><button class="icon-btn icon-btn-g" onclick="resolveQ(${i})" title="${q.resolved?'Unresolve':'Resolve'}">✓</button><button class="icon-btn" onclick="deleteQ(${i})">×</button></div></div>`).join('')
    :'<p style="color:#aaa;font-size:13px">None</p>';
  const aHtml=(t.action_items||[]).length
    ?(t.action_items).map((a,i)=>`<div class="edit-row ${a.status==='done'?'item-done':''}"><div style="flex:1;min-width:0"><div class="edit-text">${esc(a.item)}</div><div style="font-size:11px;color:#16a34a;margin-top:2px">→ ${esc(a.owner||'')}</div></div><div style="display:flex;gap:3px;flex-shrink:0;align-items:flex-start"><button class="icon-btn icon-btn-b" onclick="toggleAction(${i})">${a.status==='done'?'↺':'✓'}</button><button class="icon-btn" onclick="deleteAction(${i})">×</button></div></div>`).join('')
    :'<p style="color:#aaa;font-size:13px">None</p>';
  const dHtml=(t.decisions||[]).length
    ?(t.decisions).map((d,i)=>`<div class="edit-row"><span class="edit-text">${esc(d)}</span><button class="icon-btn" onclick="deleteDecision(${i})">×</button></div>`).join('')
    :'<p style="color:#aaa;font-size:13px">None</p>';
  const niHtml=(t.new_information||[]).length
    ?(t.new_information).map((n,i)=>`<div class="edit-row"><span class="edit-text" style="color:#555">${esc(n)}</span><button class="icon-btn" onclick="deleteNewInfo(${i})">×</button></div>`).join(''):'';
  const history=(t.history||[]);
  const hasBackup=!!t._rollback_backup;
  const histRows=history.map((h,i)=>{
    const disc=!!h.discarded;
    const ctrl=disc?'<span style="font-size:11px;color:#bbb">已弃用</span>':`<button class="btn btn-sm btn-danger" style="font-size:10px" onclick="rollback('${tid}',${i})">↺ 重新讨论</button>`;
    return `<div class="hist-entry ${disc?'discarded':''}"><div style="display:flex;justify-content:space-between;align-items:flex-start"><div style="flex:1;min-width:0"><span style="font-size:12px;font-weight:600;color:${disc?'#aaa':'#333'}">Run ${h.run} · ${h.date}</span>${h.topic?`<span style="font-size:11px;color:#2563eb;margin-left:6px">${esc(h.topic)}</span>`:''} ${h.summary?`<div style="font-size:12px;color:#555;margin-top:3px">${esc(h.summary)}</div>`:''}</div><div style="flex-shrink:0;margin-left:8px">${ctrl}</div></div></div>`;
  }).join('');
  el.innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px">
      <h2 class="panel-title">${esc((t.thread_id||tid).replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase()))}</h2>
      <button class="btn btn-sm btn-danger" onclick="deleteThread('${tid}')">Delete</button>
    </div>
    <p style="font-size:12px;color:#888;margin-bottom:14px">${esc(t.stage||'')} · Run ${history.filter(h=>!h.discarded).length}</p>
    ${t.discussion_focus?`<div class="focus-box">Focus: ${esc(t.discussion_focus)}</div>`:''}
    ${t.last_run_summary?`<p style="font-size:13px;color:#555;margin-bottom:14px;line-height:1.6"><em>${esc(t.last_run_summary)}</em></p>`:''}
    <div class="dp-section"><div class="dp-sec-title">Open questions</div>${qHtml}</div>
    <div class="dp-section"><div class="dp-sec-title">Action items</div>${aHtml}</div>
    <div class="dp-section"><div class="dp-sec-title">Decisions</div>${dHtml}</div>
    <div class="dp-divider"></div>
    <div class="dp-section">
      <div class="dp-sec-title">Known facts</div>${factsHtml}
      <div class="add-row">
        <input class="add-input" type="text" id="newFactInput" placeholder="Add a fact...">
        <button class="btn btn-primary btn-sm" onclick="addFact()">Add</button>
      </div>
    </div>
    ${niHtml?`<div class="dp-section"><div class="dp-sec-title">New info queued</div>${niHtml}</div>`:''}
    ${history.length?`<div class="dp-divider"></div><div class="dp-section"><div class="dp-sec-title">Run history</div><p style="font-size:11px;color:#aaa;margin-bottom:8px">↺ = 重新讨论 (回到该 run 之前). <strong>已发送邮件不受影响。</strong></p>${histRows}${hasBackup?`<div style="text-align:right;margin-top:6px"><button class="btn btn-sm" onclick="restoreThread('${tid}')">↩ 撤销回滚</button></div>`:''}</div>`:''}
    <div class="dp-divider"></div>
    <div class="dp-section">
      <div class="dp-sec-title">Add new information</div>
      <textarea class="new-info-area" id="newInfoText" placeholder="Paste update here..."></textarea>
      <div style="margin-top:8px;text-align:right"><button class="btn btn-primary" onclick="submitNewInfo()">Add to thread</button></div>
    </div>`;
}

async function _patch(){await fetch('/api/threads/'+_tdId,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({known_facts:_td.known_facts||[],open_questions:_td.open_questions||[],action_items:_td.action_items||[],decisions:_td.decisions||[],new_information:_td.new_information||[]})});showSaved('Saved')}
function deleteFact(i){_td.known_facts.splice(i,1);_patch();renderDetail()}
function addFact(){const inp=document.getElementById('newFactInput');const v=inp.value.trim();if(!v)return;(_td.known_facts=_td.known_facts||[]).push(v);_patch();renderDetail()}
function resolveQ(i){_td.open_questions[i].resolved=!_td.open_questions[i].resolved;_patch();renderDetail()}
function deleteQ(i){_td.open_questions.splice(i,1);_patch();renderDetail()}
function toggleAction(i){_td.action_items[i].status=_td.action_items[i].status==='done'?'open':'done';_patch();renderDetail()}
function deleteAction(i){_td.action_items.splice(i,1);_patch();renderDetail()}
function deleteDecision(i){_td.decisions.splice(i,1);_patch();renderDetail()}
function deleteNewInfo(i){_td.new_information.splice(i,1);_patch();renderDetail()}
async function submitNewInfo(){const v=document.getElementById('newInfoText').value.trim();if(!v)return;(_td.new_information=_td.new_information||[]).push(v);await _patch();renderDetail()}
function closeDetailPanel(){document.getElementById('detailOverlay').classList.remove('open')}
function closeDetailOverlay(e){if(e.target===document.getElementById('detailOverlay'))closeDetailPanel()}

async function rollback(tid,index){
  if(!confirm('将 Run '+(index+1)+' 及之后标记为"已弃用"，回到该 run 之前状态。\n⚠️ 已发送邮件不受影响。\n确认？'))return;
  const res=await fetch('/api/threads/'+tid+'/rollback/'+index,{method:'POST'});
  const d=await res.json();if(!res.ok){alert(d.error||'Failed');return;}
  _td=await fetch('/api/threads/'+tid).then(r=>r.json());renderDetail();showSaved('已回滚');
}
async function restoreThread(tid){
  if(!confirm('撤销回滚，恢复记录并还原 thread 状态？'))return;
  const res=await fetch('/api/threads/'+tid+'/restore',{method:'POST'});
  const d=await res.json();if(!res.ok){alert(d.error||'Failed');return;}
  _td=await fetch('/api/threads/'+tid).then(r=>r.json());renderDetail();
}

// Active toggle
async function toggleActive(tid,checked){const threads=await fetch('/api/threads').then(r=>r.json());let active=threads.filter(t=>t.is_active).map(t=>t.id);if(checked&&!active.includes(tid))active.push(tid);if(!checked)active=active.filter(id=>id!==tid);await fetch('/api/threads/active',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active_threads:active})});loadThreadCards()}
async function deleteThread(tid){if(!confirm('Delete this thread?'))return;await fetch('/api/threads/'+tid,{method:'DELETE'});closeDetailPanel();loadThreadCards()}

// New thread
function openNewThread(){document.getElementById('newThreadName').value='';document.getElementById('newThreadContent').value='';document.getElementById('newThreadStatus').textContent='';document.getElementById('newThreadBtn').disabled=false;document.getElementById('newThreadOverlay').classList.add('open')}
function closeNewThread(){document.getElementById('newThreadOverlay').classList.remove('open')}
function closeNewThreadOverlay(e){if(e.target===document.getElementById('newThreadOverlay'))closeNewThread()}
async function createThread(){
  const name=document.getElementById('newThreadName').value.trim();const content=document.getElementById('newThreadContent').value.trim();
  if(!name||!content){alert('Fill in both fields.');return;}
  const btn=document.getElementById('newThreadBtn'),st=document.getElementById('newThreadStatus');
  btn.disabled=true;st.textContent='⏳ Analyzing...';
  const res=await fetch('/api/threads/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,content})});
  const data=await res.json();if(!res.ok){st.textContent='❌ '+(data.error||'Failed');btn.disabled=false;return;}
  st.textContent='✓ Created!';setTimeout(()=>{closeNewThread();loadThreadCards();},800);
}

// Auth
async function loadAuthStatus(){for(const id of['bot_a','bot_b','bot_c']){try{const d=await fetch('/api/auth/'+id+'/status').then(r=>r.json());const badge=document.getElementById('authBadge_'+id);const btn=document.getElementById('authBtn_'+id);if(!badge)continue;const ts=d.token_status,fs=d.flow_status;if(fs==='polling'){badge.textContent='⏳';badge.className='auth-badge auth-pending';if(btn)btn.style.display='none';}else if(ts==='connected'){badge.textContent='✓';badge.className='auth-badge auth-connected';if(btn)btn.style.display='none';}else if(ts==='expired'){badge.textContent='⚠';badge.className='auth-badge auth-expired';if(btn){btn.style.display='';btn.textContent='Re-auth';}}else{badge.textContent='✗';badge.className='auth-badge auth-unauth';if(btn){btn.style.display='';btn.textContent='Auth';}}}catch(_){}}}
async function startAuth(botId,botName){_authBotId=botId;_clearAuthPoll();document.getElementById('authTitle').textContent='Authorize '+botName;document.getElementById('authCode').textContent='...';document.getElementById('authStatusMsg').textContent='⏳ Starting...';document.getElementById('authExpiry').textContent='';document.getElementById('authOverlay').classList.add('open');try{const res=await fetch('/api/auth/'+botId+'/start',{method:'POST'});const d=await res.json();if(!res.ok){document.getElementById('authStatusMsg').textContent='❌ '+(d.error||'Failed');return;}document.getElementById('authCode').textContent=d.user_code;document.getElementById('authStatusMsg').textContent='⏳ Waiting for sign-in...';if(d.expires_in){const exp=new Date(Date.now()+d.expires_in*1000);document.getElementById('authExpiry').textContent='Expires '+exp.toLocaleTimeString();}_authPolling=setInterval(()=>_pollAuth(botId),4000);}catch(e){document.getElementById('authStatusMsg').textContent='❌ Network error.';}}
async function _pollAuth(botId){try{const d=await fetch('/api/auth/'+botId+'/status').then(r=>r.json());if(d.flow_status==='connected'||d.token_status==='connected'){_clearAuthPoll();document.getElementById('authStatusMsg').textContent='✅ Authorized!';document.getElementById('authCode').textContent='✓';setTimeout(()=>{closeAuth();loadAuthStatus();},1500);}else if(d.flow_status==='failed'){_clearAuthPoll();document.getElementById('authStatusMsg').textContent='❌ Failed or timed out.';}}catch(_){}}
function _clearAuthPoll(){if(_authPolling){clearInterval(_authPolling);_authPolling=null;}}
function closeAuth(){_clearAuthPoll();document.getElementById('authOverlay').classList.remove('open');}
function closeAuthOverlay(e){if(e.target===document.getElementById('authOverlay'))closeAuth();}

// Utils
function showSaved(msg){const el=document.getElementById('savedMsg');el.textContent=msg||'Saved';el.classList.add('show');setTimeout(()=>{el.classList.remove('show');el.textContent='Saved';},2000)}

boot();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
