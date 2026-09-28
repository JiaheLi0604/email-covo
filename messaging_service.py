"""
messaging_service.py
---------------------
Defines the transport-agnostic communication interface used by all agents.

WHY THIS FILE EXISTS
--------------------
EmailAgent needs to send and receive messages without knowing whether it
is talking through Gmail, Outlook, Microsoft Teams, Slack, Discord, or
any other platform. This file provides the shared contract that makes
that possible:

  - `Message`          — a platform-neutral envelope for any received message.
  - `MessagingService` — an abstract base class (ABC) every transport
                         must implement.
  - `AgentInboxTimeoutError` — raised by EmailAgent.check_inbox() when
                         the inbox doesn't receive an expected message
                         within the allowed timeout.

ADDING A NEW PLATFORM
---------------------
1. Create a new file, e.g. `teams_service.py`.
2. Define `class TeamsService(MessagingService)`.
3. Implement `authenticate()`, `send()`, and `get_latest()`.
4. In `main.py`, swap `GmailService(...)` for `TeamsService(...)`.
   Nothing else in the project needs to change.

DESIGN NOTES
------------
- `get_latest()` takes structured filters (from_sender, after) rather
  than a platform-specific query string. Each implementation translates
  these into whatever its native API requires. EmailAgent never sees
  Gmail query syntax, Teams API parameters, etc.
- `send()` has no `sender` parameter. The service knows who it is
  authenticated as (stored in `account_id` after `authenticate()`).
  Platforms that embed a sender identity in the auth token (Gmail,
  Outlook, Slack, etc.) use that instead of being told externally.
- `Message.to_record()` converts a received message into the dict shape
  that MemoryService.save_email() expects, keeping the memory layer
  independent of transport details.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class AgentInboxTimeoutError(Exception):
    """
    Raised by EmailAgent.check_inbox() when no matching message arrives
    within the configured timeout window.

    This is NOT a transport error — it means delivery is just slow.
    The caller should wait a moment and retry the full run, or increase
    `inbox_timeout_seconds` in config.json.
    """


@dataclass
class Message:
    """
    A platform-neutral message envelope.

    All transport implementations (GmailService, TeamsService, etc.)
    convert their native message format into this dataclass before
    returning it to EmailAgent. This means agent logic never needs to
    know which platform a message came from.

    Fields:
        id          — unique message ID on the originating platform.
        thread_id   — thread/conversation ID; empty string if the
                      platform has no threading concept (Slack DMs,
                      Discord, etc.).
        subject     — message subject; None if the platform has no
                      subject concept (Teams, Slack, Discord channels).
        sender      — who sent this message: an email address, a
                      username, a handle — whatever the platform uses.
        recipient   — who it was sent to (email address, channel name,
                      group handle, etc.).
        body        — plain-text message body.
        timestamp   — when the message was received (UTC).
        snippet     — short preview string, used for memory logging.

    Threading fields (optional — populated by email transports):
        message_id  — RFC 2822 Message-ID header value of this message.
                      Used as In-Reply-To when replying.
        in_reply_to — RFC 2822 In-Reply-To header (parent message ID).
        references  — RFC 2822 References header (full ancestor chain).
                      Chat transports that have no threading concept
                      leave these as None.
    """

    id: str
    thread_id: str
    subject: Optional[str]
    sender: str
    recipient: str
    body: str
    timestamp: datetime
    snippet: str
    # Threading headers — optional so non-email transports need not set them.
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[str] = None

    def to_record(self) -> Dict[str, Any]:
        """
        Convert this message to the dict shape MemoryService.save_email()
        expects, so received messages can be logged without importing or
        knowing about the memory layer's internal schema.
        """
        record: Dict[str, Any] = {
            "id": self.id,
            "thread_id": self.thread_id,
            "subject": self.subject or "(no subject)",
            "from": self.sender,
            "to": self.recipient,
            "date": self.timestamp.isoformat(),
            "snippet": self.snippet,
            "body": self.body,
        }
        # Include threading headers when present so memory records are complete.
        if self.message_id:
            record["message_id"] = self.message_id
        if self.in_reply_to:
            record["in_reply_to"] = self.in_reply_to
        if self.references:
            record["references"] = self.references
        return record


class MessagingService(ABC):
    """
    Abstract base class that every communication transport must implement.

    EmailAgent depends only on this interface — it has no imports or
    references to Gmail, Teams, Slack, or any other concrete platform.

    Subclass contract:
        1. `authenticate()` must be called before `send()` or `get_latest()`.
        2. After `authenticate()` returns, `account_id` must be set to the
           authenticated account's address or handle (e.g. an email address
           for Gmail/Outlook, a username for Slack).
        3. `send()` uses `account_id` as the sender implicitly — no `sender`
           parameter is needed. The service already knows who it is.
        4. `get_latest()` returns None (not an error) when no matching message
           exists yet; callers poll for arrival rather than blocking.
    """

    # Set by subclasses during authenticate() to the address/handle that
    # outbound messages will be sent from (e.g. "bota@gmail.com" for Gmail,
    # "@BotA" for Slack). EmailAgent reads this to populate the memory record.
    account_id: Optional[str] = None

    @abstractmethod
    def authenticate(self) -> None:
        """
        Establish connection and load credentials.

        Must set self.account_id to the authenticated account's
        address or handle before returning.
        """

    @abstractmethod
    def send(
        self,
        recipient: str,
        body: str,
        subject: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        thread_id: Optional[str] = None,
        cc: Optional[List[str]] = None,
        reply_graph_id: Optional[str] = None,
    ) -> str:
        """
        Send a message and return the platform's message ID for the sent item.

        Args:
            recipient:   address or handle of the recipient.
            body:        plain-text message body.
            subject:     optional subject line (used by email platforms;
                         ignored by chat platforms like Slack/Teams/Discord).
            in_reply_to: RFC 2822 Message-ID of the message being replied to.
                         Email transports set this header; chat transports
                         may ignore it.
            references:  RFC 2822 References header value (space-separated
                         chain of ancestor Message-IDs). Email transports
                         set this header; chat transports may ignore it.
            thread_id:   platform thread/conversation ID to place this
                         message into. Gmail uses this to keep all replies
                         in one conversation. Chat transports may ignore it.
            cc:          optional list of addresses to CC. Email transports
                         add these to the Cc header; chat transports ignore it.

        Returns:
            The sent message's platform-specific ID (used for deduplication
            in MemoryService). Return an empty string if the platform does
            not provide one.
        """

    @abstractmethod
    def get_latest(
        self,
        from_sender: Optional[str] = None,
        after: Optional[datetime] = None,
    ) -> Optional["Message"]:
        """
        Return the single most recent message matching the given filters,
        or None if no matching message exists yet.

        Args:
            from_sender: only return messages from this address/handle.
                         None means any sender.
            after:       only return messages received after this UTC
                         datetime. None means no lower time bound.

        Returns:
            The newest matching Message, or None.

        Implementations translate these structured filters into whatever
        the underlying platform natively uses (Gmail query strings, Teams
        REST params, Slack conversation history args, etc.) so that
        callers remain platform-agnostic.
        """
