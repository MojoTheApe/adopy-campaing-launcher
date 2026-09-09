"""Граница дешборда и RPA: один замок на всё, что открывает браузер.

Зачем эти проверки существуют. Прогон кампании и чтение спенда с дешборда
работают с одним живым профилем Multilogin. Если дешборд откроет браузер, пока
идёт сборка кампании, сорвётся сборка — а выглядеть это будет как «RPA опять
сыпется». Защита здесь одна: общий `browser_operation`, и держится она на том,
что **все** пути в браузер через него проходят.

Такую защиту легко потерять молча. Достаточно завести свой `RLock` в новом
модуле, забыть декоратор на новой функции или заменить общий замок на
локальный — код продолжит работать, тесты продолжат проходить, и разойдётся
всё только на живом профиле, у байера, в середине прогона.

Поэтому проверки ниже смотрят на поведение — берут замок и наблюдают, кто
встал в очередь, — а не на наличие строки в исходнике.
"""
from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa.operations import browser_operation, serialized_browser  # noqa: E402


# ─────────────── сам замок ───────────────


def test_the_lock_is_reentrant():
    """Публикатор входит через очередь задач, а внутри — через декоратор.

    Обычный `Lock` дал бы здесь тупик: поток заблокировал бы сам себя, прогон
    завис бы навсегда, и на экране это выглядело бы как «зависло на публикации»
    без единой ошибки.
    """
    with browser_operation:
        # неблокирующий захват намеренно: с обычным `Lock` вложенный `with`
        # повис бы, и сторож остановил бы CI вместо того, чтобы сообщить о
        # поломке. Проверка обязана падать, а не висеть.
        got = browser_operation.acquire(blocking=False)
        if got:
            browser_operation.release()
        assert got, (
            "замок перестал быть реентрантным: публикатор входит через очередь "
            "задач, а внутри через декоратор — здесь он заблокирует сам себя, и "
            "прогон зависнет без единой ошибки"
        )


def _available_to_another_thread():
    result = []
    def probe():
        got = browser_operation.acquire(blocking=False)
        result.append(got)
        if got:
            browser_operation.release()
    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    return result[0]


def test_the_decorator_holds_the_lock_while_the_work_runs():
    """Не «взял и отпустил», а держит на всё время работы."""
    seen = {}

    @serialized_browser
    def work():
        # замок наш: другой поток сюда не войдёт
        seen["locked"] = not _available_to_another_thread()
        return "done"

    assert work() == "done"
    assert seen["locked"] is True


def test_the_lock_is_released_after_a_failure():
    """Упавшая операция не должна запирать браузер до перезапуска сервера.

    Иначе одна неудачная попытка чтения останавливает все прогоны, и причина
    выглядит как «RPA больше не запускается».
    """

    @serialized_browser
    def boom():
        raise RuntimeError("что-то пошло не так")

    with pytest.raises(RuntimeError):
        boom()

    assert _available_to_another_thread(), "замок остался захваченным"


def test_a_second_thread_waits_for_the_first():
    """Главное свойство: две операции не идут одновременно."""
    inside = threading.Event()
    may_finish = threading.Event()
    entered_second = threading.Event()

    @serialized_browser
    def first():
        inside.set()
        may_finish.wait(timeout=5)

    @serialized_browser
    def second():
        entered_second.set()

    t1 = threading.Thread(target=first)
    t1.start()
    assert inside.wait(timeout=5), "первая операция не стартовала"

    t2 = threading.Thread(target=second)
    t2.start()
    assert not entered_second.wait(timeout=0.5), (
        "вторая операция вошла, пока первая не закончила — браузер не защищён"
    )

    may_finish.set()
    t1.join(timeout=5)
    assert entered_second.wait(timeout=5), "вторая операция так и не дождалась"
    t2.join(timeout=5)


# ─────────────── все ли им пользуются ───────────────


#: Пути в браузер, которые обязаны стоять в общей очереди. Модуль → функции.
#: Список ведётся руками намеренно: новая функция, открывающая браузер, должна
#: попасть сюда осознанно, а не «как-нибудь потом».
MUST_SERIALIZE = {
    "src/analysis_agent/account_spend.py": (
        "read_ad_account",      # спрашивает у кабинета его номер
        "open_for_automation",  # поднимает профиль
        "read_via_rpa",         # читает спенд и состояние
    ),
    "src/fb_ads_rpa/rpa_publisher.py": (
        "publish_draft",
        "publish_slots",
    ),
}


def _decorated(path: str) -> dict[str, list[str]]:
    """Какие декораторы стоят на функциях модуля — по разбору, а не по строке.

    Чтение исходника здесь оправдано: проверяется не поведение одной функции, а
    то, что никто не завёл новый путь в браузер мимо очереди. Выполнить это
    нельзя, не открыв настоящий браузер.
    """
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = []
            for d in node.decorator_list:
                names.append(d.id if isinstance(d, ast.Name)
                             else getattr(d, "attr", getattr(getattr(d, "func", None), "id", "")))
            out[node.name] = names
    return out


@pytest.mark.parametrize("path,funcs", list(MUST_SERIALIZE.items()))
def test_every_browser_path_goes_through_the_queue(path, funcs):
    found = _decorated(path)
    for name in funcs:
        assert name in found, f"{path}: функция {name} исчезла — список пора обновить"
        assert "serialized_browser" in found[name], (
            f"{path}: {name} открывает браузер мимо общей очереди. "
            "Она может пойти одновременно с прогоном кампании на том же профиле."
        )


def test_everyone_shares_one_and_the_same_lock():
    """Свой `RLock` в новом модуле выключил бы защиту беззвучно.

    Код продолжил бы работать, тесты — проходить, а разошлось бы только на
    живом профиле, в середине прогона.
    """
    from src.analysis_agent import account_spend
    from src.web import publish_jobs

    assert account_spend._LOCK is browser_operation, (
        "чтение спенда встало в свою очередь, а не в общую"
    )
    assert publish_jobs._publish_serial is browser_operation, (
        "очередь публикации перестала делить замок с чтением"
    )
