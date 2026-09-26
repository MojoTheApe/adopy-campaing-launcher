"""Телеметрия прогонов: что сыпется чаще и почему.

Это единственное место, которое отвечает на вопрос «какой шаг подводит», —
скриншоты на него не отвечают. И у модуля есть обещание, важнее самих цифр:
**запись не должна ронять прогон**. Телеметрия — наблюдение за работой, а не
её условие; исключение отсюда убило бы сборку кампании ради строки в журнале.

Проверяется поведение на настоящих файлах во временной папке: формат здесь —
строки JSON на диске, и заглушка проверяла бы заглушку.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _no_current_run():
    """Каждый тест начинает без текущего прогона.

    Тесты робота, которые гонят `run_recorded_campaign_flow` (например,
    test_route_placeholder_is_loading, test_rpa_absent_elements_cost_no_time),
    оставляют в процессе прогон «Example», и при полном прогоне записи здесь
    получали чужую кампанию.
    """
    rt.clear_current_run()
    yield
    rt.clear_current_run()


# ─────────────── разбор имени шага ───────────────


@pytest.mark.parametrize("label,step,ok", [
    ("after_website_click", "website_click", True),
    ("success_budget", "budget", True),
    ("fail_creative_upload", "creative_upload", False),
])
def test_the_screenshot_name_already_says_what_happened(label, step, ok):
    """Записанный поток и так называет снимки `after_` и `fail_`.

    Отдельной разметки шагов не нужно — она разошлась бы с самими шагами.
    """
    assert rt.normalise_step(label) == (step, ok)


def test_an_unnamed_label_claims_neither_success_nor_failure():
    """`None` здесь — не «ничего не случилось», а «неизвестно».

    Такие записи не должны попадать в статистику надёжности: они испортили бы
    долю отказов, притворившись успехами.
    """
    assert rt.normalise_step("just_a_note") == ("just_a_note", None)


# ─────────────── запись ───────────────


def test_a_record_lands_as_one_json_line(tmp_path):
    p = tmp_path / "t.jsonl"
    rt.record("website_click", True, run_id="abc", campaign="KW_1", duration_s=1.234, path=p)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["step"] == "website_click"
    assert rows[0]["ok"] is True
    assert rows[0]["run_id"] == "abc"
    assert rows[0]["campaign"] == "KW_1"
    assert rows[0]["duration_s"] == 1.2, "длительность округляется до одного знака"
    assert rows[0]["ts"], "без времени запись бесполезна"


def test_empty_extras_do_not_clutter_the_record(tmp_path):
    p = tmp_path / "t.jsonl"
    rt.record("step", False, path=p)
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    for absent in ("campaign", "duration_s", "diag", "note"):
        assert absent not in rec


def test_records_append_rather_than_replace(tmp_path):
    p = tmp_path / "t.jsonl"
    rt.record("one", True, path=p)
    rt.record("two", False, path=p)
    assert len(p.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_writing_telemetry_never_breaks_the_run(tmp_path):
    """Обещание модуля: `record` не бросает исключений.

    Иначе строка журнала стоила бы сборки кампании. Здесь путь заведомо
    негодный — вместо файла подсунут каталог.
    """
    bad = tmp_path / "это-каталог"
    bad.mkdir()
    rt.record("step", True, path=bad)  # не должно бросить


# ─────────────── чтение ───────────────


def test_a_broken_line_does_not_lose_the_rest(tmp_path):
    """Одна испорченная строка не должна обнулять всю историю прогонов."""
    p = tmp_path / "t.jsonl"
    p.write_text('{"step":"a","ok":true}\n не json \n\n{"step":"b","ok":false}\n',
                 encoding="utf-8")
    steps = [r["step"] for r in rt.load(p)]
    assert steps == ["a", "b"]


def test_no_file_means_no_rows(tmp_path):
    assert rt.load(tmp_path / "нет.jsonl") == []


# ─────────────── сводка ───────────────


def _write(p: Path, rows: list[dict]) -> None:
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_the_worst_step_comes_first(tmp_path):
    """Сводка отвечает на «за что браться», значит порядок — часть ответа."""
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"run_id": "r1", "step": "надёжный", "ok": True},
        {"run_id": "r1", "step": "надёжный", "ok": True},
        {"run_id": "r1", "step": "хлипкий", "ok": False},
        {"run_id": "r1", "step": "хлипкий", "ok": True},
        {"run_id": "r1", "step": "мёртвый", "ok": False},
    ])
    out = rt.summarize(p)
    assert [s["step"] for s in out] == ["мёртвый", "хлипкий", "надёжный"]
    assert out[0]["fail_rate"] == 1.0
    assert out[1]["fail_rate"] == 0.5
    assert out[2]["runs"] == 2


def test_records_without_a_verdict_stay_out_of_the_statistics(tmp_path):
    """`ok: null` — «неизвестно», и в долю отказов оно попасть не должно."""
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"run_id": "r1", "step": "шаг", "ok": None},
        {"run_id": "r1", "step": "шаг", "ok": False},
    ])
    out = rt.summarize(p)
    assert out[0]["runs"] == 1, "запись без вердикта посчитали как прогон"
    assert out[0]["fail_rate"] == 1.0


def test_the_slowest_time_and_the_last_diagnosis_are_kept(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"run_id": "r1", "step": "шаг", "ok": True, "duration_s": 2.0},
        {"run_id": "r1", "step": "шаг", "ok": False, "duration_s": 9.5,
         "diag": {"url": "about:blank"}},
        {"run_id": "r1", "step": "шаг", "ok": True, "duration_s": 1.0},
    ])
    s = rt.summarize(p)[0]
    assert s["slowest_s"] == 9.5
    assert s["last_diag"] == {"url": "about:blank"}, "диагностика неудачи потеряна"


def test_only_the_last_runs_can_be_asked_for(tmp_path):
    """«Стало ли хуже» — вопрос про свежие прогоны, а не про всю историю."""
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"run_id": "старый", "step": "шаг", "ok": False},
        {"run_id": "новый", "step": "шаг", "ok": True},
    ])
    assert rt.summarize(p, last_runs=1)[0]["fail_rate"] == 0.0
    assert rt.summarize(p)[0]["fail_rate"] == 0.5


def test_an_empty_history_summarizes_to_nothing(tmp_path):
    assert rt.summarize(tmp_path / "нет.jsonl") == []


def test_run_ids_are_unique():
    assert len({rt.new_run_id() for _ in range(200)}) == 200


def test_window_state_marks_a_minimized_window():
    class P:
        def evaluate(self, js):
            return {"inner": [2048, 964], "outer": [108, 20], "dpr": 0.75, "visibility": "hidden", "focus": False}
    from src.fb_ads_rpa import run_telemetry as rt
    w = rt.window_state(P())
    assert w["minimized"] is True and w["dpr"] == 0.75


def test_window_state_never_raises():
    class P:
        def evaluate(self, js):
            raise RuntimeError("closed")
    from src.fb_ads_rpa import run_telemetry as rt
    assert rt.window_state(P()) == {}
