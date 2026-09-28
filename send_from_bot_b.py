"""
send_from_bot_b.py
-------------------
One-off script: send a test message from Bot B (testbotuno1@gmail.com)
to Bot A (zhongrenhanart@gmail.com).

This reuses the same GmailService class from gmail_service.py — just
pointed at a different token file (token_bot_b.json) and a narrower
scope (send-only, since Bot B doesn't need to read mail here). This is
the pattern future bots/providers should follow: one GmailService
instance per account, never duplicated Gmail API logic.

Run with:
    python send_from_bot_b.py
"""

from gmail_service import GmailService, GmailServiceError

# Bot B's identity and the message to send.
SENDER_EMAIL = "testbotuno1@gmail.com"
RECIPIENT_EMAIL = "zhongrenhanart@gmail.com"
SUBJECT = "yooo"
BODY = "yooo from Bot B."

# Bot B only sends here, so it only requests the send scope.
SEND_ONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def main() -> None:
    """Authenticate as Bot B and send the test message to Bot A."""
    service = GmailService(token_file="token_bot_b.json", scopes=SEND_ONLY_SCOPES)

    try:
        service.authenticate()
    except GmailServiceError as e:
        print("ERROR: Authentication failed.")
        print(f"  {e}")
        print()
        print("Likely causes:")
        print("  - credentials.json is missing or invalid.")
        print(f"  - {SENDER_EMAIL} isn't added as a Test user in Google Cloud.")
        print("  - You logged into the wrong Google account in the browser.")
        return

    try:
        result = service.send_email(SENDER_EMAIL, RECIPIENT_EMAIL, SUBJECT, BODY)
    except GmailServiceError as e:
        print("ERROR: Could not send the email.")
        print(f"  {e}")
        return

    print("Success! Email sent.")
    print(f"  From: {SENDER_EMAIL}")
    print(f"  To:   {RECIPIENT_EMAIL}")
    print(f"  Gmail message ID: {result.get('id', 'unknown')}")


if __name__ == "__main__":
    main()
