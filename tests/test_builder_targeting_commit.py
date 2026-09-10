"""Набранный руками таргетинг доводится до выбора — или о нём говорят.

Скрытое поле с интересами заполнялось ТОЛЬКО кликом мышью по строке списка.
Напечатал код, ушёл с поля — имя кампании собиралось из напечатанного текста и
выглядело правильным, а интересы оставались пустыми.

10 сентября так ушли в черновик обе кампании: в имени `Trading` и `FX`, в
телеметрии RPA `targeting` записан пропуском со словами «в черновике нет
интересов». На экране всё выглядело собранным.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def test_the_page_code_behaves(tmp_path):
    """Поведение проверяется исполнением кода страницы, а не чтением файла."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "builder_targeting_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


# ── и публикация не молчит ────────────────────────────────────────────────

def test_publish_warns_about_targeting_that_never_resolved():
    """Кампания без детального таргетинга не должна уходить незаметно."""
    tpl = TPL.read_text(encoding="utf-8")
    block = tpl[tpl.index("window.cbPublish = function"):]
    block = block[: block.index("function _doCbPublish")]
    assert "lostTargeting" in block, "публикация снова молчит о непроставленном таргетинге"
    assert "cbTargetingCommit(input)" in block, (
        "перед публикацией напечатанное не доводится до выбора"
    )


def test_the_warning_names_what_was_typed():
    """«Таргетинг не выбран» без слова — это загадка, а не сообщение."""
    tpl = TPL.read_text(encoding="utf-8")
    block = tpl[tpl.index("var lostTargeting = []"):]
    block = block[: block.index("_safeConfirm(")]
    assert "names" in block and "aclEscHtml" in block, "набранное не показано или не экранировано"


def test_the_commit_runs_on_leaving_the_field():
    """Иначе правка живёт только в публикации, а имя собирается раньше."""
    tpl = TPL.read_text(encoding="utf-8")
    block = tpl[tpl.index("// Close targeting dropdown on blur"):]
    block = block[: block.index("window.cbTypeChange")]
    assert "cbTargetingCommit(input)" in block
