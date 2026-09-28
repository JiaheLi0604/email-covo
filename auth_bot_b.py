"""
auth_bot_b.py
-------------
One-time authorization script for Brian.

Brian: run this once on your own computer to generate token_bot_b.json,
then send that file back to Li. You won't need to do this again.

Requirements (run once in terminal):
    pip install google-auth-oauthlib

Usage:
    1. Put this file and credentials.json in the same folder.
    2. Run: python auth_bot_b.py
    3. A browser window will open — log in with brianwu@cratusasset.com and click Allow.
    4. Send the generated token_bot_b.json back to Li.
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

def main():
    print("Opening browser for authorization...")
    print("Make sure you log in with: brianwu@cratusasset.com\n")

    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)

    with open("token_bot_b.json", "w") as f:
        f.write(creds.to_json())

    print("\nDone! Send token_bot_b.json back to Li.")

if __name__ == "__main__":
    main()
