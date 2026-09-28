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
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
THREADS_DIR = BASE_DIR / "threads"
RUN_LOG_FILE = BASE_DIR / "run_log.json"

app = Flask(__name__)

# ---------------------------------------------------------------------------
# In-memory run state (reset on process restart)
# ---------------------------------------------------------------------------

_run_state: dict = {"running": False, "started_at": None, "error": None}

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

    def _do():
        _run_state["running"]    = True
        _run_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _run_state["error"]      = None
        turns  = 0
        status = "failed"
        try:
            from main import load_config as lc, run_conversation, _auto_update_thread
            cfg        = lc()
            transcript = run_conversation(cfg)
            if transcript:
                turns  = len(transcript)
                status = "completed"
                if cfg.get("conversation_mode") == "deal":
                    _auto_update_thread(cfg, transcript)
        except Exception as e:
            _run_state["error"] = str(e)
            status = "error"
        finally:
            _run_state["running"] = False
        _append_run_log({
            "started_at": _run_state["started_at"],
            "turns":      turns,
            "status":     status,
            "error":      _run_state.get("error"),
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
    if "role" in d: cfg[bot_id]["role"] = d["role"]
    if "style" in d: cfg[bot_id]["style"] = d["style"]
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
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;color:#1a1a1a;background:#f5f5f3;min-height:100vh}
a{color:inherit;text-decoration:none}
button{font-family:inherit;cursor:pointer}
input,select,textarea{font-family:inherit;font-size:13px}

/* layout */
.topbar{background:#fff;border-bottom:1px solid #e5e5e3;padding:0 24px;height:52px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar-left{display:flex;align-items:center;gap:10px}
.logo{font-weight:600;font-size:15px;letter-spacing:-.01em}
.status-dot{width:7px;height:7px;border-radius:50%;background:#22c55e;display:inline-block}
.status-label{font-size:12px;color:#666}
.topbar-right{display:flex;align-items:center;gap:8px}
.main{max-width:1100px;margin:0 auto;padding:24px 24px 48px}

/* stats row */
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
.stat-card{background:#fff;border:1px solid #e5e5e3;border-radius:10px;padding:14px 18px}
.stat-n{font-size:22px;font-weight:600;letter-spacing:-.02em;margin-bottom:2px}
.stat-l{font-size:12px;color:#888}

/* two-column layout */
.cols{display:grid;grid-template-columns:1fr 340px;gap:16px;align-items:start}

/* cards */
.card{background:#fff;border:1px solid #e5e5e3;border-radius:10px;padding:18px 20px;margin-bottom:16px}
.card:last-child{margin-bottom:0}
.card-title{font-size:12px;font-weight:600;color:#888;letter-spacing:.06em;text-transform:uppercase;margin-bottom:14px}

/* form rows */
.field{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ee}
.field:last-child{border-bottom:none}
.field-label{font-size:13px;color:#444}
.field-right{display:flex;align-items:center;gap:6px}
input[type=text],input[type=number],input[type=time]{border:1px solid #ddd;border-radius:6px;padding:4px 8px;font-size:13px;color:#1a1a1a;background:#fff;outline:none;transition:border .15s}
input[type=text]:focus,input[type=number]:focus,input[type=time]:focus{border-color:#666}
input[type=text]{width:220px}
input[type=number]{width:56px;text-align:center}
input[type=time]{width:100px}
.toggle{position:relative;display:inline-block;width:36px;height:20px}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#ccc;border-radius:20px;transition:.2s;cursor:pointer}
.slider:before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
input:checked+.slider{background:#1a1a1a}
input:checked+.slider:before{transform:translateX(16px)}
.save-row{display:flex;justify-content:flex-end;margin-top:12px}
.btn{border:1px solid #ddd;border-radius:6px;padding:6px 14px;font-size:13px;background:#fff;color:#1a1a1a;transition:background .15s,border .15s}
.btn:hover{background:#f5f5f3;border-color:#bbb}
.btn-primary{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
.btn-primary:hover{background:#333;border-color:#333}
.btn-run{width:100%;padding:11px;font-size:14px;font-weight:600;background:#1a1a1a;color:#fff;border:none;border-radius:8px;transition:background .15s}
.btn-run:hover{background:#333}
.btn-run:disabled{background:#999;cursor:not-allowed}
.btn-sm{padding:3px 10px;font-size:12px;border-radius:5px}
.saved-msg{font-size:12px;color:#22c55e;opacity:0;transition:opacity .3s}
.saved-msg.show{opacity:1}

/* threads list */
.thread-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f0f0ee}
.thread-row:last-child{border-bottom:none}
.thread-name{font-weight:500;font-size:13px}
.thread-meta{font-size:11px;color:#999;margin-top:2px}
.thread-badges{display:flex;gap:5px;align-items:center}
.badge{font-size:11px;padding:2px 7px;border-radius:20px;font-weight:500}
.badge-yellow{background:#fef3c7;color:#92400e}
.badge-blue{background:#dbeafe;color:#1e40af}
.badge-gray{background:#f3f4f6;color:#6b7280}

/* schedule */
.sched-row{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:13px;color:#444}
.sched-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sched-past{color:#aaa;text-decoration:line-through}

/* run history */
.log-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #f0f0ee;font-size:12px}
.log-row:last-child{border-bottom:none}
.log-status{font-size:11px;padding:1px 6px;border-radius:4px;font-weight:500}
.log-ok{background:#dcfce7;color:#166534}
.log-err{background:#fee2e2;color:#991b1b}
.log-fail{background:#f3f4f6;color:#6b7280}
.run-indicator{font-size:12px;color:#666;margin-top:8px;display:none}
.run-indicator.show{display:block}

/* thread detail panel */
.detail-overlay{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:200;display:none;align-items:flex-start;justify-content:flex-end}
.detail-overlay.open{display:flex}
.detail-panel{background:#fff;width:560px;height:100vh;overflow-y:auto;padding:24px;box-shadow:-4px 0 24px rgba(0,0,0,.08)}
.detail-close{float:right;font-size:20px;border:none;background:none;color:#888;cursor:pointer;padding:0 4px}
.detail-section{margin-bottom:20px}
.detail-section-title{font-size:11px;font-weight:600;color:#888;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px}
.detail-section p,.detail-section li{font-size:13px;color:#333;line-height:1.6}
.detail-section ul{padding-left:16px}
.detail-section li{margin-bottom:4px}
.q-item{padding:7px 10px;border-radius:6px;background:#f9f9f7;margin-bottom:5px;font-size:13px;color:#333;line-height:1.5}
.action-item{padding:7px 10px;border-radius:6px;background:#f0fdf4;margin-bottom:5px;font-size:13px}
.action-owner{font-size:11px;color:#16a34a;margin-top:2px}
.new-info-area{width:100%;border:1px solid #ddd;border-radius:6px;padding:8px 10px;font-size:13px;resize:vertical;min-height:70px;outline:none}
.new-info-area:focus{border-color:#666}
.focus-box{background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:10px 12px;font-size:13px;color:#1e3a5f;margin-bottom:12px;line-height:1.5}
.divider{height:1px;background:#f0f0ee;margin:14px 0}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <span class="logo">Cratus Email Automation</span>
    <span class="status-dot" id="statusDot"></span>
    <span class="status-label" id="statusLabel">Loading...</span>
  </div>
  <div class="topbar-right">
    <span class="saved-msg" id="savedMsg">Saved</span>
  </div>
</div>

<div class="main">

  <!-- Stats -->
  <div class="stats">
    <div class="stat-card">
      <div class="stat-n" id="statRunsToday">—</div>
      <div class="stat-l">Scheduled today</div>
    </div>
    <div class="stat-card">
      <div class="stat-n" id="statNextRun">—</div>
      <div class="stat-l">Next run</div>
    </div>
    <div class="stat-card">
      <div class="stat-n" id="statThreadRun">—</div>
      <div class="stat-l">Active thread progress</div>
    </div>
  </div>

  <div class="cols">
    <!-- Left column -->
    <div>

      <!-- Config -->
      <div class="card">
        <div class="card-title">Schedule &amp; flow</div>

        <div class="field">
          <span class="field-label">Scheduler enabled</span>
          <label class="toggle">
            <input type="checkbox" id="cfgEnabled">
            <span class="slider"></span>
          </label>
        </div>

        <div class="field">
          <span class="field-label">Email subject</span>
          <input type="text" id="cfgSubject">
        </div>

        <div class="field">
          <span class="field-label">Send window (ET)</span>
          <div class="field-right">
            <input type="time" id="cfgWinStart">
            <span style="color:#aaa;font-size:12px">to</span>
            <input type="time" id="cfgWinEnd">
          </div>
        </div>

        <div class="field">
          <span class="field-label">Runs per day</span>
          <div class="field-right">
            <input type="number" id="cfgRunsMin" min="1" max="10">
            <span style="color:#aaa;font-size:12px">–</span>
            <input type="number" id="cfgRunsMax" min="1" max="10">
          </div>
        </div>

        <div class="field">
          <span class="field-label">Min gap between runs (min)</span>
          <input type="number" id="cfgGap" min="10" max="480" style="width:70px">
        </div>

        <div class="field">
          <span class="field-label">Rounds per session</span>
          <div class="field-right">
            <input type="number" id="cfgRoundsMin" min="1" max="20">
            <span style="color:#aaa;font-size:12px">–</span>
            <input type="number" id="cfgRoundsMax" min="1" max="20">
          </div>
        </div>

        <div class="save-row">
          <button class="btn btn-primary" onclick="saveConfig()">Save changes</button>
        </div>
      </div>

      <!-- Threads -->
      <div class="card">
        <div class="card-title">Deal threads</div>
        <div id="threadsList"><span style="color:#aaa;font-size:13px">Loading...</span></div>
      </div>

      <!-- Agents -->
      <div class="card">
        <div class="card-title">Agent personalities</div>
        <div id="agentsList"><span style="color:#aaa;font-size:13px">Loading...</span></div>
      </div>

    </div>

    <!-- Right column -->
    <div>

      <!-- Run Now -->
      <div class="card">
        <div class="card-title">Controls</div>
        <button class="btn-run" id="runBtn" onclick="triggerRun()">▶ Run now</button>
        <div class="run-indicator" id="runIndicator">⏳ Run in progress...</div>
      </div>

      <!-- Today's schedule -->
      <div class="card">
        <div class="card-title">Today's schedule</div>
        <div id="scheduleList"><span style="color:#aaa;font-size:13px">Loading...</span></div>
      </div>

      <!-- Run history -->
      <div class="card">
        <div class="card-title">Recent runs</div>
        <div id="runLog"><span style="color:#aaa;font-size:13px">Loading...</span></div>
      </div>

    </div>
  </div>
</div>

<!-- Prompt editor modal -->
<div class="detail-overlay" id="promptOverlay" onclick="closePromptOverlay(event)">
  <div class="detail-panel" id="promptPanel">
    <button class="detail-close" onclick="closePromptPanel()">✕</button>
    <div id="promptContent"></div>
  </div>
</div>

<!-- Thread detail panel -->
<div class="detail-overlay" id="detailOverlay" onclick="closeDetail(event)">
  <div class="detail-panel" id="detailPanel">
    <button class="detail-close" onclick="closeDetailPanel()">✕</button>
    <div id="detailContent"></div>
  </div>
</div>

<script>
// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let _runPolling = null;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  await Promise.all([loadConfig(), loadThreads(), loadSchedule(), loadRunLog(), loadBots()]);
  pollRunStatus();
  setInterval(() => {
    loadSchedule();
    loadRunLog();
    loadThreads();
    pollRunStatus();
  }, 30000);
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
async function loadConfig() {
  const d = await fetch('/api/config').then(r => r.json());
  document.getElementById('cfgEnabled').checked     = d.schedule_enabled;
  document.getElementById('cfgSubject').value       = d.subject;
  document.getElementById('cfgWinStart').value      = d.window_start;
  document.getElementById('cfgWinEnd').value        = d.window_end;
  document.getElementById('cfgRunsMin').value       = d.runs_min;
  document.getElementById('cfgRunsMax').value       = d.runs_max;
  document.getElementById('cfgGap').value           = d.min_gap;
  document.getElementById('cfgRoundsMin').value     = d.rounds_min;
  document.getElementById('cfgRoundsMax').value     = d.rounds_max;
  updateStatusBadge(d.schedule_enabled);
}

async function saveConfig() {
  const payload = {
    schedule_enabled: document.getElementById('cfgEnabled').checked,
    subject:      document.getElementById('cfgSubject').value,
    window_start: document.getElementById('cfgWinStart').value,
    window_end:   document.getElementById('cfgWinEnd').value,
    runs_min:     parseInt(document.getElementById('cfgRunsMin').value),
    runs_max:     parseInt(document.getElementById('cfgRunsMax').value),
    min_gap:      parseInt(document.getElementById('cfgGap').value),
    rounds_min:   parseInt(document.getElementById('cfgRoundsMin').value),
    rounds_max:   parseInt(document.getElementById('cfgRoundsMax').value),
  };
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const msg = document.getElementById('savedMsg');
  msg.classList.add('show');
  setTimeout(() => msg.classList.remove('show'), 2000);
  updateStatusBadge(payload.schedule_enabled);
  loadSchedule();
}

function updateStatusBadge(enabled) {
  const dot   = document.getElementById('statusDot');
  const label = document.getElementById('statusLabel');
  dot.style.background   = enabled ? '#22c55e' : '#d1d5db';
  label.textContent      = enabled ? 'Scheduler active' : 'Scheduler paused';
}

// ---------------------------------------------------------------------------
// Threads
// ---------------------------------------------------------------------------
async function loadThreads() {
  const threads = await fetch('/api/threads').then(r => r.json());
  const el = document.getElementById('threadsList');
  if (!threads.length) { el.innerHTML = '<span style="color:#aaa;font-size:13px">No threads yet.</span>'; return; }

  // Update stat card
  if (threads[0]) {
    document.getElementById('statThreadRun').textContent = `Run ${threads[0].run_count}`;
  }

  el.innerHTML = threads.map(t => `
    <div class="thread-row">
      <div>
        <div class="thread-name">${t.name}</div>
        <div class="thread-meta">${t.stage} · Run ${t.run_count}</div>
        ${t.discussion_focus ? `<div style="font-size:11px;color:#666;margin-top:3px;max-width:340px;line-height:1.4">${t.discussion_focus}</div>` : ''}
      </div>
      <div class="thread-badges">
        ${t.open_questions ? `<span class="badge badge-yellow">${t.open_questions} open Qs</span>` : ''}
        ${t.open_actions ? `<span class="badge badge-blue">${t.open_actions} actions</span>` : ''}
        <button class="btn btn-sm" onclick="openDetail('${t.id}')">View</button>
      </div>
    </div>`).join('');
}

// ---------------------------------------------------------------------------
// Thread detail
// ---------------------------------------------------------------------------
async function openDetail(tid) {
  const t = await fetch(`/api/threads/${tid}`).then(r => r.json());
  const el = document.getElementById('detailContent');

  const qHtml = (t.open_questions || [])
    .filter(q => !q.resolved)
    .map(q => `<div class="q-item">${q.question}</div>`).join('') || '<p style="color:#aaa">None</p>';

  const aHtml = (t.action_items || [])
    .filter(a => a.status === 'open')
    .map(a => `<div class="action-item"><div>${a.item}</div><div class="action-owner">→ ${a.owner}</div></div>`).join('') || '<p style="color:#aaa">None</p>';

  const factsHtml = (t.known_facts || []).map(f => `<li>${f}</li>`).join('');
  const decisionsHtml = (t.decisions || []).map(d => `<li>${d}</li>`).join('');
  const newInfoHtml = (t.new_information || []).map(i => `<div class="q-item">${i}</div>`).join('');

  el.innerHTML = `
    <h2 style="font-size:17px;font-weight:600;margin-bottom:4px">${(t.thread_id||tid).replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase())}</h2>
    <p style="font-size:12px;color:#888;margin-bottom:18px">${t.stage} · Run ${(t.history||[]).length}</p>

    ${t.discussion_focus ? `<div class="focus-box">Today's focus: ${t.discussion_focus}</div>` : ''}
    ${t.last_run_summary ? `<p style="font-size:13px;color:#555;margin-bottom:16px;line-height:1.6"><em>${t.last_run_summary}</em></p>` : ''}

    <div class="detail-section">
      <div class="detail-section-title">Open questions</div>${qHtml}
    </div>

    <div class="detail-section">
      <div class="detail-section-title">Action items</div>${aHtml}
    </div>

    <div class="detail-section">
      <div class="detail-section-title">Decisions made</div>
      <ul>${decisionsHtml || '<li style="color:#aaa">None</li>'}</ul>
    </div>

    <div class="divider"></div>

    <div class="detail-section">
      <div class="detail-section-title">Known facts</div>
      <ul>${factsHtml || '<li style="color:#aaa">None</li>'}</ul>
    </div>

    ${newInfoHtml ? `<div class="detail-section"><div class="detail-section-title">New information queued</div>${newInfoHtml}</div>` : ''}

    <div class="divider"></div>

    <div class="detail-section">
      <div class="detail-section-title">Add new information</div>
      <textarea class="new-info-area" id="newInfoText" placeholder="Paste seller reply, new data, or any update here..."></textarea>
      <div style="margin-top:8px;text-align:right">
        <button class="btn btn-primary" onclick="submitNewInfo('${tid}')">Add to thread</button>
      </div>
    </div>`;

  document.getElementById('detailOverlay').classList.add('open');
}

async function submitNewInfo(tid) {
  const info = document.getElementById('newInfoText').value.trim();
  if (!info) return;
  await fetch(`/api/threads/${tid}/new_info`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({info})
  });
  document.getElementById('newInfoText').value = '';
  const msg = document.getElementById('savedMsg');
  msg.textContent = 'Added';
  msg.classList.add('show');
  setTimeout(() => { msg.classList.remove('show'); msg.textContent = 'Saved'; }, 2000);
}

function closeDetailPanel() {
  document.getElementById('detailOverlay').classList.remove('open');
}
function closeDetail(e) {
  if (e.target === document.getElementById('detailOverlay')) closeDetailPanel();
}

// ---------------------------------------------------------------------------
// Schedule
// ---------------------------------------------------------------------------
async function loadSchedule() {
  const slots = await fetch('/api/schedule/today').then(r => r.json());
  const el = document.getElementById('scheduleList');

  document.getElementById('statRunsToday').textContent = slots.length || '—';

  const next = slots.find(s => !s.past);
  document.getElementById('statNextRun').textContent = next ? next.time : (slots.length ? 'Done' : 'None');

  if (!slots.length) {
    el.innerHTML = '<span style="color:#aaa;font-size:13px">No sessions scheduled (weekend or disabled).</span>';
    return;
  }
  el.innerHTML = slots.map(s => `
    <div class="sched-row ${s.past ? 'sched-past' : ''}">
      <span class="sched-dot" style="background:${s.past ? '#d1d5db' : '#22c55e'}"></span>
      <span>${s.time}</span>
      ${s.past ? '<span style="font-size:11px;color:#bbb">done</span>' : '<span style="font-size:11px;color:#22c55e">upcoming</span>'}
    </div>`).join('');
}

// ---------------------------------------------------------------------------
// Run log
// ---------------------------------------------------------------------------
async function loadRunLog() {
  const log = await fetch('/api/runlog').then(r => r.json());
  const el = document.getElementById('runLog');
  if (!log.length) { el.innerHTML = '<span style="color:#aaa;font-size:13px">No runs yet.</span>'; return; }
  el.innerHTML = log.map(r => {
    const dt = r.started_at ? new Date(r.started_at).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '—';
    const cls = r.status === 'completed' ? 'log-ok' : (r.status === 'error' ? 'log-err' : 'log-fail');
    return `<div class="log-row">
      <span>${dt}</span>
      <div style="display:flex;align-items:center;gap:6px">
        ${r.turns ? `<span style="font-size:11px;color:#888">${r.turns} turns</span>` : ''}
        <span class="log-status ${cls}">${r.status}</span>
      </div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// Run now
// ---------------------------------------------------------------------------
async function triggerRun() {
  const btn = document.getElementById('runBtn');
  const ind = document.getElementById('runIndicator');
  const res = await fetch('/api/run', {method:'POST'});
  if (!res.ok) { alert((await res.json()).error); return; }
  btn.disabled = true;
  ind.classList.add('show');
  startRunPolling();
}

function startRunPolling() {
  if (_runPolling) clearInterval(_runPolling);
  _runPolling = setInterval(pollRunStatus, 3000);
}

async function pollRunStatus() {
  const s = await fetch('/api/run/status').then(r => r.json());
  const btn = document.getElementById('runBtn');
  const ind = document.getElementById('runIndicator');
  if (s.running) {
    btn.disabled = true;
    ind.classList.add('show');
  } else {
    btn.disabled = false;
    ind.classList.remove('show');
    if (_runPolling) { clearInterval(_runPolling); _runPolling = null; }
    loadRunLog();
    loadThreads();
  }
}

// ---------------------------------------------------------------------------
// Bots / Agents
// ---------------------------------------------------------------------------
let _editingBotId = null;

async function loadBots() {
  const bots = await fetch('/api/bots').then(r => r.json());
  const el = document.getElementById('agentsList');
  if (!bots.length) { el.innerHTML = '<span style="color:#aaa;font-size:13px">No agents found.</span>'; return; }
  el.innerHTML = bots.map(b => `
    <div style="padding:14px 0;border-bottom:1px solid #f0f0ee">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div>
          <span style="font-weight:600;font-size:14px">${b.name}</span>
          <span style="font-size:11px;color:#aaa;margin-left:6px">${b.email}</span>
        </div>
        <button class="btn btn-sm" onclick="openPromptEditor('${b.id}','${b.name}')">Edit full prompt</button>
      </div>
      <div class="field" style="padding:5px 0">
        <span class="field-label">Role</span>
        <input type="text" id="role_${b.id}" value="${b.role.replace(/"/g,'&quot;')}" style="width:260px">
      </div>
      <div class="field" style="padding:5px 0;border-bottom:none">
        <span class="field-label">Style</span>
        <input type="text" id="style_${b.id}" value="${b.style.replace(/"/g,'&quot;')}" style="width:260px">
      </div>
      <div style="text-align:right;margin-top:8px">
        <button class="btn btn-primary btn-sm" onclick="saveBot('${b.id}')">Save</button>
      </div>
    </div>`).join('');
}

async function saveBot(botId) {
  const role  = document.getElementById(`role_${botId}`).value;
  const style = document.getElementById(`style_${botId}`).value;
  await fetch(`/api/bots/${botId}`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({role, style})
  });
  const msg = document.getElementById('savedMsg');
  msg.textContent = 'Saved';
  msg.classList.add('show');
  setTimeout(() => { msg.classList.remove('show'); msg.textContent = 'Saved'; }, 2000);
}

async function openPromptEditor(botId, botName) {
  _editingBotId = botId;
  const {content} = await fetch(`/api/prompts/${botId}`).then(r => r.json());
  const escaped = content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  document.getElementById('promptContent').innerHTML = `
    <h2 style="font-size:17px;font-weight:600;margin-bottom:4px">${botName} — Prompt Template</h2>
    <p style="font-size:12px;color:#888;margin-bottom:14px">Full instructions that govern ${botName}'s writing style, role, and behavior in every email.</p>
    <textarea id="promptTextarea" style="width:100%;min-height:420px;border:1px solid #ddd;border-radius:6px;padding:10px 12px;font-size:12px;font-family:monospace;line-height:1.6;resize:vertical;outline:none;color:#222">${escaped}</textarea>
    <div style="text-align:right;margin-top:10px">
      <button class="btn btn-primary" onclick="savePrompt()">Save prompt</button>
    </div>`;
  document.getElementById('promptOverlay').classList.add('open');
}

async function savePrompt() {
  const content = document.getElementById('promptTextarea').value;
  await fetch(`/api/prompts/${_editingBotId}`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({content})
  });
  const msg = document.getElementById('savedMsg');
  msg.textContent = 'Prompt saved';
  msg.classList.add('show');
  setTimeout(() => { msg.classList.remove('show'); msg.textContent = 'Saved'; }, 2500);
}

function closePromptPanel() { document.getElementById('promptOverlay').classList.remove('open'); }
function closePromptOverlay(e) { if (e.target === document.getElementById('promptOverlay')) closePromptPanel(); }

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
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
