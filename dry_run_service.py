"""
dry_run_service.py
-------------------
A MessagingService implementation that simulates email delivery without
sending anything over the network. Used when config.json has "dry_run": true.

HOW IT WORKS
------------
All outbound messages are printed to the terminal and deposited into a
module-level in-memory mailbox keyed by recipient address. get_latest()
reads from that same mailbox, so the full conversation flow:

    send() → poll() → reply() → poll() → follow-up()

runs identically to the real Gmail path — just without OAuth, network
traffic, or any risk of emailing real people.

SWITCHING TO REAL GMAIL
-----------------------
Set "dry_run": false in config.json.  No code changes are required.
main.py will swap DryRunService for GmailService automatically.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from messaging_service import Message, MessagingService

# ---------------------------------------------------------------------------
# Module-level shared mailbox
# Key: recipient email address.  Value: list of Message objects, oldest first.
# All DryRunService instances in the same process share this store, so a
# message "sent" by Bot A's instance is immediately visible to Bot B's instance.
# ---------------------------------------------------------------------------
_MAILBOXES: Dict[str, List[Message]] = {}
_message_counter = 0  # simple sequence for generating unique fake IDs


class DryRunService(MessagingService):
    """
    Simulated transport — prints emails to stdout, delivers them to an
    in-memory mailbox.

    Implements the full MessagingService interface so EmailAgent and the
    entire conversation loop run without modification.
    """

    def __init__(self, account_email: str, verbose: bool = False) -> None:
        """
        Args:
            account_email: The address this simulated account sends from.
                           Inbound messages are read from the mailbox keyed
                           by this address.
            verbose: When True, print a full email preview on each send.
                     When False (default), suppress all internal prints so
                     the demo terminal stays clean (transcript is shown at end).
        """
        self.account_id = account_email
        self.verbose = verbose

    # ------------------------------------------------------------------
    # MessagingService interface
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """No-op — no OAuth needed in dry-run mode."""
        if self.verbose:
            print(f"  [dry-run] Authenticated as {self.account_id!r} (simulated, no OAuth)")

    def send(
        self,
        recipient: str,
        body: str,
        subject: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        thread_id: Optional[str] = None,
        cc: Optional[List[str]] = None,
        reply_graph_id: Optional[str] = None,  # Outlook-only; ignored in dry-run
    ) -> str:
        """
        Print the email to stdout and deposit it in the recipient's mailbox.
        Returns a fake message ID.
        """
        global _message_counter
        _message_counter += 1
        fake_id = f"dry-run-{_message_counter}"

        msg = Message(
            id=fake_id,
            thread_id="dry-run-thread-1",
            subject=subject,
            sender=self.account_id,
            recipient=recipient,
            body=body,
            timestamp=datetime.now(timezone.utc),
            snippet=body[:120],
            message_id=f"<{fake_id}@dry-run.local>",
            in_reply_to=in_reply_to,
            references=references,
        )

        # ---- Print the simulated email (verbose/debug mode only) ----
        if self.verbose:
            sep = "=" * 60
            print()
            print(sep)
            print("  [DRY RUN] Email simulated — NOT sent")
            print(sep)
            print(f"  From:    {self.account_id}")
            print(f"  To:      {recipient}")
            if cc:
                print(f"  Cc:      {', '.join(cc)}")
            print(f"  Subject: {subject or '(no subject)'}")
            print("-" * 60)
            print(body)
            print(sep)
            print()

        # ---- Deposit into recipient mailbox ----
        _MAILBOXES.setdefault(recipient, []).append(msg)
        return fake_id

    def get_latest(
        self,
        from_sender: Optional[str] = None,
        after: Optional[datetime] = None,
    ) -> Optional[Message]:
        """
        Read the newest matching message from this account's in-memory mailbox.
        Applies the same sender / timestamp filters as GmailService.get_latest().
        """
        mailbox = _MAILBOXES.get(self.account_id, [])
        for msg in reversed(mailbox):
            if from_sender and msg.sender != from_sender:
                continue
            # Mirror the 5-second tolerance used in GmailService
            if after and msg.timestamp < after - timedelta(seconds=5):
                continue
            return msg
        return None
