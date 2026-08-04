import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request


base_url = "http://127.0.0.1:18767"
token = os.environ["ACCOUNT_WORKBENCH_IMPORT_TOKEN"]
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)
opener.open(
    f"{base_url}/access?token={urllib.parse.quote(token, safe='')}", timeout=20
).read()

with opener.open(f"{base_url}/api/inbox/codes", timeout=20) as response:
    inbox = json.load(response)
aliases = [
    str(item.get("hmeAddress") or "").strip().lower()
    for item in inbox.get("items", [])
    if str(item.get("hmeAddress") or "").strip().lower().endswith("@icloud.com")
]
if not aliases:
    raise SystemExit("PORTAL_SMOKE_NO_ALIAS")

request = urllib.request.Request(
    f"{base_url}/api/code/latest",
    data=json.dumps({"email": aliases[0]}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with opener.open(request, timeout=60) as response:
        result = json.load(response)
except urllib.error.HTTPError as error:
    raise SystemExit(f"PORTAL_SMOKE_HTTP_{error.code}") from error
if not result.get("ok") or not str(result.get("code") or "").strip():
    raise SystemExit("PORTAL_SMOKE_INVALID_RESPONSE")
print("PORTAL_LIVE_ALIAS_OK")
