"""Check which account each token file is actually authenticated as."""
import requests
from outlook_service import OutlookService

bots = [
    ("bot_a", "token_bot_a.json", "kli@cratusasset.com"),
    ("bot_b", "token_bot_b.json", "brianwu@cratusasset.com"),
    ("bot_c", "token_bot_c.json", "joeywan@cratusasset.com"),
]

for bot_key, token_file, expected_email in bots:
    try:
        svc = OutlookService(token_file=token_file, account_email=expected_email)
        svc.authenticate()
        me = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {svc._access_token}"},
        ).json()
        actual = me.get("mail") or me.get("userPrincipalName", "?")
        match = "✓" if actual.lower() == expected_email.lower() else "✗ MISMATCH"
        print(f"{bot_key}: expected={expected_email}  actual={actual}  {match}")

        # Also show the 3 most recent emails in inbox
        r = requests.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
            headers={"Authorization": f"Bearer {svc._access_token}"},
            params={"$orderby": "receivedDateTime desc", "$top": "3",
                    "$select": "receivedDateTime,from,subject"},
        ).json()
        for m in r.get("value", []):
            sender = m.get("from", {}).get("emailAddress", {}).get("address", "?")
            print(f"   {m.get('receivedDateTime','')}  {sender:<35} {m.get('subject','')[:30]}")
        print()

    except Exception as e:
        print(f"{bot_key}: ERROR — {e}\n")
