"""
claude_service.py
------------------
Wraps the Anthropic Claude API to generate short, professional email
body text for one agent's turn in the weekly conversation.

This is the ONLY file that should import the `anthropic` package or
call the Claude API directly — the same rule GmailService follows for
the Gmail API and MemoryService follows for local storage.

Prompt engineering lives in prompts/*.md, not in this file. This module
only loads a template, fills in a handful of data placeholders (agent
identity, topic, conversation so far, memory), and sends the result to
Claude. Persona, tone, business rules, and output format are entirely
defined by the markdown template files — edit those, not this code, to
change how the bots write.
"""

import os
from string import Template
from typing import List, Optional

from anthropic import Anthropic
from dotenv import load_dotenv

# A current, generally-available Claude model. Change here if you want
# a different model — nothing else in the project needs to know about it.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Keep generated emails short — this is a hard ceiling on reply length,
# not a target. The prompt template itself asks for 3-6 sentences.
DEFAULT_MAX_TOKENS = 300

# Where prompt templates live, and which one to use if config.json
# doesn't specify a conversation_template.
PROMPTS_DIR = "prompts"
DEFAULT_TEMPLATE = "general"


class ClaudeServiceError(Exception):
    """Raised when ClaudeService fails to generate a message."""


class ClaudeService:
    """
    Generates short, professional email body text via the Claude API,
    using an external markdown prompt template to define persona/tone.

    Usage:
        claude = ClaudeService()
        body = claude.generate_email_body(
            agent_name="Bot A",
            agent_role="Operations Coordinator",
            agent_style="clear, concise, professional",
            sender="bota@example.com",
            receiver="botb@example.com",
            topic="Discuss this week's campground operations updates.",
            template_name="general",
        )
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        prompts_dir: str = PROMPTS_DIR,
    ) -> None:
        """
        Args:
            api_key: Anthropic API key. If not given, read from the
                     ANTHROPIC_API_KEY environment variable (loaded from
                     a .env file in the project root, if present).
            model: Claude model name to use for generation.
            prompts_dir: folder containing <template_name>.md prompt files.
        """
        load_dotenv()  # populates os.environ from .env if it exists; harmless otherwise
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ClaudeServiceError(
                "ANTHROPIC_API_KEY not found. Create a '.env' file in the project "
                "root (copy .env.example) and set ANTHROPIC_API_KEY=<your key>."
            )
        self.model = model
        self.prompts_dir = prompts_dir
        self._client = Anthropic(api_key=self.api_key)

    def generate_email_body(
        self,
        agent_name: str,
        agent_role: str,
        agent_style: str,
        sender: str,
        receiver: str,
        topic: str,
        template_name: str = DEFAULT_TEMPLATE,
        thread: Optional[List[tuple]] = None,
        previous_messages: Optional[List[str]] = None,
        memory_context: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """
        Generate one short email body for this agent's turn.

        Args:
            agent_name: e.g. "Li".
            agent_role: e.g. "intern".
            agent_style: short style description, e.g. "clear, concise, professional".
            sender: this agent's email address.
            receiver: the email address this message is being sent to.
            topic: this week's topic (used only to start the conversation).
            template_name: which prompts/<template_name>.md file to use.
            thread: full conversation so far as List[(name, body)], oldest first.
                    Preferred over previous_messages when available.
            previous_messages: fallback — prior bodies without names (legacy).
            memory_context: formatted string of recent stored history, background only.
            max_tokens: hard cap on response length.

        Returns:
            Plain email body text only (no subject line, no extra commentary).
        """
        template_text = self._load_template(template_name)
        memory_section = self._build_memory_section(memory_context)
        conversation_section, instruction = self._build_conversation_section(
            thread=thread, previous_messages=previous_messages
        )

        system_prompt = Template(template_text).safe_substitute(
            agent_name=agent_name,
            agent_role=agent_role,
            agent_style=agent_style,
            sender=sender,
            receiver=receiver,
            topic=topic,
            memory_section=memory_section,
            conversation_section=conversation_section,
            instruction=instruction,
        )

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system_prompt,
                # The template carries all of the actual instructions; this
                # user turn is just the trigger to act on them.
                messages=[{"role": "user", "content": "Write the email now."}],
            )
        except Exception as e:
            # Catches anthropic.APIError and any other SDK/network failure.
            raise ClaudeServiceError(f"Claude API request failed: {e}") from e

        text = self._extract_text(response).strip()
        if not text:
            raise ClaudeServiceError("Claude returned an empty response.")
        return self._normalize_paragraphs(text)

    def _load_template(self, template_name: str) -> str:
        """Load prompts/<template_name>.md, raising a helpful error if missing."""
        path = os.path.join(self.prompts_dir, f"{template_name}.md")
        if not os.path.exists(path):
            available = self._list_available_templates()
            raise ClaudeServiceError(
                f"Prompt template '{template_name}' not found at '{path}'. "
                f"Available templates: {available or '(none found)'}"
            )
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise ClaudeServiceError(f"Could not read prompt template '{path}': {e}") from e

    def _list_available_templates(self) -> List[str]:
        """List template names (without .md) found in prompts_dir, for error messages."""
        if not os.path.isdir(self.prompts_dir):
            return []
        return sorted(
            os.path.splitext(name)[0]
            for name in os.listdir(self.prompts_dir)
            if name.endswith(".md")
        )

    @staticmethod
    def _build_memory_section(memory_context: Optional[str]) -> str:
        """Wrap the pre-formatted memory string in a labelled block, or return "" if none."""
        if not memory_context:
            return ""
        return f"Recent conversation memory (background context only):\n\n{memory_context}"

    @staticmethod
    def _build_conversation_section(
        thread: Optional[List[tuple]] = None,
        previous_messages: Optional[List[str]] = None,
    ):
        """
        Build the conversation context block and instruction line.

        Prefers `thread` (List[(name, body)]) over `previous_messages` (List[str]).
        Named messages let Claude see exactly who said what and engage naturally.

        Returns:
            (conversation_section, instruction) tuple of strings.
        """
        if thread:
            lines = ["--- Email thread so far ---"]
            for name, body in thread:
                lines.append(f"\n{name} wrote:\n{body.strip()}")
            lines.append("\n--- End of thread ---")
            section = "\n".join(lines)
            instruction = (
                "The email thread above shows what your colleagues have said so far.\n"
                "Write your reply. Engage directly with their actual points — "
                "do not simply re-answer the original topic independently."
            )
            return section, instruction

        if previous_messages:
            # Legacy fallback: no names available
            lines = ["Conversation so far (oldest to newest):"]
            for i, message in enumerate(previous_messages, start=1):
                lines.append(f"[{i}] {message}")
            return "\n".join(lines), "Write the next reply in this conversation."

        return "", "Write the first email to open this week's conversation."

    @staticmethod
    def _extract_text(response) -> str:
        """Pull plain text out of an Anthropic Messages API response."""
        parts = []
        for block in getattr(response, "content", []):
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _normalize_paragraphs(text: str) -> str:
        """
        Remove hard line breaks that Claude inserts within paragraphs.

        Claude wraps output at ~70 characters (terminal convention), but
        email clients render plain text as-is, so those hard newlines show
        up as short ragged lines instead of full-width flowing text.

        Strategy: split on blank lines (paragraph separators), then within
        each paragraph replace single newlines with a space. Blank-line
        separators between paragraphs are preserved, so the overall
        structure (greeting / body / sign-off) stays intact.
        """
        paragraphs = text.split("\n\n")
        cleaned = []
        for para in paragraphs:
            # Replace single newlines within the paragraph with a space.
            # Multiple consecutive newlines (already split above) are gone.
            cleaned.append(para.replace("\n", " ").strip())
        return "\n\n".join(cleaned)
