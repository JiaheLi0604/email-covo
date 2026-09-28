"""
EmailConvo — conversation entry point.

Exposes two public callables:

  run_conversation(config, debug=False)
      Execute one complete email conversation from a pre-loaded config dict.
      Supports any number of participants via the "conversation_flow" key.
      Returns the transcript as a list of (name, body) tuples, or None on failure.

  main()
      Load config.json, then call run_conversation().

Usage:
    python main.py                  # one-shot run
    python demo.py                  # demo runner with transcript saving
    python scheduler.py             # automated weekly schedule

Conversation flow:
    "conversation_flow" in config.json controls the order of participants:
        ["bot_a", "bot_b"]               — two-bot back-and-forth
        ["bot_a", "bot_b", "bot_c"]      — three-way relay
    "conversation_rounds" is the total number of individual messages sent.
    Each turn cycles through the flow in order (wrapping around).
    Example with 3 bots, rounds=3: Li→Brian, Brian→Celine, Celine→Li.

Dry-run mode ("dry_run": true):
    Generates messages without sending anything. DryRunService stands in for
    GmailService — the conversation loop is identical.

Debug mode ("debug": true):
    Verbose API logging. Leave false for clean demo output.
"""

import json
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from agents import EmailAgent
from claude_service import ClaudeService, ClaudeServiceError
from console import Console
from gmail_service import GmailService, GmailServiceError
from memory_service import MemoryService, MemoryServiceError
from messaging_service import AgentInboxTimeoutError
from outlook_service import OutlookService, OutlookServiceError
from thread_service import build_deal_topic

CONFIG_FILE = "config.json"


def load_config(path: str = CONFIG_FILE) -> Dict[str, Any]:
    """Load the weekly conversation config from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_flow(config: Dict[str, Any]) -> Tuple[List[str], int]:
    """
    Determine the actual conversation flow and round count for this run.

    flow_selection.mode:
      "manual"  — use conversation_flow and conversation_rounds as-is (default)
      "random"  — pick a random flow_template; optionally randomize rounds

    Returns (flow, total_turns) without modifying config.
    """
    flow_sel = config.get("flow_selection", {})
    mode = flow_sel.get("mode", "manual")

    if mode == "random":
        templates = flow_sel.get(
            "flow_templates",
            [config.get("conversation_flow", ["bot_a", "bot_b"])],
        )
        flow = random.choice(templates)

        rr = flow_sel.get("random_rounds", {})
        if rr.get("enabled", False):
            min_r = int(rr.get("min", 3))
            max_r = int(rr.get("max", 6))
            total_turns = random.randint(min_r, max_r)
        else:
            total_turns = config.get("conversation_rounds", len(flow))
    else:
        flow = config.get("conversation_flow", ["bot_a", "bot_b"])
        total_turns = config.get("conversation_rounds", len(flow))

    return flow, total_turns


def build_runtime_context(config: Dict[str, Any]) -> str:
    """
    Build the Weekly Operations Context string injected into every agent's prompt.

    Combines weekly_context (campground, metrics, active events) with
    company_memory (people, known facts). Does not modify config.json.

    Event selection modes (event_selection.mode):
      "manual"  — uses weekly_context.active_events as-is
      "random"  — randomly picks min_events..max_events from event_library

    Returns a plain-text string that replaces weekly_topic as the conversation topic.
    """
    weekly = config.get("weekly_context", {})
    memory = config.get("company_memory", {})
    event_library: List[Dict] = config.get("event_library", [])
    event_selection = config.get("event_selection", {"mode": "manual"})

    # --- Select active events ---
    mode = event_selection.get("mode", "manual")
    if mode == "random" and event_library:
        min_e = int(event_selection.get("min_events", 2))
        max_e = int(event_selection.get("max_events", 4))
        k = random.randint(min_e, min(max_e, len(event_library)))
        selected_ids = [e["id"] for e in random.sample(event_library, k)]
    else:
        selected_ids = weekly.get("active_events", [])

    event_map = {e["id"]: e for e in event_library}

    # --- Format sections ---
    campground = weekly.get("campground", "Unknown Campground")
    period = weekly.get("reporting_period", "current week")

    lines: List[str] = [
        f"== {campground} — Weekly Operations Update ==",
        f"Reporting Period: {period}",
    ]

    # Metrics
    metrics = weekly.get("metrics", {})
    if metrics:
        lines += ["", "WEEKLY METRICS"]
        for k, v in metrics.items():
            lines.append(f"  - {k.replace('_', ' ').title()}: {v}")

    # Active events
    event_lines = []
    for eid in selected_ids:
        if eid in event_map:
            e = event_map[eid]
            event_lines.append(
                f"  - {e['title']} [{e['category']}, {e['severity']} severity]: {e['description']}"
            )
    if event_lines:
        lines += ["", "ACTIVE ISSUES THIS WEEK"]
        lines += event_lines

    # Recent updates
    recent = weekly.get("recent_updates", [])
    if recent:
        lines += ["", "RECENT UPDATES"]
        lines += [f"  - {u}" for u in recent]

    # Known company facts (agents must not contradict these)
    fact_lines: List[str] = []
    for person in memory.get("people", {}).values():
        fact_lines.append(f"  - {person['name']} is the {person['role']}.")
        for rel, name in person.get("relationships", {}).items():
            fact_lines.append(f"  - {person['name']}'s {rel} is {name}.")
    for cg in memory.get("campgrounds", {}).values():
        for fact in cg.get("known_facts", []):
            fact_lines.append(f"  - {fact}")

    if fact_lines:
        lines += ["", "KNOWN COMPANY FACTS (treat these as ground truth — never contradict them)"]
        lines += fact_lines

    return "\n".join(lines)


def _apply_reply_delay(config: Dict[str, Any], agent_name: str, dry_run: bool) -> None:
    """
    Wait a random number of seconds before an agent sends a reply.

    Makes the demo feel more realistic — replies don't arrive instantly.
    No-ops when reply_delay_seconds.enabled is false or when dry_run is true.
    """
    delay_cfg = config.get("reply_delay_seconds", {})
    if not delay_cfg.get("enabled", False):
        return

    min_s = int(delay_cfg.get("min", 30))
    max_s = int(delay_cfg.get("max", 120))
    seconds = random.randint(min_s, max_s)

    if dry_run:
        print(f"    [dry run] Would wait {seconds}s before {agent_name} replies")
        return

    print(f"    Waiting {seconds} seconds before {agent_name} replies...")
    time.sleep(seconds)


def build_memory_context(memory: MemoryService) -> Optional[str]:
    """Return a formatted recent-history string, or None if no history yet."""
    ctx = memory.get_recent_context(limit=10)
    return ctx if ctx else None


def run_conversation(
    config: Dict[str, Any],
    debug: bool = False,
) -> Optional[List[Tuple[str, str]]]:
    """
    Execute one complete email conversation.

    Reads "conversation_flow" from config to determine participant order and
    "conversation_rounds" for total number of messages to send.

    Args:
        config: dict loaded from config.json (or equivalent).
        debug:  when True, enable verbose API logging.

    Returns:
        List of (agent_name, message_body) tuples — the full transcript.
        Returns None if the run could not complete.
    """
    load_dotenv()

    dry_run = config.get("dry_run", False)
    console = Console(verbose=debug)

    # --- Parse config ---
    try:
        # Build runtime context from weekly_context + company_memory + event_library.
        # Fall back to plain weekly_topic for configs that haven't been migrated yet.
        # When conversation_mode = "deal", load the active Deal Thread instead.
        mode = config.get("conversation_mode", "operations")
        if mode == "deal":
            topic = build_deal_topic(config)
        elif "weekly_context" in config:
            topic = build_runtime_context(config)
        else:
            topic = config["weekly_topic"]
        subject = config.get("subject", "Weekly Operations Update")
        reply_subject = f"Re: {subject}"
        inbox_timeout = config.get("inbox_timeout_seconds", 120)
        poll_interval = config.get("inbox_poll_interval_seconds", 5)
        flow, total_turns = resolve_flow(config)

        agents: Dict[str, EmailAgent] = {}
        for bot_key in flow:
            if bot_key not in config:
                console.error(f"config.json is missing '{bot_key}' block")
                return None
            agents[bot_key] = EmailAgent.from_config(config[bot_key])

    except KeyError as e:
        console.error(f"config.json is missing required field: {e}")
        return None

    n = len(flow)
    agent_list = [agents[k] for k in flow]

    # All unique participant emails — used to build CC lists so every
    # agent sees every message (real team email chains work this way).
    all_emails = list({agents[k].email for k in set(flow)})

    # --- Print header and summary ---
    console.header()
    console.summary(
        topic,
        [{"name": a.name, "role": a.role, "email": a.email} for a in agent_list],
        total_turns,
        dry_run=dry_run,
    )

    # --- Initialise transports (one per bot) ---
    if dry_run:
        from dry_run_service import DryRunService
        transports = {
            k: DryRunService(account_email=agents[k].email, verbose=debug)
            for k in flow
        }
    else:
        def _make_transport(bot_key: str) -> object:
            transport_type = config.get(bot_key, {}).get("transport", "gmail").lower()
            if transport_type == "outlook":
                return OutlookService(
                    token_file=f"token_{bot_key}.json",
                    account_email=agents[bot_key].email,
                    verbose=debug,
                )
            return GmailService(
                token_file=f"token_{bot_key}.json",
                account_email=agents[bot_key].email,
                verbose=debug,
            )

        transports = {k: _make_transport(k) for k in flow}

    memory = MemoryService()

    try:
        claude = ClaudeService()
    except ClaudeServiceError as e:
        console.error(f"Could not initialise Claude: {e}")
        return None

    # --- Authenticate all bots (deduplicate — bot_b may appear twice in flow) ---
    print()
    try:
        seen_auth: set = set()
        for bot_key in flow:
            if bot_key not in seen_auth:
                transports[bot_key].authenticate()
                console.auth_ok(agents[bot_key].name)
                seen_auth.add(bot_key)
    except (GmailServiceError, OutlookServiceError) as e:
        console.error(f"Authentication failed: {e}")
        return None

    memory_context = build_memory_context(memory)
    transcript: List[Tuple[str, str]] = []

    # ------------------------------------------------------------------
    # Turn 0 — first bot in flow opens the conversation
    # ------------------------------------------------------------------

    opener_key = flow[0]
    first_recipient_key = flow[1 % n]
    opener = agents[opener_key]
    first_recipient = agents[first_recipient_key]

    console.round_header(1)
    console.agent_start(opener.name, "Generating")
    try:
        body = opener.open_conversation(
            claude,
            topic=topic,
            template_name=opener.template,
            receiver_email=first_recipient.email,
            memory_context=memory_context,
        )
    except ClaudeServiceError as e:
        console.error(f"Claude failed to generate {opener.name}'s opening message: {e}")
        return None

    opener_cc = [e for e in all_emails
                 if e != opener.email and e != first_recipient.email]
    last_sent_at = datetime.now(timezone.utc)
    console.agent_step("Sending")
    try:
        opener.send_message(
            transports[opener_key], memory,
            first_recipient.email, subject, body,
            cc=opener_cc or None,
        )
    except (GmailServiceError, OutlookServiceError, MemoryServiceError) as e:
        console.error(f"Failed to send {opener.name}'s opening message: {e}")
        return None
    console.agent_ok("Delivered")
    transcript.append((opener.name, body))
    memory_context = build_memory_context(memory)

    # ------------------------------------------------------------------
    # Turns 1 … total_turns-1 — each bot reads the previous, replies to next
    # ------------------------------------------------------------------

    for turn in range(1, total_turns):
        cur_key  = flow[turn % n]
        prev_key = flow[(turn - 1) % n]
        next_key = flow[(turn + 1) % n]

        cur_agent  = agents[cur_key]
        prev_agent = agents[prev_key]
        next_agent = agents[next_key]

        console.round_header(turn + 1)
        console.agent_start(cur_agent.name, "Waiting")
        try:
            received = cur_agent.check_inbox(
                transports[cur_key],
                from_sender=prev_agent.email,
                after_time=last_sent_at,
                timeout=inbox_timeout,
                poll_interval=poll_interval,
            )
        except AgentInboxTimeoutError as e:
            console.error(str(e))
            return None

        console.agent_ok("Received")
        console.agent_step("Generating")
        try:
            body = cur_agent.generate_reply(
                claude,
                topic=topic,
                template_name=cur_agent.template,
                received=received,
                memory_context=memory_context,
                thread=transcript,  # full thread with names so agent can reference prior speakers
            )
        except ClaudeServiceError as e:
            console.error(f"Claude failed to generate {cur_agent.name}'s reply: {e}")
            return None

        reply_cc = [e for e in all_emails
                    if e != cur_agent.email and e != next_agent.email]
        _apply_reply_delay(config, cur_agent.name, dry_run)
        last_sent_at = datetime.now(timezone.utc)
        console.agent_step("Sending")
        try:
            cur_agent.send_message(
                transports[cur_key], memory,
                next_agent.email, reply_subject, body,
                reply_to=received,
                cc=reply_cc or None,
            )
        except (GmailServiceError, OutlookServiceError, MemoryServiceError) as e:
            console.error(f"Failed to send {cur_agent.name}'s reply: {e}")
            return None
        console.agent_ok("Delivered")
        transcript.append((cur_agent.name, body))
        memory_context = build_memory_context(memory)

    return transcript


def _auto_update_thread(config: Dict[str, Any], transcript: List[Tuple[str, str]]) -> None:
    """
    After a deal-mode conversation, update the active thread's persistent state.
    Called automatically from main() when conversation_mode = 'deal'.
    Failures are non-fatal — the conversation already completed successfully.
    """
    active_threads = config.get("active_threads", [])
    if not active_threads:
        return
    thread_id = active_threads[0]
    try:
        from thread_service import load_thread
        from thread_updater import update_thread, print_thread_summary
        thread = load_thread(thread_id)
        run_num = len(thread.get("history", [])) + 1
        updated = update_thread(thread_id, transcript, run_num)
        print_thread_summary(updated)
    except Exception as e:
        print(f"\n[thread_updater] Warning: could not auto-update thread '{thread_id}': {e}")
        print("  The conversation was completed. Run thread_updater.py manually if needed.")


def main() -> None:
    """Direct entry point: load config.json and run one conversation."""
    try:
        config = load_config()
    except FileNotFoundError:
        print("ERROR: config.json not found.")
        print("       Run from the project root, or copy config.json from demo/.")
        return
    except json.JSONDecodeError as e:
        print(f"ERROR: config.json contains invalid JSON: {e}")
        return

    debug = config.get("debug", False)
    transcript = run_conversation(config, debug=debug)

    if transcript:
        console = Console(verbose=debug)
        console.complete()
        console.transcript(transcript)
        if config.get("conversation_mode") == "deal":
            _auto_update_thread(config, transcript)


if __name__ == "__main__":
    main()
