"""Render one complete snapshot; failed updates preserve last-known-good data."""
from __future__ import annotations
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .model import payload

WEB = Path(__file__).with_name("web")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".tracker-", delete=False) as f:
        f.write(content)
        temp = Path(f.name)
    temp.chmod(0o644)
    temp.replace(path)


def render(data):
    template = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    revision = hashlib.sha256((template + css + js).encode()).hexdigest()
    result = dict(data, revision=revision)
    digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    result["digest"] = digest
    # JSON in a script element must not allow issue text to close that element.
    encoded = json.dumps(result, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = template.replace("__STYLE__", css).replace("__DATA__", encoded).replace("__APP__", js)
    return page, result


def sync(api, config, output, *, allow_empty=False, queue=None):
    output = Path(output)
    try:
        previous = json.loads((output / "status.json").read_text())
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    error = "fetch_failed"
    try:
        issues = api.issues()
        if not issues and not allow_empty and (output / "index.html").exists() and previous.get("issues") != 0:
            error = "empty_response"
            raise ValueError("Unexpected empty update")
        milestones = api.request(api.root + "/milestones?state=all&per_page=100", paginate=True)
        if queue is None:
            from .queue import StateStore
            queue = StateStore(api, config["project"]["base_branch"]).read(optional=True)[0]
        error = "render_failed"
        from .queue import public_state
        data = payload(issues, config, milestones, public_state(queue))
        page, data = render(data)
        timestamp = now()
        error = "write_failed"
        # Publish content before its status pointer; readers verify matching digests.
        atomic_write(output / "data.json", json.dumps(data))
        atomic_write(output / "index.html", page)
        status = {"ok": True, "synced_at": timestamp, "digest": data["digest"],
                  "revision": data["revision"], "issues": len(data["tasks"]), "empty_confirmed": not bool(data["tasks"])}
        atomic_write(output / "status.json", json.dumps(status))
        print(f"Synced {status['issues']} issues at {timestamp}")
        return status
    except Exception:
        previous.update(ok=False, attempted_at=now(), error=error)
        atomic_write(output / "status.json", json.dumps(previous))
        raise RuntimeError(f"Sync failed ({error}); previous board preserved") from None
