"""
find_chat.py
------------
One-time utility: authenticate as Li and list all Teams chats
so you can find the chat_id for the group chat with Brian and Joey.

Run once after Azure permissions are granted:
    python find_chat.py

Copy the ID of the Brian+Joey+Li group chat into config.json:
    "teams": { "chat_id": "19:xxx@thread.v2" }
"""
import requests
from teams_service import TeamsService, TeamsServiceError

svc = TeamsService(
    token_file="token_teams_bot_a.json",
    account_email="kli@cratusasset.com",
    chat_id="",   # empty — we're finding it
)

try:
    svc.authenticate()
    print(f"Authenticated as: {svc.account_id}\n")
except TeamsServiceError as e:
    print(f"Auth failed: {e}")
    raise SystemExit(1)

resp = requests.get(
    "https://graph.microsoft.com/v1.0/me/chats",
    headers={"Authorization": f"Bearer {svc._access_token}"},
    params={"$expand": "members", "$top": "20"},
)

if resp.status_code != 200:
    print(f"Failed to list chats ({resp.status_code}): {resp.text[:300]}")
    raise SystemExit(1)

chats = resp.json().get("value", [])
print(f"Found {len(chats)} chat(s):\n")
print("=" * 60)
for c in chats:
    chat_type = c.get("chatType", "?")
    topic     = c.get("topic") or "(no topic)"
    chat_id   = c.get("id", "")
    members   = [
        m.get("email") or m.get("displayName", "?")
        for m in c.get("members", [])
    ]
    print(f"Type:    {chat_type}")
    print(f"Topic:   {topic}")
    print(f"Members: {', '.join(members)}")
    print(f"ID:      {chat_id}")
    print()
