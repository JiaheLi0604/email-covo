"""
step1_gen_brian_url.py
----------------------
Li runs this to generate an authorization URL for Brian.
Brian does NOT need Python — just a browser.

Usage:
    python step1_gen_brian_url.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
flow.redirect_uri = "http://localhost:8080"

auth_url, _ = flow.authorization_url(
    access_type="offline",
    prompt="consent",
)

print()
print("=" * 70)
print("Send this URL to Brian (via WeChat / Slack / any message):")
print("=" * 70)
print()
print(auth_url)
print()
print("=" * 70)
print()
print("Instructions for Brian:")
print("  1. Open the URL above in his browser")
print("  2. Log in with brianwu@cratusasset.com and click Allow")
print("  3. He will see a 'This site can't be reached' error — that's normal")
print("  4. Ask him to copy the FULL URL from his browser's address bar")
print("     (it will start with http://localhost:8080/?code=...)")
print("  5. Send that URL back to you")
print()
print("Then run: python step2_save_brian_token.py")
