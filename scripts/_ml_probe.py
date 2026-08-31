"""Try MLX workspace/profile API using shard info from token."""
import os, sys, json, base64
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
from config.paths import load_env
load_env()
import requests

TOKEN = os.environ.get("MULTILOGIN_AUTOMATION_TOKEN_A", "")
H = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

# Decode JWT for workspace/shard info
parts = TOKEN.split(".")
payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4) if len(parts) >= 2 else ""
payload = json.loads(base64.b64decode(payload_b64)) if payload_b64 else {}
workspace_id = payload.get("workspaceID", "")
print(f"workspaceID: {workspace_id}")

# Try MLX-specific profile listing endpoints
probes = [
    f"https://launcher.mlx.yt:45001/api/v2/profile",
    f"https://launcher.mlx.yt:45001/api/v2/workspace/{workspace_id}/profile",
    f"https://launcher.mlx.yt:45001/api/v1/workspace/{workspace_id}/profile",
    # Try without SSL
    f"http://launcher.mlx.yt:45001/api/v2/profile",
    # Try the launcher discovery endpoint
    f"https://launcher.mlx.yt:45001/",
    f"https://launcher.mlx.yt:45001/api",
    f"https://launcher.mlx.yt:45001/api/v2",
    f"https://launcher.mlx.yt:45001/api/v2/profile/f/",
    f"https://launcher.mlx.yt:45001/api/v1/profile/list",
    f"https://launcher.mlx.yt:45001/api/v2/browser",
]

for url in probes:
    try:
        r = requests.get(url, headers=H, timeout=8, verify=False)
        if r.status_code < 500:
            print(f"{r.status_code}  {url}  =>  {r.text[:100]}")
    except Exception as e:
        print(f"ERR  {url}  =>  {type(e).__name__}")
