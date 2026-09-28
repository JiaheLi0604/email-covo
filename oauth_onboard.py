#!/usr/bin/env python3
"""
oauth_onboard.py
----------------
Simple local web server for remote Gmail OAuth authorization.

Lets someone on a remote computer authorize their Gmail account for this
project, with the token saved locally in your project folder.

Usage (local):
    python oauth_onboard.py
    Open http://localhost:5000

Usage (remote via ngrok):
    python oauth_onboard.py
    ngrok http 5000
    # Then set the ngrok URL as BASE_URL and restart:
    BASE_URL=https://abc123.ngrok.io python oauth_onboard.py

IMPORTANT — before sharing with Brian:
    1. Add Brian's Gmail to Google Cloud Console → Test users
    2. Add <ngrok-url>/oauth2callback to Google Cloud Console →
       Credentials → your OAuth client → Authorized redirect URIs
"""

import os
import sys

# Allow OAuth over HTTP for local testing and ngrok prototype use.
# Remove this line if you switch to a proper HTTPS server in production.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from flask import Flask, redirect, request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
CREDENTIALS_FILE = "credentials.json"

# Override BASE_URL when running behind ngrok:
#   BASE_URL=https://abc123.ngrok.io python oauth_onboard.py
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")

TOKEN_FILES = {
    "bot_a": "token_bot_a.json",
    "bot_b": "token_bot_b.json",
}
BOT_LABELS = {
    "bot_a": "Bot A",
    "bot_b": "Bot B",
}

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.urandom(24)


def _redirect_uri() -> str:
    return f"{BASE_URL}/oauth2callback"


def _make_flow(state: str = None) -> Flow:
    return Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=_redirect_uri(),
        state=state,
    )


def _fix_callback_url(url: str) -> str:
    """
    When running behind ngrok the BASE_URL is https:// but Flask sees the
    request as http:// (ngrok strips TLS before forwarding). Fix the scheme
    so google-auth-oauthlib can verify the callback URL.
    """
    if BASE_URL.startswith("https://") and url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>EmailConvo — Gmail Authorization</title>
  <style>
    body { font-family: sans-serif; max-width: 520px; margin: 60px auto; padding: 0 20px; color: #222; }
    h1   { font-size: 1.4rem; margin-bottom: 6px; }
    p    { color: #555; margin-bottom: 28px; }
    .btn { display: inline-block; padding: 12px 24px; border-radius: 6px;
           background: #1a73e8; color: #fff; text-decoration: none;
           font-size: 0.95rem; margin-right: 12px; }
    .btn:hover { background: #1558b0; }
  </style>
</head>
<body>
  <h1>EmailConvo — Gmail Authorization</h1>
  <p>Connect the Gmail accounts for each bot.<br>
     Click the button that matches your account.</p>
  <a class="btn" href="/connect/bot_a">Connect Bot A Gmail</a>
  <a class="btn" href="/connect/bot_b">Connect Bot B Gmail</a>
</body>
</html>
"""


@app.route("/connect/<bot>")
def connect(bot: str):
    if bot not in TOKEN_FILES:
        return "Unknown bot — use /connect/bot_a or /connect/bot_b", 400

    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",   # always ask for a refresh token
        state=bot,          # carried through the flow so we know which token to save
    )
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    bot = request.args.get("state", "")
    if bot not in TOKEN_FILES:
        return "Invalid OAuth state — please restart the flow.", 400

    flow = _make_flow(state=bot)

    try:
        flow.fetch_token(authorization_response=_fix_callback_url(request.url))
    except Exception as e:
        return f"OAuth failed: {e}", 500

    creds = flow.credentials

    # Save token
    token_file = TOKEN_FILES[bot]
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    # Resolve the authorized email address (nice to show on success page)
    email = ""
    try:
        svc = build("gmail", "v1", credentials=creds)
        email = svc.users().getProfile(userId="me").execute().get("emailAddress", "")
    except Exception:
        pass

    label = BOT_LABELS[bot]
    print(f"  ✓ {label} authorized{f' ({email})' if email else ''} → {token_file}")

    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Gmail Connected</title>
  <style>
    body {{ font-family: sans-serif; max-width: 480px; margin: 80px auto; padding: 0 20px; color: #222; }}
    .check {{ font-size: 2.5rem; }}
    h1 {{ font-size: 1.3rem; margin: 10px 0 6px; }}
    p  {{ color: #555; }}
  </style>
</head>
<body>
  <div class="check">✅</div>
  <h1>Gmail connected successfully.</h1>
  <p><strong>{label}</strong>{f' ({email})' if email else ''} is now authorized.</p>
  <p>You can close this page.</p>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _check_credentials():
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"ERROR: {CREDENTIALS_FILE} not found.")
        print("       Download it from Google Cloud Console → Credentials")
        print("       and save it in this folder.")
        sys.exit(1)


def _print_instructions():
    print()
    print("=" * 60)
    print("  EmailConvo — Remote Gmail OAuth Onboarding")
    print("=" * 60)
    print()
    print(f"  Server:       {BASE_URL}")
    print(f"  Callback URI: {BASE_URL}/oauth2callback")
    print()
    print("  ── Local use ─────────────────────────────────────────")
    print("  Open http://localhost:5000 in your browser.")
    print()
    print("  ── Remote use (ngrok) ────────────────────────────────")
    print("  1. In another terminal:  ngrok http 5000")
    print("  2. Copy the ngrok URL   (e.g. https://abc123.ngrok.io)")
    print("  3. In Google Cloud Console → Credentials → your OAuth")
    print("     client, add this Authorized redirect URI:")
    print("       <ngrok-url>/oauth2callback")
    print("  4. Restart this script with:")
    print("       BASE_URL=https://abc123.ngrok.io python oauth_onboard.py")
    print("  5. Send Brian the ngrok URL.")
    print("  6. Brian clicks Connect Bot B Gmail and signs in.")
    print("  7. token_bot_b.json is saved here automatically.")
    print()
    print("  Press Ctrl+C to stop.")
    print()


if __name__ == "__main__":
    _check_credentials()
    _print_instructions()
    app.run(host="0.0.0.0", port=5000, debug=False)
