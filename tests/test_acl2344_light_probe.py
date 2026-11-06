"""ACL-2344 P1/P2(A'): synthetic same-origin document + dummy hand-over; no network, no Meta."""
import glob
import os
import socket
import subprocess
import tempfile
import time
import urllib.request

import pytest

from src.fb_api import light_probe
from src.fb_ads_rpa.tab_watchdog import RawCdp


def _chromium():
    hits = glob.glob(os.path.expanduser(r"~\AppData\Local\ms-playwright\chromium-*\chrome-win*\chrome.exe"))
    hits += glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"))
    return sorted(hits)[-1] if hits else None


@pytest.fixture()
def browser():
    exe = _chromium()
    if not exe:
        pytest.skip("no local Chromium")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={tempfile.mkdtemp()}",
         "--proxy-server=http://127.0.0.1:9", "about:blank"],   # dead proxy: nothing can leave the machine
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                urllib.request.urlopen(url + "/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        yield url
    finally:
        proc.kill()


def test_rawcdp_nested_call_in_event_handler_keeps_reply_id():
    sent = []

    class Sock:
        pass

    cdp = RawCdp.__new__(RawCdp)
    cdp._id = 0
    replies = [b'{"method":"Fetch.requestPaused","params":{}}', b'{"id":1,"result":{"ok":1}}']
    cdp._send_frame = lambda op, payload, timeout: sent.append(payload)
    cdp._recv_message = lambda deadline: replies.pop(0)
    nested = []

    def on_event(msg):
        cdp._id += 1          # what a nested call() does
        nested.append(msg["method"])

    assert cdp.call("Page.navigate", {}, on_event=on_event) == {"ok": 1}
    assert nested == ["Fetch.requestPaused"]


def test_light_document_and_dummy_handover(browser):
    result = light_probe.run_p1(browser)
    assert result["work_doc"] == {"ok": True, "reason": "", "fulfilled": 1, "blocked": 0}
    assert result["capture_doc"]["ok"] is True
    h = result["handover"]
    assert h["handover_ok"] and h["acked"] and h["same_world_survives"] and h["main_world_clean"]
    assert "DUMMY" not in repr(result)
