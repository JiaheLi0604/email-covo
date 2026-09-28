"""
thread_updater.py
-----------------
Post-conversation intelligence layer for persistent Deal Threads.

After a conversation finishes, the Thread Updater:
  1. Reads current thread state from threads/<thread_id>.json
  2. Passes the full conversation transcript to Claude
  3. Claude extracts structured updates (conservatively)
  4. Thread state is updated and written back to disk
  5. new_information is moved into known_facts, then cleared
  6. A history entry is appended

CONSERVATIVE RULES (MVP):
  - Only add facts EXPLICITLY confirmed in the conversation (not inferred)
  - Only add questions actually raised in the conversation
  - Mark questions resolved only if clearly answered
  - Add decisions only if explicitly agreed upon
  - NEVER auto-delete existing known_facts
  - NEVER convert speculation or inference into known_facts
  - new_information items are moved to known_facts after processing, then cleared

Called automatically from main.py when conversation_mode = 'deal'.
Can also be run standalone:
    python thread_updater.py --thread washington_rv_park --run 1 \\
                             --transcript transcripts/my_transcript.json
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from anthropic import Anthropic
from dotenv import load_dotenv

from thread_service import load_thread, save_thread

EXTRACTION_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1000


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

def _build_extraction_prompt(thread: Dict[str, Any], transcript: List[Tuple[str, str]]) -> str:
    """Build the prompt asking Claude to extract structured updates from the transcript."""

    thread_snapshot = json.dumps(
        {
            "known_facts": thread.get("known_facts", []),
            "open_questions": thread.get("open_questions", []),
            "decisions": thread.get("decisions", []),
            "action_items": thread.get("action_items", []),
            "new_information": thread.get("new_information", []),
        },
        indent=2,
    )

    transcript_text = "\n\n".join(
        f"{name} wrote:\n{body.strip()}"
        for name, body in transcript
    )

    return f"""You are a conservative business analyst. Your job is to extract ONLY explicitly confirmed information from a business conversation and structure it for a persistent deal thread.

CURRENT THREAD STATE:
{thread_snapshot}

CONVERSATION TRANSCRIPT:
{transcript_text}

EXTRACTION RULES (strict — follow exactly):
1. known_facts_to_add: Only facts that were EXPLICITLY STATED AND CONFIRMED in the conversation.
   - Do NOT include speculation, inference, or hypothetical statements.
   - Do NOT include things like "could be", "might be", "if X then Y".
   - Example OK: "The site visit was completed on Monday."
   - Example NOT OK: "The electrical system may require significant investment."

2. questions_to_add: New open questions that were raised but not answered in this conversation.
   - Only include questions clearly posed by a participant, not rhetorical observations.

3. questions_to_resolve: Zero-based indices from the open_questions list above that were
   CLEARLY AND DIRECTLY ANSWERED in this conversation. Be conservative — only mark as
   resolved if the answer was explicit, not just partially addressed.

4. decisions_to_add: Decisions that were explicitly agreed upon by the participants.
   - "We should look into X" is NOT a decision. "We agreed to do X" IS a decision.

5. action_items_to_add: Concrete next steps with a named owner if mentioned.
   - Include only things someone specifically committed to doing.

6. one_sentence_summary: One sentence (under 120 chars) describing what this conversation covered.

Respond with ONLY valid JSON in this exact format — no explanation, no markdown, just JSON:
{{
  "known_facts_to_add": [],
  "questions_to_add": [],
  "questions_to_resolve": [],
  "decisions_to_add": [],
  "action_items_to_add": [
    {{"item": "description", "owner": "Name or empty string", "status": "open"}}
  ],
  "one_sentence_summary": "..."
}}"""


# ---------------------------------------------------------------------------
# Core update function
# ---------------------------------------------------------------------------

def update_thread(
    thread_id: str,
    transcript: List[Tuple[str, str]],
    run_number: int,
    client: Optional[Anthropic] = None,
) -> Dict[str, Any]:
    """
    Update thread state based on a completed conversation.

    Args:
        thread_id:   ID matching threads/<thread_id>.json
        transcript:  list of (agent_name, message_body) from run_conversation()
        run_number:  sequential run number (1, 2, 3, ...)
        client:      optional pre-built Anthropic client

    Returns:
        The updated thread dict (already saved to disk).
    """
    load_dotenv()
    if client is None:
        client = Anthropic()

    thread = load_thread(thread_id)
    prompt = _build_extraction_prompt(thread, transcript)

    try:
        response = client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        print(f"[thread_updater] Claude API call failed: {e}")
        print("  Thread not updated — conversation was saved but thread state unchanged.")
        return thread

    # Parse JSON response
    try:
        updates = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                updates = json.loads(match.group())
            except json.JSONDecodeError:
                updates = {}
        else:
            updates = {}

    if not updates:
        print("[thread_updater] Warning: could not parse Claude's extraction response.")
        print(f"  Raw response (first 300 chars): {raw[:300]}")
        print("  Thread not updated.")
        return thread

    # --- Apply updates (conservative) ---

    # 1. Add new known facts (no duplicates)
    existing_facts = set(thread.get("known_facts", []))
    for fact in updates.get("known_facts_to_add", []):
        if fact and fact not in existing_facts:
            thread.setdefault("known_facts", []).append(fact)
            existing_facts.add(fact)

    # 2. Add new open questions (no duplicates)
    existing_questions = {q["question"] for q in thread.get("open_questions", [])}
    for q_text in updates.get("questions_to_add", []):
        if q_text and q_text not in existing_questions:
            thread.setdefault("open_questions", []).append(
                {"question": q_text, "resolved": False}
            )
            existing_questions.add(q_text)

    # 3. Mark questions resolved (by index, conservative)
    open_questions = thread.get("open_questions", [])
    for idx in updates.get("questions_to_resolve", []):
        if isinstance(idx, int) and 0 <= idx < len(open_questions):
            open_questions[idx]["resolved"] = True

    # 4. Add decisions (no duplicates)
    existing_decisions = set(thread.get("decisions", []))
    for d in updates.get("decisions_to_add", []):
        if d and d not in existing_decisions:
            thread.setdefault("decisions", []).append(d)
            existing_decisions.add(d)

    # 5. Add action items (no duplicates by item text)
    existing_action_items = {a["item"] for a in thread.get("action_items", [])}
    for a in updates.get("action_items_to_add", []):
        item_text = a.get("item", "")
        if item_text and item_text not in existing_action_items:
            thread.setdefault("action_items", []).append(a)
            existing_action_items.add(item_text)

    # 6. Move new_information into known_facts, then clear it
    #    new_information was already read by agents during the conversation.
    #    Now we absorb it into known_facts so it doesn't re-appear as "new" next run.
    cleared_new_info = list(thread.get("new_information", []))
    for info in cleared_new_info:
        if info and info not in existing_facts:
            thread.setdefault("known_facts", []).append(info)
            existing_facts.add(info)
    thread["new_information"] = []

    # 7. Save last_run_summary (used by thread_service to inform next focus)
    one_sentence = updates.get("one_sentence_summary", "(no summary generated)")
    thread["last_run_summary"] = one_sentence

    # 8. Append history entry
    history_entry = {
        "run": run_number,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "summary": one_sentence,
        "questions_raised": updates.get("questions_to_add", []),
        "decisions_made": updates.get("decisions_to_add", []),
        "new_information_processed": cleared_new_info,
    }
    thread.setdefault("history", []).append(history_entry)

    save_thread(thread)
    print(f"[thread_updater] Thread '{thread_id}' updated — Run {run_number} recorded.")
    return thread


# ---------------------------------------------------------------------------
# Human-readable summary
# ---------------------------------------------------------------------------

def print_thread_summary(thread: Dict[str, Any]) -> None:
    """Print a readable summary of current thread state to stdout."""
    print(f"\n{'=' * 55}")
    print(f"Deal Thread: {thread.get('thread_id', '?')}")
    print(f"Stage:       {thread.get('stage', '?')}")

    known = thread.get("known_facts", [])
    print(f"\nKnown facts ({len(known)}):")
    for f in known:
        print(f"  - {f}")

    open_q = [q for q in thread.get("open_questions", []) if not q.get("resolved")]
    resolved_q = [q for q in thread.get("open_questions", []) if q.get("resolved")]
    print(f"\nOpen questions ({len(open_q)}):")
    for q in open_q:
        print(f"  ? {q['question']}")
    if resolved_q:
        print(f"\nResolved questions ({len(resolved_q)}):")
        for q in resolved_q:
            print(f"  ✓ {q['question']}")

    decisions = thread.get("decisions", [])
    print(f"\nDecisions ({len(decisions)}):")
    for d in decisions:
        print(f"  ✓ {d}")

    open_a = [a for a in thread.get("action_items", []) if a.get("status") != "completed"]
    print(f"\nOpen action items ({len(open_a)}):")
    for a in open_a:
        owner = a.get("owner") or "?"
        print(f"  [{owner}] {a['item']}")

    new_info = thread.get("new_information", [])
    if new_info:
        print(f"\nPending new_information ({len(new_info)}) — not yet processed:")
        for n in new_info:
            print(f"  + {n}")

    history = thread.get("history", [])
    print(f"\nHistory ({len(history)} run(s)):")
    for h in history:
        print(f"  Run {h.get('run', '?')} ({h.get('date', '')}): {h.get('summary', '')}")

    print(f"{'=' * 55}\n")


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Update a deal thread after a conversation run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python thread_updater.py --thread washington_rv_park --run 1 \\
      --transcript transcripts/my_run.json
  python thread_updater.py --thread washington_rv_park --summary
        """,
    )
    parser.add_argument(
        "--thread", required=True,
        help="Thread ID (e.g. washington_rv_park)"
    )
    parser.add_argument(
        "--run", type=int,
        help="Run number (e.g. 1, 2, 3)"
    )
    parser.add_argument(
        "--transcript",
        help="Path to transcript JSON: [{\"name\": \"Li\", \"body\": \"...\"}]"
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Just print the current thread state without updating"
    )
    args = parser.parse_args()

    if args.summary:
        thread = load_thread(args.thread)
        print_thread_summary(thread)
        raise SystemExit(0)

    if not args.transcript:
        print("ERROR: --transcript is required unless using --summary.")
        raise SystemExit(1)
    if not args.run:
        print("ERROR: --run is required unless using --summary.")
        raise SystemExit(1)

    with open(args.transcript, "r", encoding="utf-8") as f:
        raw_transcript = json.load(f)

    # Support two transcript formats:
    # 1. [{"name": "Li", "body": "..."}]  (structured)
    # 2. [["Li", "..."]]                  (tuple-style)
    if raw_transcript and isinstance(raw_transcript[0], dict):
        transcript = [(item["name"], item["body"]) for item in raw_transcript]
    else:
        transcript = [tuple(item) for item in raw_transcript]

    updated = update_thread(args.thread, transcript, args.run)
    print_thread_summary(updated)
