"""Граница дешборда и RPA: кто и когда открывает браузер.

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


def test_everyone_goes_through_the_same_gate():
    """Свой замок в новом модуле выключил бы защиту беззвучно.

    Код продолжил бы работать, тесты — проходить, а разошлось бы только на
    живом профиле, в середине прогона.
    """
    from src.analysis_agent import account_spend
    from src.web import publish_jobs
    from src.fb_ads_rpa.operations import profile_lock, publish_lease

    assert account_spend.browser_operation is browser_operation, (
        "чтение спенда встало в свою очередь, а не в общую"
    )
    assert account_spend.profile_lock is profile_lock, (
        "чтение спенда завело свой замок по профилю"
    )
    # С acl-1210 публикация занимает профиль, а не процесс — тем же замком,
    # что и чтения этого профиля. Своя очередь у публикации выключила бы
    # «чтение ждёт публикацию» так же беззвучно, как и прежде.
    assert publish_jobs._publish_serial is publish_lease, (
        "очередь публикации перестала делить замок профиля с чтением"
    )
    assert publish_lease("550e8400-e29b-41d4-a716-446655440000") is profile_lock("550e8400-e29b-41d4-a716-446655440000"), (
        "публикация и чтение одного профиля стоят в разных очередях"
    )


# ─────────────── роли: публикация против чтений (acl-113e) ───────────────
#
# Прежде замок был один на всё, и «чтение не лезет в браузер во время
# публикации» держалось само собой. Теперь роли разные, и каждую надо
# проверять отдельно — потерять любую из них молча очень легко.


def test_two_profiles_are_read_at_the_same_time():
    """Второй аккаунт больше не ждёт первого: это два разных профиля.

    Ради этого 113e и заводился. Общего ресурса у них нет — у каждого свой
    профиль Multilogin и свой прокси.
    """
    inside_a = threading.Event()
    may_finish = threading.Event()
    inside_b = threading.Event()

    @serialized_browser(reading=True)
    def read(profile):
        (inside_a if profile == "550e8400-e29b-41d4-a716-446655440000" else inside_b).set()
        if profile == "550e8400-e29b-41d4-a716-446655440000":
            may_finish.wait(timeout=5)

    a = threading.Thread(target=read, args=("550e8400-e29b-41d4-a716-446655440000",))
    a.start()
    assert inside_a.wait(timeout=5)

    b = threading.Thread(target=read, args=("6ba7b810-9dad-11d1-80b4-00c04fd430c8",))
    b.start()
    assert inside_b.wait(timeout=2), "второй профиль ждёт первого без причины"

    may_finish.set()
    a.join(timeout=5)
    b.join(timeout=5)


def test_the_same_profile_is_still_read_one_at_a_time():
    """Один профиль — одна очередь: два захода в один браузер его и ломают."""
    inside = threading.Event()
    may_finish = threading.Event()
    second = threading.Event()

    @serialized_browser(reading=True)
    def read(profile):
        if not inside.is_set():
            inside.set()
            may_finish.wait(timeout=5)
        else:
            second.set()

    first = threading.Thread(target=read, args=("550e8400-e29b-41d4-a716-446655440000",))
    first.start()
    assert inside.wait(timeout=5)

    other = threading.Thread(target=read, args=("550e8400-e29b-41d4-a716-446655440000",))
    other.start()
    assert not second.wait(timeout=0.5), "два чтения вошли в один профиль разом"

    may_finish.set()
    first.join(timeout=5)
    assert second.wait(timeout=5)
    other.join(timeout=5)


def test_a_read_waits_while_a_publish_holds_the_browser():
    """Свойство, которое держалось само собой, пока замок был один.

    Публикация ведёт живой профиль; чтение, влезшее посреди неё, срывает
    сборку, а выглядит это как «RPA сыпется».
    """
    from src.fb_ads_rpa.operations import publish_lease

    entered = threading.Event()

    @serialized_browser(reading=True)
    def read(profile):
        entered.set()

    with publish_lease("550e8400-e29b-41d4-a716-446655440000"):
        thread = threading.Thread(target=read, args=("550e8400-e29b-41d4-a716-446655440000",))
        thread.start()
        assert not entered.wait(timeout=0.5), "чтение полезло в браузер во время публикации"

    assert entered.wait(timeout=5), "чтение так и не дождалось конца публикации"
    thread.join(timeout=5)


def test_a_read_of_another_profile_does_not_wait_for_a_publish():
    """Свойство acl-1210: публикация занимает свой профиль, а не всех."""
    from src.fb_ads_rpa.operations import publish_lease

    entered = threading.Event()

    @serialized_browser(reading=True)
    def read(profile):
        entered.set()

    with publish_lease("550e8400-e29b-41d4-a716-446655440000"):
        thread = threading.Thread(target=read, args=("6ba7b810-9dad-11d1-80b4-00c04fd430c8",))
        thread.start()
        assert entered.wait(timeout=2), "чтение другого профиля ждёт чужую публикацию"
    thread.join(timeout=5)


def test_two_profiles_publish_at_the_same_time():
    """Ради этого acl-1210 и заводился: три аккаунта — три браузера разом."""
    from src.fb_ads_rpa.operations import publish_lease

    inside_a = threading.Event()
    may_finish = threading.Event()
    inside_b = threading.Event()

    def publish(profile):
        with publish_lease(profile):
            (inside_a if profile == "550e8400-e29b-41d4-a716-446655440000" else inside_b).set()
            if profile == "550e8400-e29b-41d4-a716-446655440000":
                may_finish.wait(timeout=5)

    a = threading.Thread(target=publish, args=("550e8400-e29b-41d4-a716-446655440000",))
    a.start()
    assert inside_a.wait(timeout=5)
    b = threading.Thread(target=publish, args=("6ba7b810-9dad-11d1-80b4-00c04fd430c8",))
    b.start()
    assert inside_b.wait(timeout=2), "вторая публикация ждёт первую без причины"
    may_finish.set()
    a.join(timeout=5)
    b.join(timeout=5)


def test_the_same_profile_still_publishes_one_at_a_time():
    """Два задания на один профиль — по-прежнему очередь: два водителя на один браузер его и ломают."""
    from src.fb_ads_rpa.operations import publish_lease

    inside = threading.Event()
    may_finish = threading.Event()
    second = threading.Event()

    def publish():
        with publish_lease("550e8400-e29b-41d4-a716-446655440000"):
            if not inside.is_set():
                inside.set()
                may_finish.wait(timeout=5)
            else:
                second.set()

    first = threading.Thread(target=publish)
    first.start()
    assert inside.wait(timeout=5)
    other = threading.Thread(target=publish)
    other.start()
    assert not second.wait(timeout=0.5), "две публикации вошли в один профиль разом"
    may_finish.set()
    first.join(timeout=5)
    assert second.wait(timeout=5)
    other.join(timeout=5)


def test_a_publish_waits_for_a_read_to_finish():
    """И наоборот: публикация не начинается поверх идущего чтения."""
    from src.fb_ads_rpa.operations import publish_lease

    inside = threading.Event()
    may_finish = threading.Event()
    published = threading.Event()

    @serialized_browser(reading=True)
    def read(profile):
        inside.set()
        may_finish.wait(timeout=5)

    def publish():
        with publish_lease("550e8400-e29b-41d4-a716-446655440000"):
            published.set()

    reader = threading.Thread(target=read, args=("550e8400-e29b-41d4-a716-446655440000",))
    reader.start()
    assert inside.wait(timeout=5)

    writer = threading.Thread(target=publish)
    writer.start()
    assert not published.wait(timeout=0.5), "публикация началась поверх чтения"

    may_finish.set()
    reader.join(timeout=5)
    assert published.wait(timeout=5)
    writer.join(timeout=5)
