#!/usr/bin/env python3
"""
demo.py — Primary entry point for EmailConvo.

Loads config.json (or a demo/ config via --config), shows a brief summary,
asks for confirmation before sending real emails, runs the conversation, and
saves a markdown transcript.

Usage:
    python demo.py                              # uses config.json
    python demo.py --config demo/weekly_operations.json

Config options that control this runner:
    "dry_run": true    — generate emails without sending; no OAuth needed
    "debug":   true    — show verbose API logs (Gmail send/inbox details)

To change the topic:
    Edit "weekly_topic" in config.json

To change how each bot writes:
    Edit prompts/bot_a.md and prompts/bot_b.md
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

from console import Console
from main import load_config, run_conversation


# ---------------------------------------------------------------------------
# Transcript saving
# ---------------------------------------------------------------------------

def _topic_slug(topic: str, max_words: int = 3) -> str:
    """Convert a topic string to a short filesystem-safe slug."""
    words = re.sub(r"[^a-z0-9\s]", "", topic.lower()).split()[:max_words]
    return "-".join(words) or "conversation"


def save_transcript(
    transcript: list,
    config: dict,
    out_dir: str = "transcripts",
) -> str:
    """
    Save the transcript to a dated markdown file.

    Returns the path of the saved file.
    File name format: transcripts/YYYY-MM-DD_HH-MM-<topic-slug>.md
    """
    os.makedirs(out_dir, exist_ok=True)

    now = datetime.now()
    topic = config.get("weekly_topic", "conversation")
    slug = _topic_slug(topic)
    filename = f"{now.strftime('%Y-%m-%d_%H-%M')}-{slug}.md"
    path = os.path.join(out_dir, filename)

    flow = config.get("conversation_flow", ["bot_a", "bot_b"])
    participants = [config.get(k, {}) for k in flow]

    participant_lines = [
        f"- **{p.get('name', k)}** ({p.get('role', '')}) — {p.get('email', '')}"
        for p, k in zip(participants, flow)
    ]

    lines = [
        "# EmailConvo — Conversation Transcript",
        "",
        f"**Date:** {now.strftime('%Y-%m-%d')}",
        f"**Time:** {now.strftime('%H:%M')}",
        f"**Topic:** {topic}",
        "",
        "---",
        "",
        "## Participants",
        "",
        *participant_lines,
        "",
        "---",
        "",
        "## Conversation",
        "",
    ]

    for i, (name, body) in enumerate(transcript):
        lines.append(f"### {name}")
        lines.append("")
        lines.append(body.strip())
        lines.append("")
        if i < len(transcript) - 1:
            lines.append("---")
            lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="EmailConvo demo runner — runs a conversation and saves the transcript."
    )
    parser.add_argument(
        "--config",
        default="config.json",
        metavar="PATH",
        help="Path to config JSON (default: config.json). "
             "Try demo/weekly_operations.json to use a preset.",
    )
    args = parser.parse_args()

    # --- Load config ---
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"ERROR: Config file not found: {args.config}")
        print("       Available demo configs: demo/weekly_operations.json, "
              "demo/maintenance_issue.json, demo/marketing_update.json, "
              "demo/budget_review.json")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"ERROR: {args.config} contains invalid JSON: {e}")
        sys.exit(1)

    dry_run = config.get("dry_run", False)
    debug = config.get("debug", False)
    topic = config.get("weekly_topic", "(no topic set)")
    flow = config.get("conversation_flow", ["bot_a", "bot_b"])
    bots = [config.get(k, {}) for k in flow]
    bot_names = " → ".join(b.get("name", k) for b, k in zip(bots, flow))

    # --- Brief pre-run summary and confirmation ---
    print()
    print("=" * 50)
    print("EmailConvo — Demo")
    print("=" * 50)
    print(f"\n  Config:  {args.config}")
    print(f"  Topic:   {topic[:55]}")
    print(f"  Mode:    {'DRY RUN — no real emails sent' if dry_run else 'LIVE — real emails will be sent'}")
    print(f"  Bots:    {bot_names}")
    print()

    try:
        if not dry_run:
            answer = input(
                "  Real emails will be sent.  Type 'yes' to continue: "
            ).strip().lower()
            if answer != "yes":
                print("  Cancelled.")
                return
        else:
            input("  Press Enter to start (Ctrl+C to cancel) … ")
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return

    print()

    # --- Run conversation ---
    transcript = run_conversation(config, debug=debug)

    if not transcript:
        # run_conversation already printed the error
        sys.exit(1)

    # --- Save transcript ---
    saved_path = save_transcript(transcript, config)

    # --- Print final section ---
    console = Console(verbose=debug)
    console.complete(saved=True)
    console.transcript(transcript)
    console.save_notice(saved_path)


if __name__ == "__main__":
    main()
