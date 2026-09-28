"""
outlook_service.py
------------------
Outlook/Microsoft Graph transport for EmailConvo.

Uses MSAL's device code flow for authentication (no local server needed).
Token is cached to disk — user only needs to authenticate once.

First-run flow:
  1. A code and URL are printed to the terminal.
  2. User visits https://microsoft.com/devicelogin and enters the code.
  3. User signs in with their Microsoft account (Outlook, Hotmail, Live, etc.)
  4. Token is saved to disk for all future runs.

Required pip packages:
    pip install msal requests
"""

import html as _html_module
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import msal
import requests

from messaging_service import Message, MessagingService


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite", "Mail.Send", "User.Read"]
CLIENT_ID = "3637aa14-8277-4ef6-b28c-fc82f40908f2"
AUTHORITY = "https://login.microsoftonline.com/common"


class OutlookServiceError(Exception):
    """Raised on authentication or API errors in OutlookService."""


class OutlookService(MessagingService):
    """
    Microsoft Graph / Outlook transport.

    Implements the same MessagingService interface as GmailService so
    EmailAgent can use Gmail and Outlook interchangeably — no other
    files need to know which transport is in use.
    """

    def __init__(self, token_file: str, account_email: str, verbose: bool = False):
        self._token_file = token_file
        self._account_email = account_email
        self._verbose = verbose
        self._access_token: Optional[str] = None
        self.account_id: Optional[str] = None
        self._cache = msal.SerializableTokenCache()
        self._app: Optional[msal.PublicClientApplication] = None
        # IDs of messages that existed before this session started.
        # get_latest() skips these so it only returns genuinely new arrivals.
        self._seen_ids: set = set()

    # ------------------------------------------------------------------
    # Token cache helpers
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        try:
            with open(self._token_file, "r") as f:
                self._cache.deserialize(f.read())
        except FileNotFoundError:
            pass

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            with open(self._token_file, "w") as f:
                f.write(self._cache.serialize())

    def _get_app(self) -> msal.PublicClientApplication:
        if self._app is None:
            self._load_cache()
            self._app = msal.PublicClientApplication(
                CLIENT_ID,
                authority=AUTHORITY,
                token_cache=self._cache,
            )
        return self._app

    # ------------------------------------------------------------------
    # MessagingService interface
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Authenticate using cached token or device code flow.

        Silent on subsequent runs (token refresh happens automatically).
        On first run, prints a short code and URL for the user to visit.
        """
        app = self._get_app()

        # 1. Try silent auth (cached / refreshed token)
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
            if result and "access_token" in result:
                self._access_token = result["access_token"]
                self.account_id = self._account_email
                self._save_cache()
                return

        # 2. Device code flow — first run or cache fully expired
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise OutlookServiceError(
                f"Failed to initiate device flow: "
                f"{flow.get('error_description', 'unknown error')}"
            )

        print()
        print("=" * 60)
        print(flow["message"])   # e.g. "To sign in, visit https://... and enter code XXXX-XXXX"
        print("=" * 60)
        print()

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise OutlookServiceError(
                f"Authentication failed: "
                f"{result.get('error_description', result.get('error', 'unknown'))}"
            )

        self._access_token = result["access_token"]
        self.account_id = self._account_email
        self._save_cache()
        self._warmup_seen_ids()

    def _warmup_seen_ids(self) -> None:
        """Snapshot current inbox IDs so get_latest() only returns new arrivals."""
        params = {"$top": "50", "$select": "id"}
        resp = requests.get(
            f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
            headers=self._headers(),
            params=params,
        )
        if resp.status_code == 200:
            for m in resp.json().get("value", []):
                self._seen_ids.add(m["id"])

    def _headers(self, prefer_text: bool = False) -> dict:
        h = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        if prefer_text:
            h["Prefer"] = 'outlook.body-content-type="text"'
        return h

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
        Send an HTML email via Microsoft Graph API.

        When reply_graph_id is provided (the exact Graph message ID returned
        by get_latest()), sends a true createReplyAll reply on that message —
        keeping all turns in the same Outlook conversation thread.

        Turn 1 (no reply_graph_id): POST /me/sendMail — new email.
        Turn 2+ (reply_graph_id set): createReplyAll on the exact received
        message ID — no conversationId search, no silent fallback.
        """
        # Build HTML body (same style as GmailService)
        paragraphs = _html_module.escape(body).split("\n\n")
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

        # --- Threaded reply path (Turn 2+) ---
        # Use the exact Graph message ID from get_latest(). No lookup needed.
        # If this fails, raise — never silently fall back to sendMail for a reply.
        if reply_graph_id:
            return self._send_as_reply(reply_graph_id, html_body, recipient, cc)

        # --- Turn 1: new email ---
        message: dict = {
            "subject": subject or "(no subject)",
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        }
        if cc:
            message["ccRecipients"] = [
                {"emailAddress": {"address": e}} for e in cc
            ]

        resp = requests.post(
            f"{GRAPH_BASE}/me/sendMail",
            headers=self._headers(),
            json={"message": message, "saveToSentItems": True},
        )
        if resp.status_code not in (200, 202):
            raise OutlookServiceError(
                f"sendMail failed ({resp.status_code}): {resp.text[:300]}"
            )
        return f"outlook-{uuid.uuid4()}"

    def _send_as_reply(
        self,
        graph_message_id: str,
        html_body: str,
        recipient: str,
        cc: Optional[List[str]],
    ) -> str:
        """
        Send a threaded reply using the Graph createReplyAll → patch → send flow.

        Step 1: createReplyAll — creates a draft in the same conversation thread,
                pre-addressed to all original recipients (To + CC).
        Step 2: PATCH the draft — overwrite body and route to the intended next recipient.
        Step 3: send the draft.

        Uses the exact Graph message ID from get_latest() — no conversationId lookup.
        """
        # Step 1: Create a replyAll draft
        resp = requests.post(
            f"{GRAPH_BASE}/me/messages/{graph_message_id}/createReplyAll",
            headers=self._headers(),
            json={},
        )
        if resp.status_code not in (200, 201):
            raise OutlookServiceError(
                f"createReply failed ({resp.status_code}): {resp.text[:200]}"
            )
        draft_id = resp.json()["id"]

        # Step 2: Update the draft body and recipients
        patch: dict = {
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        }
        if cc:
            patch["ccRecipients"] = [
                {"emailAddress": {"address": e}} for e in cc
            ]
        resp = requests.patch(
            f"{GRAPH_BASE}/me/messages/{draft_id}",
            headers=self._headers(),
            json=patch,
        )
        if resp.status_code not in (200, 201):
            raise OutlookServiceError(
                f"PATCH draft failed ({resp.status_code}): {resp.text[:200]}"
            )

        # Step 3: Send the draft
        resp = requests.post(
            f"{GRAPH_BASE}/me/messages/{draft_id}/send",
            headers=self._headers(),
        )
        if resp.status_code not in (200, 202):
            raise OutlookServiceError(
                f"send draft failed ({resp.status_code}): {resp.text[:200]}"
            )
        return f"outlook-reply-{uuid.uuid4()}"

    def get_latest(
        self,
        from_sender: Optional[str] = None,
        after: Optional[datetime] = None,
    ) -> Optional[Message]:
        """Poll inbox for the most recent message matching the given filters."""
        # Fetch recent inbox messages and filter client-side.
        # We rely on _seen_ids (snapshotted at auth time) instead of timestamps
        # to avoid Graph API eventual-consistency delays.
        params: dict = {
            "$orderby": "receivedDateTime desc",
            "$top": "25",
            "$select": (
                "id,subject,from,toRecipients,body,"
                "receivedDateTime,conversationId,internetMessageId"
            ),
        }

        resp = requests.get(
            f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
            headers=self._headers(prefer_text=True),
            params=params,
        )
        if resp.status_code != 200:
            if self._verbose:
                print(f"[outlook] query failed ({resp.status_code}): {resp.text[:200]}")
            return None

        msgs = resp.json().get("value", [])
        if self._verbose:
            print(f"[outlook] {self._account_email} — {len(msgs)} msg(s) returned"
                  + (f", filter={from_sender}" if from_sender else ""))

        for msg in msgs:
            msg_id = msg["id"]
            sender_addr = (
                msg.get("from", {}).get("emailAddress", {}).get("address", "")
            )
            ts = msg.get("receivedDateTime", "")

            if self._verbose:
                in_seen = msg_id in self._seen_ids
                sender_match = (not from_sender) or sender_addr.lower() == from_sender.lower()
                status = "SKIP(seen)" if in_seen else ("SKIP(sender)" if not sender_match else "MATCH")
                print(f"  {ts}  {sender_addr:<35} [{status}]")

            if msg_id in self._seen_ids:
                continue

            if from_sender and sender_addr.lower() != from_sender.lower():
                continue

            # Mark as seen so we don't return it again on the next poll
            self._seen_ids.add(msg_id)

            body_content = msg.get("body", {}).get("content", "")
            body_text = re.sub(r"<[^>]+>", "", body_content).strip()

            received_raw = msg.get("receivedDateTime", "")
            received = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))

            recipients = msg.get("toRecipients", [])
            recipient_addr = (
                recipients[0]["emailAddress"]["address"] if recipients else ""
            )

            return Message(
                id=msg_id,
                thread_id=msg.get("conversationId", ""),
                subject=msg.get("subject"),
                sender=sender_addr,
                recipient=recipient_addr,
                body=body_text,
                timestamp=received,
                snippet=body_text[:100],
                message_id=msg.get("internetMessageId"),
            )

        if self._verbose:
            print(f"  → no match found this poll")
        return None
