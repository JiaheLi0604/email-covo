"""
teams_service.py
----------------
Microsoft Teams transport for EmailConvo.

Uses MSAL device code flow — same pattern as OutlookService.
Token files are separate from email tokens so email is never affected.

Token files used:
    token_teams_bot_a.json   (Li)
    token_teams_bot_b.json   (Brian)
    token_teams_bot_c.json   (Joey Wan)

Required Azure App permissions (delegated):
    User.Read
    Chat.ReadWrite

Do NOT modify the Graph API query logic or filters without careful testing.
See feedback in project memory about why that broke email.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import msal
import requests

from messaging_service import Message, MessagingService


GRAPH_BASE  = "https://graph.microsoft.com/v1.0"
SCOPES      = ["Chat.ReadWrite", "User.Read"]
CLIENT_ID   = "3637aa14-8277-4ef6-b28c-fc82f40908f2"   # same Azure app as Outlook
AUTHORITY   = "https://login.microsoftonline.com/common"
AI_TAG      = "[AI-OPS]"   # prefix so real users know a message is AI-generated


class TeamsServiceError(Exception):
    """Raised on authentication or Graph API errors in TeamsService."""


class TeamsService(MessagingService):
    """
    Microsoft Teams group-chat transport.

    Implements the same MessagingService interface as OutlookService so the
    conversation engine works identically regardless of transport.

    send()       — posts to the configured Teams group chat as the
                   authenticated user; ignores subject, cc, threading headers
                   (Teams has no concept of these).
    get_latest() — polls the group chat for the most recent message from a
                   given sender that arrived after a given time.
    """

    def __init__(
        self,
        token_file: str,
        account_email: str,
        chat_id: str,
        verbose: bool = False,
    ):
        self._token_file    = token_file
        self._account_email = account_email
        self._chat_id       = chat_id
        self._verbose       = verbose
        self._access_token: Optional[str] = None
        self.account_id:    Optional[str] = None
        self._cache = msal.SerializableTokenCache()
        self._app:  Optional[msal.PublicClientApplication] = None
        # Message IDs seen before this session started — get_latest() skips them.
        self._seen_ids: set = set()

    # ------------------------------------------------------------------
    # Token cache helpers (identical pattern to OutlookService)
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

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type":  "application/json",
        }

    # ------------------------------------------------------------------
    # MessagingService interface
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Authenticate using cached token or device code flow.
        Uses separate token files from email — email tokens are never touched.
        """
        app = self._get_app()

        # Silent auth (cached token)
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
            if result and "access_token" in result:
                self._access_token = result["access_token"]
                self.account_id    = self._account_email
                self._save_cache()
                return

        # Device code flow — first run or cache expired
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise TeamsServiceError(
                f"Failed to initiate device flow: "
                f"{flow.get('error_description', 'unknown error')}"
            )

        print()
        print("=" * 60)
        print(flow["message"])
        print("=" * 60)
        print()

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise TeamsServiceError(
                f"Authentication failed: "
                f"{result.get('error_description', result.get('error', 'unknown'))}"
            )

        self._access_token = result["access_token"]
        self.account_id    = self._account_email
        self._save_cache()
        # Snapshot existing messages so get_latest() only returns new arrivals
        self._warmup_seen_ids()

    def _warmup_seen_ids(self) -> None:
        """Snapshot current chat message IDs at auth time."""
        if not self._chat_id:
            return
        resp = requests.get(
            f"{GRAPH_BASE}/chats/{self._chat_id}/messages",
            headers=self._headers(),
            params={"$top": "50"},
        )
        if resp.status_code == 200:
            for m in resp.json().get("value", []):
                self._seen_ids.add(m["id"])

    def send(
        self,
        recipient:    str,
        body:         str,
        subject:      Optional[str] = None,        # ignored — Teams has no subjects
        in_reply_to:  Optional[str] = None,        # ignored for MVP
        references:   Optional[str] = None,        # ignored
        thread_id:    Optional[str] = None,        # ignored
        cc:           Optional[List[str]] = None,  # ignored
    ) -> str:
        """Post a message to the Teams group chat as the authenticated user."""
        tagged = f"{AI_TAG} {body}"
        resp = requests.post(
            f"{GRAPH_BASE}/chats/{self._chat_id}/messages",
            headers=self._headers(),
            json={"body": {"contentType": "text", "content": tagged}},
        )
        if resp.status_code not in (200, 201):
            raise TeamsServiceError(
                f"sendMessage failed ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json().get("id", f"teams-{uuid.uuid4()}")

    def get_latest(
        self,
        from_sender: Optional[str] = None,
        after:       Optional[datetime] = None,
    ) -> Optional[Message]:
        """
        Poll the Teams group chat for the most recent matching message.
        Skips system events and AI-generated messages (those starting with AI_TAG).
        """
        resp = requests.get(
            f"{GRAPH_BASE}/chats/{self._chat_id}/messages",
            headers=self._headers(),
            params={"$top": "20"},
        )
        if resp.status_code != 200:
            if self._verbose:
                print(f"[teams] query failed ({resp.status_code}): {resp.text[:200]}")
            return None

        msgs = resp.json().get("value", [])
        if self._verbose:
            print(f"[teams] {self._account_email} — {len(msgs)} msg(s)"
                  + (f", filter={from_sender}" if from_sender else ""))

        for msg in msgs:
            # Skip system events (member added, call ended, etc.)
            if msg.get("messageType", "message") != "message":
                continue

            msg_id       = msg["id"]
            user_info    = (msg.get("from") or {}).get("user") or {}
            sender_email = (
                user_info.get("email")
                or user_info.get("userPrincipalName", "")
            )
            created_raw  = msg.get("createdDateTime", "")
            created      = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            body_content = (msg.get("body") or {}).get("content", "")

            if self._verbose:
                in_seen      = msg_id in self._seen_ids
                sender_ok    = (not from_sender) or sender_email.lower() == from_sender.lower()
                time_ok      = (not after) or created > after
                if in_seen:       status = "SKIP(seen)"
                elif not sender_ok: status = "SKIP(sender)"
                elif not time_ok:   status = "SKIP(time)"
                elif body_content.startswith(AI_TAG): status = "SKIP(ai-tag)"
                else:               status = "MATCH"
                print(f"  {created_raw}  {sender_email:<35} [{status}]")

            if msg_id in self._seen_ids:
                continue
            if from_sender and sender_email.lower() != from_sender.lower():
                continue
            if after and created <= after:
                continue
            # Skip our own AI-generated messages so bots don't reply to themselves
            if body_content.startswith(AI_TAG):
                self._seen_ids.add(msg_id)
                continue

            self._seen_ids.add(msg_id)
            body_text = re.sub(r"<[^>]+>", "", body_content).strip()

            return Message(
                id=msg_id,
                thread_id=self._chat_id,
                subject=None,
                sender=sender_email,
                recipient="",
                body=body_text,
                timestamp=created,
                snippet=body_text[:100],
            )

        if self._verbose:
            print("  → no match found this poll")
        return None
