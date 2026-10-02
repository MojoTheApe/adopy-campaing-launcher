"""«Publish all»: поднять черновиками всё собранное, кроме сломанного.

Кнопка одна, а работ за ней столько, сколько аккаунтов. С acl-1210 они
уходят на сервер разом: у каждого аккаунта свой профиль Multilogin, а
очередь на один и тот же профиль держит сервер. До 16.09 страница ждала
конца каждой работы перед следующей — и была вторым замком.

Кампания с ошибкой не уходит, но и не задерживает соседнюю: незаполненная
карточка — не повод не поднять ту, что рядом и собрана. Единственная
ошибка, которая не держит, — пустая ссылка: её добавляют и после сборки.

Самое опасное здесь не видно на экране. Во-первых, план с сервера **не
стоит в одном ряду** с карточками: сервер группирует кампании по имени,
две одноимённые карточки становятся одной строкой плана, и отбор по
номеру при первом же совпадении пропустил бы не ту кампанию — сломанная
ушла бы в публикацию, собранная осталась бы дома. Во-вторых, поля
называются по номеру (`camp_0_asset`), и сервер читает их подряд от нуля:
выбросив кампанию из середины, номера надо сдвинуть, иначе он прочитает
меньше, чем отправлено.

Обе вещи проверяются исполнением кода страницы.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _block() -> str:
    src = TPL.read_text(encoding="utf-8")
    start = src.index("window.CB_LINK_NOT_BLOCKING =")
    return src[start: src.index("// ── Диагностика превью", start)]


def test_the_page_code_behaves():
    """Отбор кампаний и пересборка номеров — на живом коде шаблона."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "builder_publish_all_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


def test_it_never_clicks_meta_publish():
    """Черновик и только черновик. RPA заполняет кампанию и оставляет её в
    Ads Manager; кнопку Publish в самом фейсбуке не нажимает никто."""
    block = _block()
    assert "fd.set('hand_off', '1')" in block, "потерян режим «оставить черновиком»"
    assert "fd.set('activate', '0')" in block
    assert "fd.set('dry_run', '0')" in block


def test_accounts_are_sent_at_once_and_each_is_watched():
    """Очередь — на сервере, по профилю. Страница шлёт всех и следит за каждым."""
    block = _block()
    assert "cbPollJob(" in block, "за работой перестали следить"
    assert "function _runPublishQueue" in block
    run = block[block.index("function cbNewRun"):]
    # ACL-1355: следующий аккаунт проверяется сразу после отправки этого.
    assert re.search(r"submit\(e, payload\.body, skipped\);\s*pump\(\);", run), (
        "следующий аккаунт снова ждёт конца предыдущего")
    assert "One account at a time" not in block, "подтверждение обещает очередь, которой больше нет"


def test_the_owner_is_asked_once_and_told_what_will_happen():
    """Кнопка запускает работу на живых аккаунтах — молча такое не начинают."""
    block = _block()
    ask = block[block.index("window.cbPublishAll"):]
    ask = ask[: ask.index("function _runPublishQueue")]
    assert "_safeConfirm(" in ask
    assert "Draft" in ask and "never clicks Publish" in ask, "в вопросе не сказано, что это черновик"
    assert "Do not lock the screen" in ask, (
        "пропало предупреждение о заблокированном экране — на нём загрузка креативов встаёт"
    )


def test_an_unchecked_campaign_is_not_published():
    """Имени карточки нет в плане — значит, её никто не проверял. Молча
    поднять такой черновик хуже, чем не поднять."""
    block = _block()
    body = block[block.index("function cbSkipByName"):]
    body = body[: body.index("\n}") + 2]
    assert "unchecked" in body and "skip[i] = true" in body
    assert re.search(r"if \(!\(name in verdict\)\)", body), (
        "непроверенная карточка перестала отсеиваться"
    )
