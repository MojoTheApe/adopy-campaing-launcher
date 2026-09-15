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

    публикация   исключительна — ни с чем не соседствует, одна за раз
                 (решение владельца 31.07: несколько аккаунтов обслуживаются
                 очередью, «parallel profiles are a later, deliberate step»)
    чтения       идут параллельно друг другу, но по одному на профиль
    чтение при   ждёт, пока публикация закончится
    публикации

**Чего этот порядок не обещает.** Публикация может ждать дольше, чем хотелось
бы: новых читателей она не отсекает, а дожидается, пока кончатся нынешние.
Сделано намеренно — иначе вложенное чтение внутри уже идущего чтения встало бы
насмерть, а такие вложения у нас есть (дерево читается тем же заходом, что и
спенд). И то, и другое запускает человек руками, так что очередь конечна.

Живёт всё это в одном процессе. Два **процесса** ACL на одной машине по-
прежнему могут взяться за один профиль — от этого защищает не замок, а то,
оператор не запускает два процесса ACL на одном профиле; Multilogin сам
по себе не гарантирует эксклюзивное управление через CDP.
"""
import threading
import inspect
from functools import wraps


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

_profile_locks: dict[str, threading.RLock] = {}
_profile_guard = threading.Lock()


def profile_lock(key: str) -> threading.RLock:
    """Очередь на одну цель. Одинаковый ключ — одна очередь, разные — не мешают.

    Реентерантный: внутри чтения вызывается другое чтение того же профиля —
    дерево читается тем же заходом, что и спенд, — и обычный замок здесь встал
    бы намертво.
    """
    name = str(key or "")
    with _profile_guard:
        lock = _profile_locks.get(name)
        if lock is None:
            lock = _profile_locks[name] = threading.RLock()
        return lock


def serialized_browser(fn=None, *, key=None, reading=False):
    """Serialize mutations globally; opt reads into a shared, per-profile lease.

    Publication remains exclusive even when called directly, outside the job
    runner. Read keys are resolved from the function signature so positional
    and keyword calls occupy the same profile. A failing key resolver refuses
    the call instead of silently acquiring a different profile's lock.
    """
    def decorate(func):
        signature = inspect.signature(func)
        first_parameter = next(iter(signature.parameters), None)

        @wraps(func)
        def wrapped(*args, **kwargs):
            if not reading:
                with browser_operation:
                    return func(*args, **kwargs)
            if key is not None:
                name = key(*args, **kwargs)
            else:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                name = bound.arguments.get(first_parameter, "")
            name = str(name or "").strip()
            with browser_operation.reading(), profile_lock(name):
                return func(*args, **kwargs)
        return wrapped

    return decorate(fn) if fn is not None else decorate
