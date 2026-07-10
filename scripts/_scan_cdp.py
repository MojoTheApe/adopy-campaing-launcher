"""Scan local ports for running Multilogin X browser profiles (CDP endpoints)."""
import requests

# MLX typically uses ports in these ranges
ranges = (
    list(range(35000, 35200)) +
    list(range(45000, 45200)) +
    list(range(10000, 10050)) +
    list(range(9220, 9240)) +
    list(range(36000, 36050)) +
    list(range(50000, 50050)) +
    list(range(40000, 40050))
)

found = []
for port in ranges:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=0.3)
        if r.status_code == 200:
            info = r.json()
            found.append((port, info.get("Browser", "?"), info.get("webSocketDebuggerUrl", "")))
    except Exception:
        pass

if found:
    for port, browser, ws in found:
        print(f"PORT {port}: {browser}  ws={ws[:60] if ws else '-'}")
else:
    print("No CDP endpoints found in scanned ranges")
    print("Try: netstat -ano | findstr ESTABLISHED | findstr chrome")
