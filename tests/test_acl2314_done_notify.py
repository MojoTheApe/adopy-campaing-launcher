"""ACL-2314: уведомление о конце Check plan / Publish, пока вкладка спрятана.

Логика (разрешение только из кнопки, уведомление только при спрятанной вкладке, значок
в заголовке и favicon, звук выключен по умолчанию, текст без id) проверяется в node на
настоящем блоке шаблона. Проводку к концу проверки и сборки — по тексту шаблона.
Браузерных тестов здесь нет намеренно.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def test_done_notifier_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run([node, str(ROOT / "tests" / "acl2314_done_notify_harness.cjs"), str(TPL)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr + r.stdout


def test_hooks_are_wired_and_nothing_asks_on_load():
    html = TPL.read_text(encoding="utf-8")
    assert "aclDoneNotify.finished('check'" in html
    assert "aclDoneNotify.finished('publish'" in html
    # requestPermission встречается только в обработчике кнопки (внутри enable).
    assert len(re.findall(r"requestPermission\(", html)) == 1
    block = html[html.index("/* ACL-2314 begin */"):html.index("/* ACL-2314 end */")]
    enable = block[block.index("function enable()"):block.index("function setSound")]
    assert "requestPermission(" in enable
