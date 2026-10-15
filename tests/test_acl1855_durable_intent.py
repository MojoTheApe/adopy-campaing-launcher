"""ACL-1855: команда над кампанией не пишет в Meta, пока её намерение не легло на диск.

Аудит ACL-1800 нашёл: `OperationStore._persist` глотал OSError, и `_run`
отправлял POST, хотя журнал не записался — итог «Succeeded» без истории.
Здесь сбои журнала (append, flush, fsync) внедряются на каждом шаге и
проверяется: до POST — ноль записей и видимая ошибка; после POST — сохранённое
ранее намерение и «не проверено», а не «ничего не записали»; рестарт на любой
границе восстанавливает правду с тем же op_id и ключом; без слепых повторов.

Ни Meta, ни Multilogin не трогаются: Graph — `FakeMeta` из тестов ACL-1800.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import commands as cmd  # noqa: E402
from tests.test_acl1800_campaign_commands import (  # noqa: E402
    CAMP_A, SET_BID, FakeMeta, World, t)


# ───────────────────────── внедрение сбоев ─────────────────────────

class _FaultyFile:
    """Обёртка файла журнала: падает на write / flush в назначенный момент."""

    def __init__(self, real, fail: str, partial: bool):
        self._real, self._fail, self._partial = real, fail, partial

    def write(self, data):
        if self._fail == "append":
            if self._partial:              # оборванная строка: половина байтов успела лечь
                self._real.write(data[: len(data) // 2])
                self._real.flush()
            raise OSError(28, "No space left on device")
        return self._real.write(data)

    def flush(self):
        if self._fail == "flush":
            raise OSError(5, "Input/output error")
        return self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._real.close()
        except OSError:
            pass
        return False


class Faults:
    """Счётчик сохранений журнала; сбой на выбранных номерах (с 1)."""

    def __init__(self, store: cmd.OperationStore, monkeypatch, *, at=(), kind="append",
                 from_on: int | None = None, partial: bool = False):
        self.n = 0
        self.at, self.kind, self.from_on, self.partial = set(at), kind, from_on, partial
        self.failed: list[int] = []
        real_open = store._open_append
        real_fsync = cmd.os.fsync
        faults = self

        def _open(path):
            faults.n += 1
            fh = real_open(path)
            if faults._hit():
                faults.failed.append(faults.n)
                if faults.kind == "fsync":
                    faults._fsync_fail = True
                    return fh
                return _FaultyFile(fh, faults.kind, faults.partial)
            faults._fsync_fail = False
            return fh

        def _fsync(fd):
            if getattr(faults, "_fsync_fail", False):
                faults._fsync_fail = False
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        monkeypatch.setattr(store, "_open_append", _open)
        monkeypatch.setattr(cmd.os, "fsync", _fsync)

    def _hit(self) -> bool:
        return self.n in self.at or (self.from_on is not None and self.n >= self.from_on)


def _rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


KINDS = ["append", "flush", "fsync"]
TWO = [t("campaign", CAMP_A), t("adset", SET_BID)]

# Сохранения при двух объектах и обычном ходе: 1 Running (execute), 2 sending#1,
# 3 после POST#1, 4 sending#2, 5 после POST#2, 6 Verifying, 7 итог.


# ───────────────────────── исходная ошибка ─────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_original_bug_disk_gone_means_no_write_and_no_success(world, monkeypatch, kind):
    """Журнал не пишется вообще: раньше уходил ACTIVE POST и было «Succeeded» без истории."""
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    Faults(world.store, monkeypatch, kind=kind, from_on=1)
    with pytest.raises(cmd.CommandRefused) as err:
        world.execute(pv)
    assert err.value.code == "storage" and err.value.status == 503
    assert "nothing was sent" in str(err.value).lower()
    assert world.meta.posts == []
    assert world.meta.objs[CAMP_A]["status"] == "PAUSED"
    assert world.final(pv)["state"] == cmd.FAILED


def test_original_bug_reproduction_without_test_seams(world, monkeypatch):
    """То же через один `os.fsync` — работает и на коде ACL-1800 (там уходил POST)."""
    def _broken(fd):
        raise OSError(5, "Input/output error")

    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    monkeypatch.setattr(cmd.os, "fsync", _broken)
    try:
        world.execute(pv)
    except cmd.CommandRefused:
        pass
    assert world.meta.posts == []
    assert world.final(pv)["state"] != cmd.SUCCEEDED


# ───────────────────────── сбой до POST ─────────────────────────

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("persistent", [False, True])
def test_failure_saving_the_first_sending_record_sends_nothing(world, monkeypatch, kind, persistent):
    pv = world.preview("set_status", "ACTIVE", TWO)
    Faults(world.store, monkeypatch, kind=kind, at={2}, from_on=2 if persistent else None)
    world.execute(pv)
    done = world.final(pv)
    assert world.meta.posts == []
    assert done["state"] == cmd.FAILED
    assert [a["result"] for a in done["affected"]] == ["not_sent", "not_attempted"]
    assert "history could not be saved" in done["message"].lower()
    assert "nothing was written" in done["message"].lower()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("persistent", [False, True])
def test_failure_saving_a_later_sending_record_stops_there(world, monkeypatch, kind, persistent):
    pv = world.preview("set_status", "ACTIVE", TWO)
    Faults(world.store, monkeypatch, kind=kind, at={4}, from_on=4 if persistent else None)
    world.execute(pv)
    done = world.final(pv)
    assert [p for p, _ in world.meta.posts] == [f"/{CAMP_A}"]        # второй объект не отправлен
    assert world.meta.objs[SET_BID]["status"] == "PAUSED"
    assert [a["result"] for a in done["affected"]] == ["saved", "not_sent"]   # частичный успех виден
    assert done["state"] != cmd.SUCCEEDED
    assert "history could not be saved" in done["affected"][1]["note"].lower()


def test_failure_saving_the_running_record_refuses_with_a_storage_error(world, monkeypatch):
    pv = world.preview("set_budget", "20", [t("adset", SET_BID)])
    Faults(world.store, monkeypatch, at={1})
    with pytest.raises(cmd.CommandRefused) as err:
        world.execute(pv, key="idem-key-0001")
    assert err.value.code == "storage" and world.meta.posts == []
    # повтор с тем же ключом не пишет и не притворяется успехом
    again = world.execute(pv, key="idem-key-0001")
    assert again["state"] == cmd.FAILED and world.meta.posts == []


# ───────────────────────── сбой после POST ─────────────────────────

@pytest.mark.parametrize("kind", KINDS)
def test_failure_after_the_post_keeps_the_durable_intent(world, monkeypatch, tmp_path, kind):
    """Запрос ушёл, а дальше диск отказал: в журнале остаётся «sending», итог — не проверен."""
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    Faults(world.store, monkeypatch, kind=kind, from_on=3)
    world.execute(pv)
    done = world.final(pv)
    assert len(world.meta.posts) == 1
    assert done["state"] == cmd.UNKNOWN                     # не «Succeeded» и не «ничего не записали»
    assert "nothing was written" not in done["message"].lower()
    assert "history could not be saved" in done["message"].lower()
    after = cmd.OperationStore(tmp_path / "history.jsonl").get(pv["op_id"])
    assert "nothing was written" not in after["message"].lower()
    if kind == "append":
        # ни одна строка после POST не легла: последнее надёжное — намерение
        assert _rows(tmp_path / "history.jsonl")[-2]["affected"][0]["result"] == "sending"
        assert after["state"] == cmd.UNKNOWN and after["affected"][0]["result"] == "unknown"
    else:
        # flush/fsync отказали, но байты, может быть, легли: журнал не хуже правды
        assert after["affected"][0]["result"] in ("unknown", "saved")


def test_one_lost_save_after_the_post_is_healed_by_the_next_one(world, monkeypatch, tmp_path):
    pv = world.preview("set_status", "ACTIVE", TWO)
    Faults(world.store, monkeypatch, at={3})
    world.execute(pv)
    done = world.final(pv)
    assert done["state"] == cmd.SUCCEEDED and len(world.meta.posts) == 2
    assert _rows(tmp_path / "history.jsonl")[-1]["state"] == cmd.SUCCEEDED


def test_failed_final_save_reports_unverified_not_success(world, monkeypatch, tmp_path):
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    f = Faults(world.store, monkeypatch, at=())
    world.execute(pv)
    total = f.n
    # тот же ход заново, но падает только последнее сохранение
    w2 = World(tmp_path / "b")
    pv2 = w2.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    Faults(w2.store, monkeypatch, at={total})
    w2.execute(pv2)
    done = w2.final(pv2)
    assert done["state"] == cmd.UNKNOWN and "history could not be saved" in done["message"].lower()
    after = cmd.OperationStore(w2.store.path).get(pv2["op_id"])
    assert after["state"] != cmd.SUCCEEDED


# ───────────────────────── оборванная строка ─────────────────────────

def test_a_torn_line_does_not_swallow_the_next_record(world, monkeypatch, tmp_path):
    pv = world.preview("set_status", "ACTIVE", TWO)
    Faults(world.store, monkeypatch, at={3}, partial=True)    # половина строки после POST#1
    world.execute(pv)
    raw = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert sum(1 for line in raw.splitlines() if line.strip()) > len(_rows(tmp_path / "history.jsonl"))
    got = cmd.OperationStore(tmp_path / "history.jsonl").get(pv["op_id"])
    assert got["state"] == cmd.SUCCEEDED
    assert [a["result"] for a in got["affected"]] == ["saved", "saved"]


# ───────────────────────── рестарт на каждой границе ─────────────────────────

def _record_run(tmp_path, monkeypatch, *, lost: str | None = None):
    """Прогон двух объектов; на каждое сохранение — сколько POST уже ушло к этому моменту."""
    meta = FakeMeta()
    if lost:
        meta.fail_next[SET_BID] = lost
    w = World(tmp_path, meta)
    pv = w.preview("set_status", "ACTIVE", TWO)
    posts_at_save: list[int] = []
    real = w.store._open_append

    def _open(path):
        posts_at_save.append(len(meta.posts))
        return real(path)

    monkeypatch.setattr(w.store, "_open_append", _open)
    w.execute(pv, key="idem-key-0001")
    return w, pv, posts_at_save


@pytest.mark.parametrize("lost", [None, "lost", "applied"])
def test_restart_at_every_boundary_recovers_a_truthful_state(tmp_path, monkeypatch, lost):
    w, pv, posts_at_save = _record_run(tmp_path, monkeypatch, lost=lost)
    lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) == len(posts_at_save)
    for k in range(1, len(lines) + 1):
        # процесс умер после k-й строки: до (k+1)-й могли уйти ещё POST
        sent_by_crash = posts_at_save[k] if k < len(lines) else len(w.meta.posts)
        part = tmp_path / f"crash_{k}.jsonl"
        part.write_text("".join(lines[:k]), encoding="utf-8")
        store = cmd.OperationStore(part)
        got = store.get(pv["op_id"])
        assert got is not None, k
        assert got["state"] in cmd.FINAL_STATES, k
        assert store.by_key("idem-key-0001")["op_id"] == pv["op_id"], k
        assert got["revision_id"] == pv["revision_id"], k
        results = [a["result"] for a in got["affected"]]
        for i, res in enumerate(results):
            if i < sent_by_crash:      # запись по объекту могла уйти — нельзя врать «не отправлено»
                assert res in ("unknown", "saved", "mismatch", "refused"), (k, results)
        if sent_by_crash == 0:
            assert got["state"] == cmd.FAILED and set(results) <= {"not_sent", "not_attempted"}, k
        else:
            assert "Nothing was written" not in got["message"], (k, got["message"])
        if got["state"] == cmd.SUCCEEDED:
            assert results == ["saved", "saved"], k
        # после рестарта тот же ключ ничего не пишет
        before = len(w.meta.posts)
        again = cmd.execute(store, w.open_session, revision_id=pv["revision_id"],
                            idempotency_key="idem-key-0001", spawn=lambda fn: fn(),
                            sleep=lambda s: None)
        assert again["op_id"] == pv["op_id"] and len(w.meta.posts) == before, k


def test_restart_after_readback_keeps_the_confirmed_results(tmp_path):
    """Сверка закончилась, итог записать не успели: подтверждённое не превращается в «не отправлено»."""
    path = tmp_path / "h.jsonl"
    store = cmd.OperationStore(path)
    op = {"op_id": "op_v", "state": cmd.VERIFYING, "created_at": "x", "revision_id": "rev_v",
          "idempotency_key": "idem-key-0009",
          "affected": [{"id": "1", "result": "saved"}, {"id": "2", "result": "saved"}]}
    store.add(op)
    store.save(op)
    got = cmd.OperationStore(path).get("op_v")
    assert got["state"] == cmd.SUCCEEDED
    assert [a["result"] for a in got["affected"]] == ["saved", "saved"]


def test_lost_answer_is_reconciled_by_reading_not_by_posting_again(world):
    world.meta.fail_next[CAMP_A] = "lost"
    pv = world.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    world.execute(pv, key="idem-key-0001")
    assert len(world.meta.posts) == 1 and world.final(pv)["state"] == cmd.UNKNOWN
    reborn = cmd.OperationStore(world.store.path)
    again = cmd.execute(reborn, world.open_session, revision_id=pv["revision_id"],
                        idempotency_key="idem-key-0001", spawn=lambda fn: fn(), sleep=lambda s: None)
    assert again["state"] == cmd.UNKNOWN and len(world.meta.posts) == 1
