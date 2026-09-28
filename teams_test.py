"""
teams_test.py
-------------
Phase 1 test: authenticate as Li and post one message to the Teams group chat.
Run AFTER setting chat_id in config.json and confirming Azure permissions.

Usage:
    python teams_test.py
"""
import json
import sys
from teams_service import TeamsService, TeamsServiceError

# Load chat_id from config
with open("config.json") as f:
    config = json.load(f)

chat_id = config.get("teams", {}).get("chat_id", "")
if not chat_id:
    print("ERROR: Set teams.chat_id in config.json first (run find_chat.py).")
    sys.exit(1)

svc = TeamsService(
    token_file="token_teams_bot_a.json",
    account_email="kli@cratusasset.com",
    chat_id=chat_id,
    verbose=True,
)

try:
    svc.authenticate()
    print(f"Authenticated as: {svc.account_id}")

    msg_id = svc.send(recipient="", body="Hello from the Teams integration.")
    print(f"\nMessage posted! ID: {msg_id}")
    print("Check the Teams group chat — you should see:")
    print("  [AI-OPS] Hello from the Teams integration.")

except TeamsServiceError as e:
    print(f"\nERROR: {e}")
    sys.exit(1)
