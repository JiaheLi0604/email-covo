"""Quick debug: check what Graph API returns for Brian's inbox."""
import time
import requests
from outlook_service import OutlookService

svc = OutlookService(
    token_file="token_bot_b.json",
    account_email="brianwu@cratusasset.com",
    verbose=True,
)
svc.authenticate()

# First: who does this token actually belong to?
me = requests.get(
    "https://graph.microsoft.com/v1.0/me",
    headers={"Authorization": f"Bearer {svc._access_token}"},
).json()
print(f"\nToken identity: {me.get('mail') or me.get('userPrincipalName')}")
print(f"Display name:   {me.get('displayName')}\n")

# Then: poll inbox every 5s and show top 5 messages
FROM = "joeywan@cratusasset.com"
TOKEN = svc._access_token
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

print(f"\n=== 1. Search ALL mail for Joey's emails (filter by sender) ===")
r = requests.get(
    "https://graph.microsoft.com/v1.0/me/messages",
    headers={**HEADERS, "ConsistencyLevel": "eventual"},
    params={
        "$filter": f"from/emailAddress/address eq '{FROM}'",
        "$orderby": "receivedDateTime desc",
        "$top": "10",
        "$select": "id,subject,from,receivedDateTime,parentFolderId",
        "$count": "true",
    },
)
data = r.json()
print(f"Status: {r.status_code}  Total matching: {data.get('@odata.count', '?')}")
for m in data.get("value", []):
    print(f"  {m.get('receivedDateTime')}  folder={m.get('parentFolderId','?')[:12]}  {m.get('subject','')[:50]}")

print(f"\n=== 2. Check Junk folder directly ===")
r2 = requests.get(
    "https://graph.microsoft.com/v1.0/me/mailFolders/junkemail/messages",
    headers=HEADERS,
    params={"$orderby": "receivedDateTime desc", "$top": "5",
            "$select": "id,subject,from,receivedDateTime"},
)
junk = r2.json().get("value", [])
joey_junk = [m for m in junk if FROM.lower() in
             m.get("from", {}).get("emailAddress", {}).get("address", "").lower()]
print(f"Junk status: {r2.status_code}  Joey emails in Junk: {len(joey_junk)}")
for m in joey_junk:
    print(f"  {m.get('receivedDateTime')}  {m.get('subject','')[:50]}")

print(f"\n=== 3. Check inbox top 5 ===")
r3 = requests.get(
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
    headers=HEADERS,
    params={"$orderby": "receivedDateTime desc", "$top": "5",
            "$select": "id,subject,from,receivedDateTime"},
)
for m in r3.json().get("value", []):
    print(f"  {m.get('receivedDateTime')}  {m.get('from',{}).get('emailAddress',{}).get('address','?')}")
