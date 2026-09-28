"""
console.py
----------
Clean, formatted terminal output for EmailConvo.

All user-facing display goes through Console so the presentation layer is
separated from conversation logic.  Debug/verbose output is gated behind
Console.log() — it only prints when verbose=True (set via "debug": true
in config.json).
"""

from typing import List, Optional, Tuple


class Console:
    WIDE = 50    # width of major section dividers  (===)
    THIN = 36    # width of minor transcript dividers (---)

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    # ------------------------------------------------------------------
    # Major sections
    # ------------------------------------------------------------------

    def header(self) -> None:
        print("=" * self.WIDE)
        print("EmailConvo")
        print("Weekly AI Email Conversation")
        print("=" * self.WIDE)

    def summary(
        self,
        topic: str,
        agents: list,
        rounds: int,
        dry_run: bool = False,
    ) -> None:
        """
        Args:
            agents: list of dicts with keys "name", "role", "email".
        """
        print()
        if dry_run:
            print("  [DRY RUN — no real emails sent]\n")
        print(f"Topic:\n  {topic}\n")
        for agent in agents:
            print(f"{agent['name']}:\n  {agent['role']}\n  {agent['email']}\n")
        print(f"Conversation rounds:  {rounds}")

    def auth_ok(self, name: str) -> None:
        print(f"  Authenticating {name} ... ✓")

    def round_header(self, n: int) -> None:
        print(f"\n{'-' * self.WIDE}")
        print(f"Round {n}")
        print("-" * self.WIDE)

    # ------------------------------------------------------------------
    # Per-agent progress steps
    # ------------------------------------------------------------------

    def agent_start(self, name: str, action: str) -> None:
        """Print agent name + first action."""
        print(f"\n  {name}")
        print(f"    {action}...")

    def agent_step(self, action: str) -> None:
        """Print a follow-on action line for the current agent."""
        print(f"    {action}...")

    def agent_ok(self, label: str = "") -> None:
        """Print a ✓ confirmation with an optional label."""
        print(f"    {label} ✓" if label else "    ✓")

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def complete(self, saved: bool = False) -> None:
        print(f"\n{'-' * self.WIDE}")
        print("Conversation complete.")
        if saved:
            print("Conversation saved.")
        print("=" * self.WIDE)

    # ------------------------------------------------------------------
    # Transcript display
    # ------------------------------------------------------------------

    def transcript(self, entries: List[Tuple[str, str]]) -> None:
        print(f"\n{'=' * self.WIDE}")
        print("Conversation Transcript")
        print("=" * self.WIDE)
        for i, (name, body) in enumerate(entries):
            print(f"\n{name}")
            for line in body.strip().splitlines():
                print(f"  {line}")
            if i < len(entries) - 1:
                print(f"\n{'-' * self.THIN}")
        print(f"\n{'=' * self.WIDE}")

    def save_notice(self, path: str) -> None:
        print(f"\nTranscript saved → {path}")

    # ------------------------------------------------------------------
    # Errors and verbose/debug output
    # ------------------------------------------------------------------

    def error(self, msg: str) -> None:
        print(f"\nERROR: {msg}")

    def log(self, *args) -> None:
        """Print only in verbose/debug mode."""
        if self.verbose:
            print(*args)
