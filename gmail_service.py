"""
gmail_service.py
-----------------
Gmail implementation of the MessagingService interface.

This module owns ALL direct interaction with the Gmail API.
Every other file in this project must go through GmailService (or another
MessagingService implementation) rather than calling googleapiclient
directly. This keeps the rest of the project transport-agnostic.

GmailService implements three abstract methods from MessagingService:
  - authenticate()  — OAuth2 token management (load / refresh / browser flow)
  - send()          — send a plain-text email; returns the Gmail message ID
  - get_latest()    — search inbox by sender and/or timestamp; return a Message

All other methods in this class are private helpers that support those three.
"""

import base64
import html as _html_module
import json
import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from messaging_service import Message, MessagingService


# Scopes requested for Bot A. Keep this list as narrow as each milestone
# requires:
#   - gmail.send     -> Milestone 1 (send email)
#   - gmail.readonly -> Milestone 2 (read inbox)
# Do not add gmail.modify or full Gmail access unless a future milestone
# actually needs to change/delete mail.
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailServiceError(Exception):
    """Base exception for any failure raised by GmailService."""


class GmailAuthError(GmailServiceError):
    """Raised specifically when authentication/login fails."""


class GmailService(MessagingService):
    """
    Gmail implementation of MessagingService.

    Exposes the three abstract transport methods:
        authenticate() — OAuth2 flow; sets self.account_id to the
                         authenticated Gmail address.
        send()         — sends a plain-text email; returns message ID.
        get_latest()   — searches inbox with structured filters;
                         returns a Message or None.

    Also retains the original helper methods from earlier milestones
    (send_email, read_inbox, get_newest_email, _parse_message, etc.)
    so that existing standalone scripts (send_from_bot_b.py) continue
    to work without changes.

    Usage:
        service = GmailService()
        service.authenticate()          # sets service.account_id
        msg = service.get_latest(from_sender="x@gmail.com", after=dt)
        service.send("y@gmail.com", "Hello", subject="Hi")
    """

    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token_bot_a.json",
        scopes: Optional[List[str]] = None,
        account_email: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        """
        Args:
            credentials_file: Path to the OAuth client JSON from Google Cloud.
            token_file: Path where this bot's OAuth token is cached.
            scopes: Gmail API scopes to request. Defaults to DEFAULT_SCOPES.
            account_email: The Gmail address this token belongs to. When
                provided, authenticate() uses it directly instead of calling
                getProfile (which requires gmail.readonly and would fail for
                send-only tokens like Bot B's).
            verbose: When True, print detailed API call logs (send headers,
                inbox query results, debug searches). When False (default),
                all internal prints are suppressed for clean demo output.
        """
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.scopes = scopes or DEFAULT_SCOPES
        self.account_id: Optional[str] = account_email  # pre-set if known
        self.verbose = verbose

        self._creds: Optional[Credentials] = None
        self._api = None  # the built googleapiclient "Resource" object
        self._debug_searched = False  # gate diagnostic searches to first failed poll

    # ------------------------------------------------------------------
    # MessagingService interface — authenticate()
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Ensure we have valid credentials and build the Gmail API client.
        Sets self.account_id to the authenticated Gmail address so that
        send() can use it as the MIME From header without needing it
        passed externally.

        Safe to call multiple times; only does real work the first time.
        """
        self._creds = self._load_or_refresh_credentials()
        self._api = build("gmail", "v1", credentials=self._creds)

        # Resolve account_id if not already provided via the constructor.
        # We call getProfile only when the scopes allow it (gmail.readonly
        # or broader). Send-only tokens (gmail.send only, e.g. Bot B) would
        # get a 403 from getProfile, so we skip it when account_email was
        # already supplied — main.py passes agent.email for every bot.
        if self.account_id is None:
            try:
                profile = self._api.users().getProfile(userId="me").execute()
                self.account_id = profile.get("emailAddress")
            except HttpError as e:
                raise GmailAuthError(
                    f"Authenticated but failed to read account profile: {e}\n"
                    f"Tip: pass account_email=<address> to GmailService() to "
                    f"avoid this call (required for send-only tokens)."
                ) from e

    def _ensure_authenticated(self) -> None:
        """Authenticate automatically if a caller forgot to do it first."""
        if self._api is None:
            self.authenticate()

    # ------------------------------------------------------------------
    # MessagingService interface — send()
    # ------------------------------------------------------------------

    def send(
        self,
        recipient: str,
        body: str,
        subject: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        thread_id: Optional[str] = None,
        cc: Optional[List[str]] = None,
        reply_graph_id: Optional[str] = None,  # Outlook-only; ignored by Gmail
    ) -> str:
        """
        Implements MessagingService.send().

        Sends a plain-text email from the authenticated account to
        `recipient` and returns the Gmail message ID of the sent item.
        When in_reply_to / references are supplied the outgoing message
        carries the correct RFC 2822 threading headers so Gmail groups
        the whole conversation into one thread.
        When cc is supplied, those addresses receive a copy.
        """
        self._ensure_authenticated()

        if self.verbose:
            print(f"    [send] from={self.account_id!r}")
            print(f"    [send] to={recipient!r}")
            if cc:
                print(f"    [send] cc={cc!r}")
            print(f"    [send] subject={subject!r}")

        result = self.send_email(
            sender=self.account_id,
            recipient=recipient,
            subject=subject or "(no subject)",
            body_text=body,
            in_reply_to=in_reply_to,
            references=references,
            thread_id=thread_id,
            cc=cc,
        )
        message_id = result.get("id", "")
        if self.verbose:
            print(f"    [send] Gmail message ID: {message_id!r}")

        # Verify full headers by reading back the sent message.
        if self.verbose and message_id:
            try:
                raw = (
                    self._api.users()
                    .messages()
                    .get(
                        userId="me",
                        id=message_id,
                        format="metadata",
                        metadataHeaders=[
                            "From", "To", "Subject", "Date",
                            "Message-ID", "Delivered-To",
                        ],
                    )
                    .execute()
                )
                hdrs = {
                    h["name"].lower(): h["value"]
                    for h in raw.get("payload", {}).get("headers", [])
                }
                print(f"    [send] Verified From:         {hdrs.get('from', '(missing)')!r}")
                print(f"    [send] Verified To:           {hdrs.get('to', '(missing)')!r}")
                print(f"    [send] Verified Subject:      {hdrs.get('subject', '(missing)')!r}")
                print(f"    [send] Verified Date:         {hdrs.get('date', '(missing)')!r}")
                print(f"    [send] Verified Message-ID:   {hdrs.get('message-id', '(missing)')!r}")
                print(f"    [send] Verified Delivered-To: {hdrs.get('delivered-to', '(missing)')!r}")
            except HttpError:
                pass  # verification is best-effort only

        return message_id

    # ------------------------------------------------------------------
    # MessagingService interface — get_latest()
    # ------------------------------------------------------------------

    def get_latest(
        self,
        from_sender: Optional[str] = None,
        after: Optional[datetime] = None,
    ) -> Optional[Message]:
        """
        Implements MessagingService.get_latest().

        Two-strategy approach to handle all delivery scenarios:

        Strategy A — labelIds listing (no search-index delay):
          Checks INBOX then SPAM using the label index, which is near-real-time
          for cross-domain mail (e.g. Columbia → Gmail). The INBOX label is
          applied quickly for incoming external mail.

        Strategy B — sender search (catches same-domain Gmail mail):
          For Gmail → Gmail delivery, the INBOX label can lag behind the search
          index: the email is findable via `from:` search before the INBOX label
          appears. A `from:{sender} newer_than:1h` search catches this case.

        Both strategies apply the same sender + timestamp filters in Python.
        """
        self._ensure_authenticated()

        if self.verbose:
            print(f"    [inbox] polling for from={from_sender!r} after={after}")

        def _check_refs(refs: list, tag: str) -> Optional[Message]:
            """Filter refs by sender/timestamp; return the first match or None."""
            if self.verbose:
                print(f"    [{tag}] found {len(refs)} message(s)")
            for ref in refs:
                meta = self._fetch_message_metadata(ref["id"])
                if self.verbose:
                    print(f"    [{tag}] id={meta['id']!r}  "
                          f"sender={meta['sender']!r}  ts={meta['timestamp']}")
                if from_sender and meta["sender"].lower() != from_sender.lower():
                    if self.verbose:
                        print(f"    [{tag}]   → skip: wrong sender")
                    continue
                # Allow 5s tolerance: email timestamps have only second precision,
                # while after_time carries microseconds.
                if after and meta["timestamp"] < after - timedelta(seconds=5):
                    if self.verbose:
                        print(f"    [{tag}]   → skip: too old")
                    continue
                if self.verbose:
                    print(f"    [{tag}]   → MATCH — fetching full message")
                parsed = self._fetch_and_parse_message(ref["id"])
                return self._dict_to_message(parsed)
            return None

        # --- Strategy A: label-based listing (real-time for cross-domain mail) ---
        for label in ("INBOX", "SPAM"):
            try:
                resp = (
                    self._api.users()
                    .messages()
                    .list(userId="me", labelIds=[label], maxResults=50,
                          includeSpamTrash=True)
                    .execute()
                )
            except HttpError as e:
                raise GmailServiceError(f"Gmail {label} listing failed: {e}") from e
            result = _check_refs(resp.get("messages", []), label.lower())
            if result:
                return result

        # --- Strategy B: sender search (catches Gmail→Gmail before INBOX label applied) ---
        if from_sender:
            try:
                resp = (
                    self._api.users()
                    .messages()
                    .list(
                        userId="me",
                        q=f"from:{from_sender} newer_than:1h",
                        maxResults=10,
                        includeSpamTrash=True,
                    )
                    .execute()
                )
            except HttpError:
                resp = {}
            result = _check_refs(resp.get("messages", []), "search")
            if result:
                return result

        if self.verbose:
            print(f"    [inbox] no matching message found this poll")
            if not self._debug_searched:
                self._debug_searched = True
                self._search_mailbox_debug(from_sender)
        return None

    # ------------------------------------------------------------------
    # Diagnostic helper (debug only — called once per inbox polling session)
    # ------------------------------------------------------------------

    def _search_mailbox_debug(self, from_sender: Optional[str]) -> None:
        """
        Run a set of broad queries against this account's entire mailbox
        (including spam/trash/promotions) to locate a missing message.
        Called once on the first failed poll so the output doesn't repeat.
        """
        queries: List[str] = []
        if from_sender:
            queries.append(f"from:{from_sender}")
            queries.append(f"in:anywhere from:{from_sender}")
        queries.append('subject:"Weekly Agent Conversation"')
        if self.account_id:
            queries.append(f"to:{self.account_id}")
        queries.append("newer_than:1d")

        print(f"\n    [debug] === diagnostic search in {self.account_id!r} mailbox ===")
        for q in queries:
            try:
                resp = (
                    self._api.users()
                    .messages()
                    .list(userId="me", q=q, maxResults=5, includeSpamTrash=True)
                    .execute()
                )
                msgs = resp.get("messages", [])
                print(f"    [debug] q={q!r}")
                print(f"    [debug]   → {len(msgs)} result(s)")
                for ref in msgs[:3]:
                    try:
                        meta = self._fetch_message_metadata(ref["id"])
                        print(
                            f"    [debug]   id={meta['id']!r}  "
                            f"sender={meta['sender']!r}  ts={meta['timestamp']}"
                        )
                    except GmailServiceError:
                        print(f"    [debug]   id={ref['id']!r}  (metadata fetch failed)")
            except HttpError as e:
                print(f"    [debug] q={q!r} → ERROR: {e}")
        print(f"    [debug] === end diagnostic ===\n")

    # ------------------------------------------------------------------
    # Internal OAuth / credential management
    # ------------------------------------------------------------------

    def _load_or_refresh_credentials(self) -> Credentials:
        """
        Load cached credentials, refresh them if expired, or run the
        browser OAuth flow if no valid credentials exist yet.

        Also detects the case where a previously saved token was granted
        a narrower set of scopes than we now need (e.g. an old
        send-only token, now that we also need gmail.readonly) and
        forces a fresh login in that case.
        """
        creds = self._load_cached_credentials()

        if creds and not creds.valid:
            creds = self._refresh_credentials(creds)

        if not creds or not creds.valid:
            creds = self._run_oauth_flow()
            self._save_credentials(creds)

        return creds

    def _load_cached_credentials(self) -> Optional[Credentials]:
        """
        Read token_file if it exists; return None if missing, corrupt, or
        if it was granted fewer scopes than we currently need.

        Important: Credentials.from_authorized_user_file(path, scopes)
        would silently overwrite creds.scopes with whatever `scopes` we
        pass in, which makes it impossible to detect a stale, narrower
        grant by inspecting creds.scopes afterwards. So we read the raw
        JSON first and compare against the *actually granted* scopes
        before building the Credentials object.
        """
        if not os.path.exists(self.token_file):
            return None
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Corrupt or unreadable token file -> treat as "no token".
            return None

        granted_scopes = set(data.get("scopes", []))
        if not set(self.scopes).issubset(granted_scopes):
            # The cached token predates a scope change (e.g. it only has
            # gmail.send, but we now also need gmail.readonly). Treat it
            # as unusable so _run_oauth_flow() requests fresh consent.
            return None

        try:
            return Credentials.from_authorized_user_info(data, self.scopes)
        except Exception:
            return None

    def _refresh_credentials(self, creds: Credentials) -> Optional[Credentials]:
        """Try to silently refresh an expired token using its refresh token."""
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_credentials(creds)
                return creds
            except Exception:
                return None
        return None

    def _run_oauth_flow(self) -> Credentials:
        """Launch the browser-based OAuth consent flow."""
        if not os.path.exists(self.credentials_file):
            raise GmailAuthError(
                f"'{self.credentials_file}' was not found. Download the OAuth "
                f"client JSON from Google Cloud Console and save it as "
                f"'{self.credentials_file}' in the project root."
            )
        try:
            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, self.scopes)
            return flow.run_local_server(port=0)
        except Exception as e:
            raise GmailAuthError(f"OAuth login failed: {e}") from e

    def _save_credentials(self, creds: Credentials) -> None:
        """Persist credentials (access + refresh token) to token_file."""
        with open(self.token_file, "w") as f:
            f.write(creds.to_json())

    # ------------------------------------------------------------------
    # Sending (original helper — still used by send() and standalone scripts)
    # ------------------------------------------------------------------

    def send_email(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body_text: str,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        thread_id: Optional[str] = None,
        cc: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Send a plain-text email and return the Gmail API's response dict
        (includes the new message's "id").

        When thread_id is supplied, the Gmail API places the message into
        that existing thread — keeping the full conversation in one place
        regardless of subject-line changes.

        Kept as a named method (rather than inlined into send()) so that
        standalone scripts like send_from_bot_b.py continue to work.
        """
        self._ensure_authenticated()
        message_body = self._build_raw_message(
            sender, recipient, subject, body_text,
            in_reply_to=in_reply_to, references=references, cc=cc,
        )
        # threadId tells Gmail which conversation to attach this message to.
        # This is the reliable way to thread — In-Reply-To/References alone
        # can fail when Gmail's search index hasn't caught up yet.
        if thread_id:
            message_body["threadId"] = thread_id
        try:
            return self._api.users().messages().send(userId="me", body=message_body).execute()
        except HttpError as e:
            raise GmailServiceError(f"Gmail API rejected the send request: {e}") from e

    @staticmethod
    def _build_raw_message(
        sender: str,
        recipient: str,
        subject: str,
        body_text: str,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
        cc: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """Build the base64url-encoded raw RFC 2822 message Gmail expects.

        Sends as text/html so that all recipients see full-width text.
        Plain-text QP encoding wraps at 76 chars and some clients (including
        Gmail for certain accounts) render those soft-breaks as visible hard
        breaks, producing narrow ragged-right columns. HTML sidesteps this:
        the browser ignores whitespace in the source and wraps text to the
        container width, so every recipient sees the same full-width layout.
        """
        # Convert plain text → minimal HTML.
        # \\n\\n = paragraph break → <p>; remaining \\n = <br> (sign-off, etc.)
        paragraphs = _html_module.escape(body_text).split("\n\n")
        html_parts = [
            f'<p style="margin:0 0 1em 0">{p.replace(chr(10), "<br>")}</p>'
            for p in paragraphs
        ]
        html_body = (
            '<html><body style="font-family:Arial,sans-serif;font-size:14px;'
            'line-height:1.6;color:#222;margin:0;padding:0">'
            + "".join(html_parts)
            + "</body></html>"
        )

        message = EmailMessage()
        message.set_content(html_body, subtype="html")
        message["To"] = recipient
        message["From"] = sender
        message["Subject"] = subject
        if cc:
            message["Cc"] = ", ".join(cc)
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        return {"raw": encoded}

    # ------------------------------------------------------------------
    # Reading — search and fetch
    # ------------------------------------------------------------------

    def search_emails(self, query: str = "", max_results: int = 10) -> List[Dict[str, Any]]:
        """
        Search the inbox using a Gmail query string and return parsed
        message dicts for all matching results (newest first).

        Args:
            query:       Gmail search query (e.g. "from:x@gmail.com after:1750000000").
                         Empty string returns all inbox messages.
            max_results: maximum number of messages to return.

        Returns:
            List of parsed message dicts (same shape as _parse_message returns).
        """
        self._ensure_authenticated()
        try:
            response = (
                self._api.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except HttpError as e:
            raise GmailServiceError(f"Gmail API search failed (query='{query}'): {e}") from e

        refs = response.get("messages", [])
        return [self._fetch_and_parse_message(ref["id"]) for ref in refs]

    def read_inbox(self, max_results: int = 10) -> List[Dict[str, Any]]:
        """
        Return lightweight message references (id, threadId) from INBOX,
        newest first. Kept for backwards compatibility with get_newest_email().
        """
        self._ensure_authenticated()
        try:
            response = (
                self._api.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
                .execute()
            )
        except HttpError as e:
            raise GmailServiceError(f"Gmail API rejected the inbox list request: {e}") from e
        return response.get("messages", [])

    def get_newest_email(self) -> Dict[str, Any]:
        """
        Fetch and parse the single newest message in the inbox.

        Returns a dict with keys: id, subject, from, to, date, snippet, body.
        Raises GmailServiceError if the inbox is empty.
        Kept for backwards compatibility with earlier milestones.
        """
        refs = self.read_inbox(max_results=1)
        if not refs:
            raise GmailServiceError("No emails found in the inbox.")
        return self._fetch_and_parse_message(refs[0]["id"])

    def _fetch_message_metadata(self, message_id: str) -> Dict[str, Any]:
        """
        Fetch only the From and Date headers for one message (no body).
        Much lighter than a full fetch — used by get_latest() to filter
        candidates before deciding whether to fetch the full message.

        Returns a dict with keys: id, thread_id, sender (bare address), timestamp.
        """
        try:
            raw = (
                self._api.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Date"],
                )
                .execute()
            )
        except HttpError as e:
            raise GmailServiceError(f"Failed to fetch metadata for '{message_id}': {e}") from e

        payload = raw.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        _, sender_addr = parseaddr(headers.get("from", ""))

        try:
            timestamp = parsedate_to_datetime(headers.get("date", ""))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        except Exception:
            timestamp = datetime.now(timezone.utc)

        return {
            "id": raw.get("id"),
            "thread_id": raw.get("threadId", ""),
            "sender": sender_addr or headers.get("from", ""),
            "timestamp": timestamp,
        }

    def _fetch_and_parse_message(self, message_id: str) -> Dict[str, Any]:
        """Fetch one message by ID (full detail) and parse it into a plain dict."""
        try:
            raw = self._api.users().messages().get(userId="me", id=message_id, format="full").execute()
        except HttpError as e:
            raise GmailServiceError(f"Failed to fetch message '{message_id}': {e}") from e
        return self._parse_message(raw)

    @staticmethod
    def _parse_message(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Extract the fields we care about from a raw Gmail API message resource."""
        payload = raw.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        return {
            "id": raw.get("id"),
            "thread_id": raw.get("threadId", ""),
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", "(unknown sender)"),
            "to": headers.get("to", "(unknown recipient)"),
            "date": headers.get("date", "(unknown date)"),
            "snippet": raw.get("snippet", ""),
            "body": GmailService._extract_plain_text_body(payload),
            # RFC 2822 threading headers — present on real emails, absent on drafts.
            "message_id": headers.get("message-id"),
            "in_reply_to": headers.get("in-reply-to"),
            "references": headers.get("references"),
        }

    @staticmethod
    def _extract_plain_text_body(payload: Dict[str, Any]) -> Optional[str]:
        """
        Walk a (possibly nested/multipart) message payload and return the
        first text/plain part's decoded content, or None if there isn't one.
        """
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return GmailService._decode_body_data(payload["body"]["data"])

        for part in payload.get("parts", []) or []:
            found = GmailService._extract_plain_text_body(part)
            if found is not None:
                return found
        return None

    @staticmethod
    def _decode_body_data(data: str) -> str:
        """Decode Gmail's base64url message body data into plain text."""
        decoded_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
        return decoded_bytes.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Conversion helper
    # ------------------------------------------------------------------

    @staticmethod
    def _dict_to_message(d: Dict[str, Any]) -> Message:
        """
        Convert a parsed Gmail dict (from _parse_message) into the
        transport-agnostic Message dataclass.

        Handles:
          - Date header parsing ("Tue, 30 Jun 2026 14:57:43 +0000" → datetime)
          - Extracting bare email addresses from "Display Name <addr>" headers
        """
        # Parse the RFC 2822 date header to a timezone-aware datetime.
        try:
            timestamp = parsedate_to_datetime(d.get("date", ""))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        except Exception:
            timestamp = datetime.now(timezone.utc)

        # Extract just the email address from "Display Name <addr>" format.
        _, sender_addr = parseaddr(d.get("from", ""))
        _, recipient_addr = parseaddr(d.get("to", ""))

        body = d.get("body") or ""
        return Message(
            id=d.get("id", ""),
            thread_id=d.get("thread_id", ""),
            subject=d.get("subject"),
            sender=sender_addr or d.get("from", ""),
            recipient=recipient_addr or d.get("to", ""),
            body=body,
            timestamp=timestamp,
            snippet=d.get("snippet") or body[:120],
            message_id=d.get("message_id"),
            in_reply_to=d.get("in_reply_to"),
            references=d.get("references"),
        )
