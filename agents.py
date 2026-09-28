"""
agents.py
---------
Defines EmailAgent — a transport-agnostic autonomous communication agent.

DESIGN PRINCIPLE
----------------
EmailAgent depends only on MessagingService (the abstract interface),
never on GmailService or any other concrete transport. This means the
exact same agent can participate in a conversation over Gmail today and
over Teams, Slack, Outlook, or Discord in the future — main.py wires in
the right transport; EmailAgent never needs to change.

TRANSPORT-AGNOSTIC BEHAVIOR
----------------------------
Each agent can independently:
  1. open_conversation()  — generate the first message on a topic.
  2. check_inbox()        — poll its own inbox (via transport) until a
                            new message from an expected sender arrives.
  3. generate_reply()     — generate a reply from the ACTUAL received
                            message body, not from a Python variable passed
                            from another agent. Gmail (or any transport) is
                            the single source of truth for message content.
  4. send_message()       — send the generated text and log it to memory.

KEY ARCHITECTURAL CONSTRAINT
-----------------------------
generate_reply() receives a Message object returned by check_inbox().
That Message's body came from the transport (Gmail inbox), not from the
other agent's Python process. If Bot A and Bot B were running on two
separate computers talking only through Gmail, this code would work
identically — because neither agent ever reads the other's local variables.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from claude_service import ClaudeService
from memory_service import MemoryService
from messaging_service import AgentInboxTimeoutError, Message, MessagingService


@dataclass
class EmailAgent:
    """
    A single autonomous agent identity.

    Fields:
        name:     display name used in prompts and logs (e.g. "Bot A").
        email:    the agent's address on the current transport. For Gmail
                  this is an email address; for other platforms it could
                  be a username, handle, or channel ID.
        role:     short description of this agent's role — injected into
                  the prompt template as $agent_role.
        style:    short description of this agent's writing style —
                  injected into the prompt template as $agent_style.
        template: which prompts/<template>.md file governs this agent's
                  persona and tone. Defaults to "general". Set per-bot
                  in config.json (e.g. "bot_a", "bot_b") so each agent
                  can have its own editable prompt file.
    """

    name: str
    email: str
    role: str
    style: str
    template: str = "general"

    # ------------------------------------------------------------------
    # Opening a new conversation (no inbox read needed)
    # ------------------------------------------------------------------

    def open_conversation(
        self,
        claude: ClaudeService,
        topic: str,
        template_name: str,
        receiver_email: str,
        memory_context: Optional[str] = None,
    ) -> str:
        """
        Generate the opening message for a new conversation.

        No inbox read is needed here — this agent is initiating, so
        there is nothing yet to read. The receiver email is passed
        explicitly because we have no received Message to derive it from.

        Args:
            claude:         ready-to-use ClaudeService instance.
            topic:          the weekly topic to open a conversation about.
            template_name:  which prompts/<name>.md template to use.
            receiver_email: address/handle of the intended recipient.
            memory_context: formatted recent history string from
                            MemoryService.get_recent_context(), for background.

        Returns:
            Plain-text email body for the opening message.
        """
        return claude.generate_email_body(
            agent_name=self.name,
            agent_role=self.role,
            agent_style=self.style,
            sender=self.email,
            receiver=receiver_email,
            topic=topic,
            template_name=template_name,
            previous_messages=None,
            memory_context=memory_context,
        )

    # ------------------------------------------------------------------
    # Checking the inbox (polls until a message arrives or timeout)
    # ------------------------------------------------------------------

    def check_inbox(
        self,
        transport: MessagingService,
        from_sender: str,
        after_time: datetime,
        timeout: int = 60,
        poll_interval: int = 5,
    ) -> Message:
        """
        Poll the inbox via transport until a new message from `from_sender`
        arrives, then return it as a transport-agnostic Message.

        This is the mechanism that makes each agent truly independent.
        Instead of receiving message text as a Python variable from the
        other agent, the agent waits for the real delivery through the
        transport (Gmail inbox, Teams channel, etc.) and reads the actual
        received content.

        Args:
            transport:     the messaging transport to poll (e.g. GmailService).
            from_sender:   only accept messages from this address/handle.
            after_time:    only accept messages received after this UTC datetime,
                           preventing stale messages from earlier runs matching.
            timeout:       how many seconds to wait in total before giving up.
            poll_interval: how many seconds to wait between inbox checks.

        Returns:
            The first matching Message that arrives.

        Raises:
            AgentInboxTimeoutError: if no matching message arrives within
                `timeout` seconds. This is not a transport failure — it means
                delivery is slow. The caller should wait and retry, or increase
                `inbox_timeout_seconds` in config.json.
        """
        deadline = time.monotonic() + timeout
        attempt = 0

        while True:
            attempt += 1
            message = transport.get_latest(from_sender=from_sender, after=after_time)

            if message is not None:
                return message

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            sleep_duration = min(poll_interval, remaining)
            time.sleep(sleep_duration)

        raise AgentInboxTimeoutError(
            f"{self.name} timed out waiting for a message from {from_sender} "
            f"(checked {attempt} time(s) over {timeout}s). "
            f"Gmail delivery may be slow — re-run in a moment, or increase "
            f"'inbox_timeout_seconds' in config.json."
        )

    # ------------------------------------------------------------------
    # Generating a reply (reads from actual received message, not Python var)
    # ------------------------------------------------------------------

    def generate_reply(
        self,
        claude: ClaudeService,
        topic: str,
        template_name: str,
        received: Message,
        memory_context: Optional[str] = None,
        thread: Optional[List] = None,
    ) -> str:
        """
        Generate a reply to a message that was actually received via the
        transport (returned by check_inbox).

        CRITICAL: `received.body` is content that was read from the real
        inbox — it is NOT a Python variable passed directly from another
        agent's generate call. This is the line that makes agents behave
        like independent users: the reply is grounded in what actually
        arrived over the network.

        Args:
            claude:        ready-to-use ClaudeService instance.
            topic:         the weekly topic this conversation is about.
            template_name: which prompts/<name>.md template to use.
            received:      the Message object from check_inbox() — its
                           .body is used as the prior context for Claude.
            memory_context: formatted recent history string from
                            MemoryService.get_recent_context(), for background.
            thread:        full conversation so far as List[(name, body)],
                           oldest first. Lets Claude see who said what and
                           engage with specific prior points by name.

        Returns:
            Plain-text reply body.
        """
        effective_thread = thread if thread else [(received.sender, received.body)]
        return claude.generate_email_body(
            agent_name=self.name,
            agent_role=self.role,
            agent_style=self.style,
            sender=self.email,
            receiver=received.sender,
            topic=topic,
            template_name=template_name,
            thread=effective_thread,
            memory_context=memory_context,
        )

    # ------------------------------------------------------------------
    # Sending and logging
    # ------------------------------------------------------------------

    def send_message(
        self,
        transport: MessagingService,
        memory: MemoryService,
        recipient: str,
        subject: Optional[str],
        body: str,
        reply_to: Optional[Message] = None,
        cc: Optional[List[str]] = None,
    ) -> None:
        """
        Send `body` to `recipient` via the transport, then log the sent
        message to memory.

        When `reply_to` is supplied (a Message returned by check_inbox),
        RFC 2822 threading headers are computed here and forwarded to the
        transport so the whole conversation appears as one Gmail thread.

        Args:
            transport:  the messaging transport to send through.
            memory:     MemoryService instance for logging.
            recipient:  address/handle of the recipient.
            subject:    subject line (None is valid for chat platforms).
            body:       plain-text message body to send.
            reply_to:   the Message being replied to; if provided,
                        In-Reply-To and References headers are set so
                        Gmail groups all messages into one thread.
        """
        in_reply_to: Optional[str] = None
        references: Optional[str] = None
        thread_id: Optional[str] = None
        reply_graph_id: Optional[str] = None
        if reply_to and reply_to.message_id:
            in_reply_to = reply_to.message_id
            # Append parent's Message-ID to the existing References chain,
            # or start the chain with it when References is absent.
            if reply_to.references:
                references = f"{reply_to.references} {reply_to.message_id}"
            else:
                references = reply_to.message_id
        if reply_to and reply_to.thread_id:
            # Pass the original thread ID so Gmail places this reply into
            # the same conversation — not a new thread.
            thread_id = reply_to.thread_id
        if reply_to and reply_to.id:
            # Pass the exact Graph message ID returned by get_latest() so
            # Outlook can call createReplyAll on the precise received message
            # without a redundant conversationId lookup.
            reply_graph_id = reply_to.id

        message_id = transport.send(
            recipient=recipient,
            body=body,
            subject=subject,
            in_reply_to=in_reply_to,
            references=references,
            thread_id=thread_id,
            cc=cc,
            reply_graph_id=reply_graph_id,
        )

        # Build a Message from what we know — we don't re-fetch from the
        # transport just to get a thread_id; an empty string is fine for
        # outbound logging purposes.
        sent_msg = Message(
            id=message_id,
            thread_id="",
            subject=subject,
            sender=self.email,
            recipient=recipient,
            body=body,
            timestamp=datetime.now(timezone.utc),
            snippet=body[:120],
        )
        memory.save_email(sent_msg.to_record(), direction="outbound")

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, agent_config: Dict[str, Any]) -> "EmailAgent":
        """
        Build an EmailAgent from one of config.json's "bot_a"/"bot_b" dicts.

        Expects keys: name, email, role, style. Optional: template.
        """
        return cls(
            name=agent_config["name"],
            email=agent_config["email"],
            role=agent_config["role"],
            style=agent_config["style"],
            template=agent_config.get("template", "general"),
        )
