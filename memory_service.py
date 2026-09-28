"""
memory_service.py
------------------
Local, file-based memory layer for EmailConvo.

This module owns ALL reading and writing of conversation history. No
other file should touch history/conversation_history.json directly —
the same rule that GmailService applies to the Gmail API applies here:
go through MemoryService.

This is intentionally simple (plain JSON on disk). A future milestone
could swap this for a real database without changing how main.py (or
any future agent code) calls it, as long as the MemoryService interface
stays the same.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List


class MemoryServiceError(Exception):
    """Raised when the memory layer fails to read or write history."""


class MemoryService:
    """
    Stores and retrieves email conversation history as JSON on disk.

    Responsibilities:
        - Create the history folder + history file if missing.
        - Load all stored email records.
        - Append a new email record, skipping duplicates by message_id.
        - Return the last N records.
    """

    def __init__(
        self,
        history_dir: str = "history",
        history_filename: str = "conversation_history.json",
    ) -> None:
        """
        Args:
            history_dir: Folder where conversation history is stored.
            history_filename: Name of the JSON file inside history_dir.
        """
        self.history_dir = history_dir
        self.history_path = os.path.join(history_dir, history_filename)
        self._ensure_storage_exists()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_storage_exists(self) -> None:
        """Create the history folder and an empty history file if missing."""
        os.makedirs(self.history_dir, exist_ok=True)
        if not os.path.exists(self.history_path):
            self._write_records([])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_all(self) -> List[Dict[str, Any]]:
        """Return every stored email record, oldest first."""
        return self._read_records()

    def save_email(self, email: Dict[str, Any], direction: str, source: str = "gmail") -> bool:
        """
        Append one email as a conversation record, unless a record with
        the same message_id already exists.

        Args:
            email: dict shaped like GmailService.get_newest_email()'s
                   output (keys: id, thread_id, subject, from, to, date,
                   snippet, body).
            direction: "inbound" or "outbound".
            source: where this email came from. Defaults to "gmail".

        Returns:
            True if a new record was appended.
            False if a record with this message_id already existed
            (nothing was written — this is how duplicates are prevented).
        """
        if direction not in ("inbound", "outbound"):
            raise MemoryServiceError(f"direction must be 'inbound' or 'outbound', got '{direction}'")

        message_id = email.get("id") or email.get("message_id")
        if not message_id:
            raise MemoryServiceError("Email is missing an 'id'/'message_id'; cannot deduplicate or save.")

        records = self._read_records()
        if self._find_index_by_message_id(records, message_id) is not None:
            return False

        record = {
            "message_id": message_id,
            "thread_id": email.get("thread_id", email.get("threadId", "")),
            "subject": email.get("subject", ""),
            "from": email.get("from", ""),
            "to": email.get("to", ""),
            "date": email.get("date", ""),
            "snippet": email.get("snippet", ""),
            "body": email.get("body", ""),
            "direction": direction,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        records.append(record)
        self._write_records(records)
        return True

    def get_last_n(self, n: int = 5) -> List[Dict[str, Any]]:
        """Return up to the last n stored records, oldest-to-newest order."""
        records = self._read_records()
        return records[-n:]

    def get_recent_context(
        self,
        limit: int = 10,
        body_preview_chars: int = 300,
    ) -> str:
        """
        Return a formatted string summarising the most recent stored messages,
        ready to drop directly into a Claude prompt as background context.

        Each record is shown as:

            [YYYY-MM-DD] sender@example.com → recipient@example.com
            Subject: ...
            Message: first 300 chars of body...

        Returns an empty string when there are no records yet.

        Args:
            limit:             Maximum number of recent records to include.
            body_preview_chars: How many body characters to show per record.
                               Enough for Claude to understand the gist
                               without bloating the prompt.
        """
        records = self.get_last_n(limit)
        if not records:
            return ""

        blocks: List[str] = []
        for r in records:
            # Use the first 10 chars of date ("2026-07-01T…" → "2026-07-01")
            raw_date = r.get("date") or r.get("created_at", "")
            date_str = raw_date[:10] if raw_date else "unknown date"

            sender = r.get("from", "(unknown)")
            recipient = r.get("to", "(unknown)")
            subject = r.get("subject", "(no subject)")

            # Prefer full body; fall back to snippet if body is missing.
            body = r.get("body") or r.get("snippet", "")
            preview = body[:body_preview_chars].strip()
            if len(body) > body_preview_chars:
                preview += "..."

            block = (
                f"[{date_str}] {sender} → {recipient}\n"
                f"Subject: {subject}\n"
                f"Message: {preview}"
            )
            blocks.append(block)

        return "\n\n".join(blocks)

    def has_message(self, message_id: str) -> bool:
        """Return True if a record with this message_id is already stored."""
        return self._find_index_by_message_id(self._read_records(), message_id) is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_index_by_message_id(records: List[Dict[str, Any]], message_id: str):
        """Return the list index of a record with this message_id, or None."""
        for i, record in enumerate(records):
            if record.get("message_id") == message_id:
                return i
        return None

    def _read_records(self) -> List[Dict[str, Any]]:
        """Read and parse the JSON history file. Missing file -> empty list."""
        if not os.path.exists(self.history_path):
            return []
        try:
            with open(self.history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise MemoryServiceError(f"Could not parse '{self.history_path}': {e}") from e
        except OSError as e:
            raise MemoryServiceError(f"Could not read '{self.history_path}': {e}") from e

        if not isinstance(data, list):
            raise MemoryServiceError(f"'{self.history_path}' does not contain a JSON list of records.")
        return data

    def _write_records(self, records: List[Dict[str, Any]]) -> None:
        """Write the full record list back to disk as readable JSON."""
        try:
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
        except OSError as e:
            raise MemoryServiceError(f"Could not write to '{self.history_path}': {e}") from e
