"""У папки профиля два написания, и Multilogin пользуется вторым.

9 сентября чтение спенда `KLD_BFL_X84` отказывало словами «профиль открыт
вручную в MLX Desktop» **сразу после того, как дешборд сам этот профиль и
открыл** кнопкой «open profile». Отказ выглядел осмысленно и звучал знакомо —
поэтому и не вызывал подозрений.

На машине всё было в порядке: у процесса Mimic стоял `--remote-debugging-port`,
порт слушался, CDP отвечал. Не сходилось единственное — путь. Multilogin
запускает браузер с коротким именем 8.3:

    C:\\Users\\EXAMPL~1\\mlx\\profiles\\WORKSP~1\\FOLDER~1\\PROFIL~1

а мы вычисляем длинное и сравниваем их как строки. `abspath` и `normcase`
коротких имён не разворачивают, так что два имени одной и той же папки
расходятся, проверка владения портом не находит ни одного процесса — и путь
приводит к сообщению про «вручную», которое к делу уже не относится.

Сравнение по папке при этом правильное и остаётся: без него можно прицепиться
к браузеру **другого** аккаунта, а это хуже любого непрочитанного числа.

Проверяется здесь наш порядок действий, а не Windows: подсовывается провайдер,
который узнаёт только одно написание — как настоящий Multilogin.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402

LONG = r"C:\Users\Example User\mlx\profiles\workspace-example\folder-example\profile-example"
SHORT = r"C:\Users\EXAMPL~1\mlx\profiles\WORKSP~1\FOLDER~1\PROFIL~1"


class _Entry:
    profile_id = "profile-example"
    folder_id = "folder-example"


class _Conn:
    def __init__(self):
        self.closed_keeping_window = False

    def close(self, keep_browser_open=False):
        self.closed_keeping_window = bool(keep_browser_open)


class _Provider:
    """Провайдер, который знает про папку только то написание, что дал MLX."""

    def __init__(self, knows: str):
        self.knows = knows
        self.tried: list[str] = []
        self.conn = _Conn()

    def _try_attach_existing(self, profile_id, user_data_dir, *, strict_profile=False, new_page=False):
        self.tried.append(user_data_dir)
        return self.conn if user_data_dir == self.knows else None

    def _profile_process_running(self, user_data_dir, profile_id=""):
        return True


@pytest.fixture
def wired(monkeypatch):
    """Подменяем всё, что уходит за пределы проверяемого решения.

    Подменяется **класс внутри настоящего модуля**, а не сам модуль в
    `sys.modules`. Разница не косметическая. `_attach_only` пишет
    `from src.fb_ads_rpa import browser`, а это обращение к атрибуту уже
    импортированного пакета: стоит другому тесту в прогоне импортировать
    настоящий `browser`, и подмена в `sys.modules` перестаёт что-либо значить.

    Первая версия этой проверки была написана именно так — и в полном прогоне
    молча ушла в настоящий Multilogin: подключилась по CDP к живому профилю
    рекламного аккаунта и открыла там пустую вкладку. В одиночку тест при этом
    проходил. Проверка, которая трогает живой аккаунт, недопустима независимо
    от того, что она проверяет.
    """
    def install(provider):
        monkeypatch.setattr(
            "src.fb_ads_rpa.browser.MultiloginDirectCDPProvider", lambda: provider
        )
        monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a, **k: Path(LONG))
        monkeypatch.setattr(sp, "_short_path", lambda p: SHORT if str(p) == LONG else "")
        return provider
    return install


# ── два написания одной папки ─────────────────────────────────────────────

def test_both_spellings_are_offered_long_one_first():
    """Длинное первым: на машинах, где 8.3 выключено, оно единственное."""
    import types

    monkey = types.SimpleNamespace()
    del monkey
    original = sp._short_path
    try:
        sp._short_path = lambda p: SHORT
        assert sp._dir_spellings(LONG) == [LONG, SHORT]
    finally:
        sp._short_path = original


def test_a_volume_without_short_names_gives_a_single_spelling():
    """Пустой ответ от Windows — обычное дело, а не сбой."""
    original = sp._short_path
    try:
        sp._short_path = lambda p: ""
        assert sp._dir_spellings(LONG) == [LONG]
    finally:
        sp._short_path = original


def test_the_same_spelling_is_not_offered_twice():
    """Иначе провайдер зря опрашивает все процессы во второй раз."""
    original = sp._short_path
    try:
        sp._short_path = lambda p: LONG
        assert sp._dir_spellings(LONG) == [LONG]
    finally:
        sp._short_path = original


def test_the_real_windows_call_survives_a_path_that_does_not_exist():
    """`GetShortPathNameW` на несуществующей папке отвечает нулём.

    Пустая строка здесь — верный ответ; исключение уронило бы чтение спенда.
    """
    assert sp._short_path(r"C:\нет\такой\папки\совсем") == ""


# ── чтение спенда ─────────────────────────────────────────────────────────

def test_a_profile_opened_by_automation_is_attached_even_under_its_short_name(wired):
    """Тот самый случай X84. На непочиненном коде — отказ «открыт вручную»."""
    provider = wired(_Provider(knows=SHORT))
    assert sp._attach_only(_Entry(), "KLD_BFL_X84") is provider.conn
    assert provider.tried == [LONG, SHORT], provider.tried


def test_the_long_spelling_is_still_enough_on_its_own(wired):
    """Где всё и так работало, второе написание не должно требоваться."""
    provider = wired(_Provider(knows=LONG))
    assert sp._attach_only(_Entry(), "KLD_BFL_X84") is provider.conn
    assert provider.tried == [LONG], "лишний обход процессов после успеха"


def test_a_window_without_a_debug_port_still_says_what_to_do(wired):
    """Настоящий «открыт вручную» никуда не девается — только он и остаётся."""
    provider = wired(_Provider(knows="ни то ни другое"))
    with pytest.raises(sp.ProfileNotAttachable) as exc:
        sp._attach_only(_Entry(), "KLD_BFL_X84")
    assert "вручную" in str(exc.value)
    assert provider.tried == [LONG, SHORT]


def test_nothing_is_relaunched_on_the_way(wired):
    """Единственная опасная операция — перезапуск; её здесь быть не должно.

    Перезапуск разлогинил x01 и умеет терять прокси аккаунта.
    """
    provider = wired(_Provider(knows=SHORT))
    sp._attach_only(_Entry(), "KLD_BFL_X84")
    assert not hasattr(provider, "open_called"), "провайдер поднимал профиль сам"


# ── кнопка «open profile» ─────────────────────────────────────────────────

def test_open_profile_recognises_a_window_it_already_opened(wired, monkeypatch):
    """Второе нажатие должно говорить «уже открыт», а не «открыт вручную».

    Иначе человек закрывает в MLX Desktop окно, поднятое автоматизацией, —
    ровно то, чего мы просим не делать.
    """
    provider = wired(_Provider(knows=SHORT))
    monkeypatch.setattr(sp, "_entry_for", lambda account: _Entry())
    ok, message = sp.open_for_automation("KLD_BFL_X84")
    assert ok is True
    assert "уже открыт автоматизацией" in message
    assert provider.conn.closed_keeping_window, "окно закрыли за человеком"


# ══ чтение не может поднять профиль ═══════════════════════════════════════
#
# Перезапуск профиля разлогинивает аккаунт (так вышло на x01), а ветка
# перезапуска вдобавок умеет терять прокси. Для публикации риск оправдан —
# туда ради этого и приходят. Ради одного числа за вчера — нет.
#
# До сих пор это правило держалось на честном слове: `test_nothing_is_
# relaunched_on_the_way` проверяет **один** флаг у двойника, у которого
# методов запуска нет вовсе. Вызови код завтра `start_profile`, и двойник
# упадёт с AttributeError — это заметят, но как поломку теста. А позови он
# метод, которого двойник случайно не знает, проверка промолчит.

ALLOWED = {"_try_attach_existing", "_profile_process_running"}


class _Watchful:
    """Провайдер, который помнит, что у него спрашивали.

    Любое обращение к неизвестному атрибуту записывается и возвращает
    заглушку — так вызов метода запуска не падает, а попадает в список.
    """

    def __init__(self, attaches: bool = True, running: bool = True):
        self.touched: list[str] = []
        self.conn = _Conn()
        self._attaches = attaches
        self._running = running

    def _try_attach_existing(self, profile_id, user_data_dir, *, strict_profile=False, new_page=False):
        self.touched.append("_try_attach_existing")
        assert strict_profile is True, "чтение прицепилось без strict_profile"
        return self.conn if self._attaches else None

    def _profile_process_running(self, user_data_dir, profile_id=""):
        self.touched.append("_profile_process_running")
        return self._running

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        self.touched.append(name)
        return lambda *a, **k: None


def test_reading_touches_only_the_two_methods_it_is_allowed(wired):
    """Любой новый вызов к провайдеру попадёт сюда и будет виден по имени."""
    provider = wired(_Watchful())
    sp._attach_only(_Entry(), "KLD_BFL_X84")
    unexpected = [n for n in provider.touched if n not in ALLOWED]
    assert not unexpected, f"чтение позвало у провайдера: {unexpected}"


def test_a_profile_that_will_not_attach_is_refused_not_started(wired):
    """Прицепиться не вышло, окно открыто руками — останавливаемся.

    Это и есть правило целиком: отказ вместо запуска.
    """
    provider = wired(_Watchful(attaches=False, running=True))
    with pytest.raises(sp.ProfileNotAttachable):
        sp._attach_only(_Entry(), "KLD_BFL_X84")
    unexpected = [n for n in provider.touched if n not in ALLOWED]
    assert not unexpected, f"вместо отказа чтение позвало: {unexpected}"


def test_this_check_would_catch_a_relaunch(wired):
    """Обратная проверка: сторож, который ничего не ловит, — хуже отсутствующего.

    Подсовываем провайдера, у которого попытка прицепиться сама зовёт запуск.
    """
    class _Sneaky(_Watchful):
        def _try_attach_existing(self, *a, **k):
            self.touched.append("_try_attach_existing")
            self.start_profile()          # то, чего быть не должно
            return self.conn

    provider = wired(_Sneaky())
    sp._attach_only(_Entry(), "KLD_BFL_X84")
    assert "start_profile" in provider.touched
    unexpected = [n for n in provider.touched if n not in ALLOWED]
    assert unexpected, "подложенный запуск не замечен — проверка ничего не стоит"


def test_the_reading_paths_go_through_the_one_door():
    """Каждый путь чтения зовёт `_attach_only`, а не провайдера напрямую.

    Иначе проверка выше сторожит дверь, мимо которой можно пройти.
    """
    import ast

    root = Path(sp.__file__).resolve().parent.parent.parent
    for rel in ("src/analysis_agent/account_spend.py", "src/analysis_agent/country_split.py"):
        src = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            # Две функции имеют право: `_attach_only` — сама дверь, а
            # `open_profile_for_automation` — кнопка «Open profile», то есть
            # осознанное действие человека, у которого свой отказ на профиль,
            # открытый руками. Всё остальное обязано ходить через дверь.
            if fn.name in ("_attach_only", "open_profile_for_automation"):
                continue
            called = {
                node.func.attr
                for node in ast.walk(fn)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            leaked = called & ALLOWED
            assert not leaked, f"{rel}:{fn.name} ходит к провайдеру мимо _attach_only: {leaked}"
