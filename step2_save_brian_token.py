"""
step2_save_brian_token.py
--------------------------
Brian 把浏览器地址栏的 URL 发回来后，Li 运行这个脚本保存 token。

用法：
    python step2_save_brian_token.py
"""

from urllib.parse import urlparse, parse_qs
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

print()
redirect_url = input("把 Brian 发回来的完整 URL 粘贴在这里：").strip()

parsed = urlparse(redirect_url)
params = parse_qs(parsed.query)
code = params.get("code", [None])[0]

if not code:
    print("\n错误：URL 里没有找到 code，请确认 Brian 复制的是完整地址栏 URL。")
    exit(1)

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
flow.redirect_uri = "http://localhost:8080"
flow.fetch_token(code=code)

with open("token_bot_b.json", "w") as f:
    f.write(flow.credentials.to_json())

print("\n完成！token_bot_b.json 已保存，现在可以运行 demo.py 了。")
