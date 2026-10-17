"""Кто сейчас занимает браузер: публикация — целиком, чтение — свой профиль.

Прежде здесь был **один** замок на все операции, включая чтение. Его
обоснование говорило о другом: «Separate ACL processes must not operate the
same live Multilogin profile» — то есть про один и тот же профиль. Два разных
аккаунта — два разных профиля, у каждого свой прокси, и общего ресурса между
ними нет. Второй аккаунт ждал первого без надобности (`acl-113e`).

Физическая причина, по которой два браузера нельзя было держать разом, тоже
исчезла: фоновое окно Chrome душилось, шаги шли в 10-50 раз медленнее — это
починено через CDP и измерено 30.07; неактивное окно работает почти вровень с
активным (rAF 43,6 против эталонных 59,9).

Но заменить один замок на «замок по профилю» нельзя: так теряется свойство,
которое держалось само собой, пока замок был общим, —

    пока идёт публикация, чтение в браузер не лезет.

Публикация меняет чужой кабинет и тратит деньги; чтение — нет. Поэтому роли
разные:

    публикация   исключительна **на своём профиле** — одна за раз на профиль,
                 разные профили публикуются разом (решение владельца
                 16.09, acl-1210; до этого — одна за раз на весь процесс,
                 решение 31.07: «parallel profiles are a later, deliberate
                 step». Этот шаг и сделан.)
    чтения       идут параллельно друг другу, но по одному на профиль
    чтение при   ждёт, пока публикация **этого** профиля закончится;
    публикации   чтение другого профиля не ждёт ничего

Держится это на одном замке на профиль: `profile_lock(key)`, реентерантном.
Публикация берёт его на весь прогон (`publish_lease`), чтение — на время
чтения, и очередь у обоих одна — потому чтение и не влезает в публикуемый
профиль. Общий воротник `browser_operation` остался для писателей без ключа
(их не осталось в проде, но контракт для чужого кода сохранён) и для
неблокирующих проверок границы.

**Чего этот порядок не обещает.** Публикация может ждать дольше, чем хотелось
бы: новых читателей она не отсекает, а дожидается, пока кончатся нынешние.
Сделано намеренно — иначе вложенное чтение внутри уже идущего чтения встало бы
насмерть, а такие вложения у нас есть (дерево читается тем же заходом, что и
спенд). И то, и другое запускает человек руками, так что очередь конечна.

Замок профиля общий для процессов ACM одного пользователя ОС. Его ключ —
физический UUID Multilogin (или отдельный ключ Dolphin), независимо от
конфига и порта. Граница действует на весь охваченный вызов; unmerged warm/API
сессии должны отдельно удерживать её на весь срок использования браузера.
"""
import threading
import inspect
from functools import wraps
from src.fb_ads_rpa import profile_lease as _pl
from src.fb_ads_rpa.profile_lease import profile_lock, check_nested_profile, ProfileLeaseError


class _BrowserGate:
    """Публикация против чтений: много читателей — один писатель.

    В стандартной библиотеке такого замка нет, поэтому он здесь свой и
    намеренно маленький. Оба захвата реентерантны в пределах потока: публикация
    внутри себя читает, и запрет на это остановил бы её саму.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0

    # ── писатель: `with browser_operation:` в публикации ──────────────────
    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False

    def acquire(self, blocking: bool = True) -> bool:
        """Захват писателя. Имя и вид — как у прежнего `RLock`.

        Внешняя форма оставлена прежней намеренно: `publish_jobs` берёт этот
        объект через `with`, а проверки границы дешборда и RPA пробуют
        захватить его неблокирующе из соседнего потока. Менять их ради
        переименования значило бы переписывать сторожа под подопечного.
        """
        me = threading.get_ident()
        with self._cv:
            if self._writer == me:
                self._writer_depth += 1
                return True
            # Свои же чтения этого потока не мешают — иначе публикация,
            # успевшая что-то прочитать, дождалась бы сама себя.
            while self._writer is not None or (set(self._readers) - {me}):
                if not blocking:
                    return False
                self._cv.wait()
            self._writer, self._writer_depth = me, 1
            return True

    def release(self) -> None:
        with self._cv:
            if self._writer != threading.get_ident():
                return
            self._writer_depth -= 1
            if self._writer_depth <= 0:
                self._writer, self._writer_depth = None, 0
                self._cv.notify_all()

    # ── читатель ──────────────────────────────────────────────────────────
    def acquire_read(self) -> None:
        me = threading.get_ident()
        with self._cv:
            while self._writer is not None and self._writer != me:
                self._cv.wait()
            self._readers[me] = self._readers.get(me, 0) + 1

    def release_read(self) -> None:
        me = threading.get_ident()
        with self._cv:
            left = self._readers.get(me, 0) - 1
            if left > 0:
                self._readers[me] = left
            else:
                self._readers.pop(me, None)
                self._cv.notify_all()

    def reading(self):
        """Контекст читателя — для тех, кто держит браузер без декоратора."""
        gate = self

        class _Hold:
            def __enter__(self_inner):
                gate.acquire_read()
                return gate

            def __exit__(self_inner, *_exc):
                gate.release_read()
                return False

        return _Hold()


#: Единственный воротник на процесс. Имя прежнее: `publish_jobs` берёт его
#: через `with`, и для публикации ничего не изменилось.
browser_operation = _BrowserGate()

def publish_lease(key: str, *, cancelled=None):
    """Занять профиль на всю публикацию.

    Тот же замок, что берут чтения этого профиля, — поэтому чтение,
    пришедшее посреди публикации, ждёт её конца, а чтение другого профиля
    не ждёт ничего. Реентерантен: `_run_job` берёт его на прогон, а
    `publish_draft` внутри — ещё раз, тем же потоком.
    """
    lease = profile_lock(key)
    return lease if cancelled is None else lease.holding(cancelled=cancelled, timeout=_pl.PUBLISH_WAIT_SECONDS)


#: Сколько чтение ждёт профиль, занятый другой операцией этого или другого процесса.
READ_WAIT_SECONDS = 600.0


def serialized_browser(fn=None, *, key=None, reading=False, on_lease_error=None):
    """Serialize work on one profile; reads share, writes are exclusive.

    A write with a `key` (or a resolvable first parameter) takes that
    profile's lock and nothing else, so two profiles publish at once. A write
    with no key at all keeps the old process-wide gate — the safe default
    for code that has not said which browser it drives. Read keys are
    resolved from the function signature so positional and keyword calls
    occupy the same profile. A failing key resolver refuses the call instead
    of silently acquiring a different profile's lock.
    `on_lease_error` preserves an entry point's existing result-shaped refusal;
    the browser operation is never invoked when resolution/acquisition fails.
    """
    def decorate(func):
        signature = inspect.signature(func)
        first_parameter = next(iter(signature.parameters), None)

        def resolve(args, kwargs) -> str:
            if key is not None:
                name = key(*args, **kwargs)
            else:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                name = bound.arguments.get(first_parameter, "")
            resolved = str(name or "").strip()
            check_nested_profile(resolved)
            return resolved

        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                if not reading:
                    if key is None:
                        with browser_operation:
                            return func(*args, **kwargs)
                    with publish_lease(resolve(args, kwargs)):
                        return func(*args, **kwargs)
                # Чтение (Check plan, Страницы, спенд) ждёт, пока другая операция на этом
                # профиле закончится, и подхватывает открытый профиль. Отказ «busy»
                # через 120 с (ACL-1853) превращал обычную очередь в ошибку плана.
                with browser_operation.reading(),                         profile_lock(resolve(args, kwargs)).holding(timeout=READ_WAIT_SECONDS):
                    return func(*args, **kwargs)
            except ProfileLeaseError as exc:
                if on_lease_error is None:
                    raise
                return on_lease_error(exc, *args, **kwargs)
        return wrapped

    return decorate(fn) if fn is not None else decorate
