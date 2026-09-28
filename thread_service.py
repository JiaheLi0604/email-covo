"""
thread_service.py
-----------------
Reads and writes persistent business thread state.

A thread is a long-running business discussion (e.g. an acquisition deal)
whose state evolves across multiple conversation runs. Thread state lives
in threads/<thread_id>.json and is updated by thread_updater.py after
each conversation.

This service is responsible ONLY for:
  - loading thread state from disk
  - determining the highest-priority discussion focus via Claude (pre-run)
  - building a context string for injection into agent prompts ($topic)
  - writing updated state back to disk

It does NOT send emails, poll inboxes, or touch any email transport.
The email layer is completely separate and unchanged.

Integration point in main.py:
  When conversation_mode = "deal", main.py calls build_deal_topic(config)
  instead of build_runtime_context(config). Everything else is identical.
"""

import json
import os
from typing import Any, Dict, List, Optional

THREADS_DIR = "threads"
FOCUS_MODEL = "claude-haiku-4-5-20251001"
FOCUS_MAX_TOKENS = 200


class ThreadServiceError(Exception):
    """Raised when a thread file cannot be found or parsed."""


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------

def load_thread(thread_id: str) -> Dict[str, Any]:
    """Load thread state from threads/<thread_id>.json."""
    path = os.path.join(THREADS_DIR, f"{thread_id}.json")
    if not os.path.exists(path):
        raise ThreadServiceError(
            f"Thread file not found: {path}\n"
            f"Create it first — see threads/sequim_tiny_home_village.json as a template."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_thread(thread: Dict[str, Any]) -> None:
    """Write thread state back to threads/<thread_id>.json."""
    thread_id = thread.get("thread_id", "unknown")
    os.makedirs(THREADS_DIR, exist_ok=True)
    path = os.path.join(THREADS_DIR, f"{thread_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(thread, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Dynamic focus determination
# ---------------------------------------------------------------------------

def _determine_discussion_focus(thread: Dict[str, Any]) -> str:
    """
    Call Claude to determine the single most material unresolved issue
    for this run, based on current thread state.

    Uses a fast/cheap model (Haiku) since this is a short classification task.
    Falls back to first unresolved priority_question if Claude call fails.
    """
    try:
        from anthropic import Anthropic
        from dotenv import load_dotenv
        load_dotenv()
        client = Anthropic()
    except Exception:
        # Fallback: use first priority question
        pq = thread.get("priority_questions", [])
        return pq[0] if pq else "Evaluate the investment thesis and identify the most important unknown."

    # Build a compact snapshot for the focus prompt
    snapshot = {
        "investment_thesis": thread.get("investment_thesis", ""),
        "priority_questions": thread.get("priority_questions", []),
        "open_questions": [
            q["question"] for q in thread.get("open_questions", [])
            if not q.get("resolved")
        ],
        "last_run_summary": thread.get("last_run_summary"),
        "new_information": thread.get("new_information", []),
        "decisions": thread.get("decisions", []),
        "history_summaries": [
            f"Run {h['run']} ({h.get('date','')}): {h.get('summary','')}"
            for h in thread.get("history", [])[-3:]
        ],
    }

    prompt = f"""You are advising on a real estate acquisition. Given the current state of this deal thread, identify the single most material unresolved issue that should be the focus of the next team discussion.

THREAD STATE:
{json.dumps(snapshot, indent=2)}

Rules:
- Pick the issue that, if resolved, would most change the investment decision.
- If the last run covered a topic that is still unresolved and material, continue it — do NOT switch topics just for variety.
- If there is new_information, prioritize discussion that incorporates it.
- Return ONE sentence only (under 120 characters). No explanation, no preamble. Just the focus sentence."""

    try:
        response = client.messages.create(
            model=FOCUS_MODEL,
            max_tokens=FOCUS_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        focus = response.content[0].text.strip().strip('"').strip("'")
        return focus
    except Exception as e:
        print(f"[thread_service] Focus determination failed: {e}. Using fallback.")
        pq = thread.get("priority_questions", [])
        return pq[0] if pq else "Evaluate the investment thesis and identify the most important unknown."


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_thread_context(thread: Dict[str, Any], discussion_focus: Optional[str] = None) -> str:
    """
    Build the plain-text context string injected into agent prompts as $topic.

    Sections included:
      - thread header (type, stage, objective)
      - investment thesis
      - today's discussion focus (dynamically determined)
      - priority questions
      - known facts
      - open questions (unresolved only)
      - decisions made
      - open action items
      - new information (manually added since last run)
      - last run summary
      - recent history (last 2 runs)
      - per-agent behavioral instructions
    """
    name = thread.get("thread_id", "").replace("_", " ").title()

    lines: List[str] = [
        f"== ACTIVE DEAL THREAD: {name} ==",
        f"Type:      {thread.get('type', 'unknown')}",
        f"Stage:     {thread.get('stage', 'unknown')}",
        f"Objective: {thread.get('objective', '')}",
    ]

    # Investment thesis
    thesis = thread.get("investment_thesis", "")
    if thesis:
        lines += ["", "INVESTMENT THESIS:", f"  {thesis}"]

    # Today's discussion focus — most important section
    focus = discussion_focus or thread.get("discussion_focus", "")
    if focus:
        lines += [
            "",
            "TODAY'S DISCUSSION FOCUS (this is your primary task — stay on this):",
            f"  {focus}",
        ]

    # Priority questions (manually curated, ordered by materiality)
    priority_q = thread.get("priority_questions", [])
    if priority_q:
        lines += ["", "PRIORITY QUESTIONS (ranked by materiality — address the most relevant ones):"]
        for i, q in enumerate(priority_q, 1):
            lines.append(f"  {i}. {q}")

    # Known facts
    known = thread.get("known_facts", [])
    if known:
        lines += ["", "KNOWN FACTS (ground truth — do not contradict or re-state redundantly):"]
        for fact in known:
            lines.append(f"  - {fact}")
    else:
        lines += ["", "KNOWN FACTS: (none confirmed yet)"]

    # Open questions from previous runs (unresolved only)
    open_q = [q for q in thread.get("open_questions", []) if not q.get("resolved")]
    if open_q:
        lines += ["", "OPEN QUESTIONS FROM PREVIOUS RUNS (not yet answered):"]
        for q in open_q:
            lines.append(f"  - {q['question']}")

    # Decisions
    decisions = thread.get("decisions", [])
    if decisions:
        lines += ["", "DECISIONS ALREADY MADE (do not re-litigate unless new info changes them):"]
        for d in decisions:
            lines.append(f"  - {d}")

    # Open action items
    open_actions = [
        a for a in thread.get("action_items", [])
        if a.get("status") != "completed"
    ]
    if open_actions:
        lines += ["", "OPEN ACTION ITEMS:"]
        for a in open_actions:
            owner = a.get("owner", "")
            item = a.get("item", "")
            lines.append(f"  - [{owner}] {item}" if owner else f"  - {item}")

    # New information (manually added by user before this run)
    new_info = thread.get("new_information", [])
    if new_info:
        lines += [
            "",
            "NEW INFORMATION SINCE LAST DISCUSSION (fresh — engage with it directly):",
        ]
        for n in new_info:
            lines.append(f"  + {n}")

    # Last run summary
    last_summary = thread.get("last_run_summary")
    if last_summary:
        lines += ["", f"LAST DISCUSSION SUMMARY: {last_summary}"]

    # Recent history (last 2 runs max)
    history = thread.get("history", [])
    if history:
        recent = history[-2:]
        lines += ["", "PREVIOUS RUNS:"]
        for h in recent:
            run_n = h.get("run", "?")
            date = h.get("date", "")
            summary = h.get("summary", "")
            lines.append(f"  Run {run_n} ({date}): {summary}")
            prev_q = h.get("questions_raised", [])
            if prev_q:
                lines.append(f"    Questions raised: {'; '.join(prev_q[:3])}")
            prev_d = h.get("decisions_made", [])
            if prev_d:
                lines.append(f"    Decisions: {'; '.join(prev_d)}")

    # Agent behavioral instructions
    lines += [
        "",
        "AGENT ROLES FOR THIS DEAL DISCUSSION:",
        "  Brian — Investment priorities, risk tolerance, what is still unknown, what must happen next.",
        "  Joey  — Property and deal-level observations, site findings, seller information.",
        "  Li    — Organize what is known vs. unknown, track open questions and action items,",
        "          identify gaps, help convert discussion into structured next steps.",
        "",
        "ALL AGENTS MUST:",
        "  - Focus the conversation on TODAY'S DISCUSSION FOCUS above.",
        "  - Clearly distinguish KNOWN facts, INFERENCE, and OPEN QUESTIONS.",
        "  - Not invent financial figures, site counts, pricing, NOI, cap rate, or seller details.",
        "  - If something is unknown, say so: 'we still need to confirm...', 'not sure yet...'",
        "  - Inference is allowed but must be framed as inference:",
        "    GOOD: 'If the seller is under pressure to cover the $835k debt, they may accept a lower offer.'",
        "    BAD:  'The seller is desperate and will take $1.5M.'",
        "  - Build on what was discussed in previous runs. Do not repeat resolved points.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Integration point for main.py
# ---------------------------------------------------------------------------

def build_deal_topic(config: Dict[str, Any]) -> str:
    """
    Build the topic string for deal mode. Called from main.py in place of
    build_runtime_context() when conversation_mode = 'deal'.

    Steps:
      1. Load the active thread JSON
      2. Call Claude (Haiku) to determine the highest-priority discussion focus
      3. Write the focus back to the thread JSON for logging/inspection
      4. Return the full topic context string

    Does not raise — returns an error message string on misconfiguration.
    """
    active_threads = config.get("active_threads", [])
    if not active_threads:
        return (
            "== DEAL MODE — NO ACTIVE THREAD ==\n"
            "Set 'active_threads' in config.json to the thread ID(s) you want to discuss."
        )

    thread_id = active_threads[0]
    try:
        thread = load_thread(thread_id)
    except ThreadServiceError as e:
        return f"[ERROR loading thread '{thread_id}']: {e}"

    # Determine dynamic focus for this run
    print(f"[thread_service] Determining discussion focus for '{thread_id}'...")
    focus = _determine_discussion_focus(thread)
    print(f"[thread_service] Focus: {focus}")

    # Write focus back to JSON (for inspection after each run)
    thread["discussion_focus"] = focus
    save_thread(thread)

    return build_thread_context(thread, discussion_focus=focus)
