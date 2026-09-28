"""
check_tokens.py
---------------
Prints which Google account each token file is authenticated as.
Run this locally (not in the sandbox) to verify token → account mapping.

Usage:
    python check_tokens.py
"""

import json
import warnings
warnings.filterwarnings("ignore")

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

EXPECTED = {
    "token_bot_a.json": "jiaheli0604@gmail.com",
    "token_bot_b.json": "testbotuno1@gmail.com",
    "token_bot_c.json": "jl7364@columbia.edu",
}


def check(token_file: str) -> str:
    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return svc.users().getProfile(userId="me").execute()["emailAddress"]


if __name__ == "__main__":
    all_ok = True
    for token_file, expected_email in EXPECTED.items():
        try:
            actual = check(token_file)
            ok = "✓" if actual == expected_email else "✗ WRONG"
            print(f"{token_file}: {actual}  {ok}")
            if actual != expected_email:
                print(f"         Expected: {expected_email}")
                all_ok = False
        except FileNotFoundError:
            print(f"{token_file}: NOT FOUND")
            all_ok = False
        except Exception as e:
            print(f"{token_file}: ERROR — {e}")
            all_ok = False

    print()
    if all_ok:
        print("All tokens correct.")
    else:
        print("Fix: delete the wrong token file(s) and re-run demo.py.")
        print("When the browser opens, make sure you're logged into the RIGHT Google account.")
        print("Tip: use a fresh Incognito window to avoid cross-account confusion.")
