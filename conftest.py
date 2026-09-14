"""Падение не должно теряться вместе с прокруткой терминала.

За один день четыре теста мигнули: упали в полном прогоне и прошли
поодиночке. Каждый раз текста падения у нас не было — прогон запускали с
`| tail`, чтобы увидеть итог, и подробности уезжали в никуда. Разбор
начинался с попытки воспроизвести, а мигающий тест на то и мигающий, что
воспроизводится не всегда.

Поэтому след остаётся сам, с первого раза и у кого угодно: после прогона,
в котором что-то упало, рядом появляется файл с полным текстом. Никаких
флагов помнить не надо — забытый флаг не работает ровно тогда, когда он
нужнее всего.

Файлы живут в `output/`, который не коммитится, и старые убираются: сотня
отчётов за месяц — это не архив, а мусор.
"""
from __future__ import annotations

import datetime as _dt
import os
import platform
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REPORTS = _ROOT / "output" / "test-failures"
_KEEP = 20

_failures: list[dict] = []


def pytest_sessionstart(session) -> None:
    # pytest.main can run more than one session in the same Python process.
    _failures.clear()


def _remember_failure(report, when: str) -> None:
    try:
        if report.failed:
            _failures.append({
                "id": report.nodeid,
                "when": when,
                "seconds": round(getattr(report, "duration", 0.0), 2),
                "text": report.longreprtext,
                "sections": list(getattr(report, "sections", [])),
            })
    except Exception:
        pass  # Reporting must not change the run's outcome.


def pytest_runtest_logreport(report) -> None:
    if report.when in ("setup", "call", "teardown"):
        _remember_failure(report, report.when)


def pytest_collectreport(report) -> None:
    _remember_failure(report, "collect")


def _trim(current: Path) -> None:
    files = sorted(p for p in _REPORTS.glob("*.md") if p != current)
    for stale in files[:max(0, len(files) - (_KEEP - 1))]:
        try:
            stale.unlink()
        except OSError:
            pass


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Пишем в конце и говорим куда.

    Именно этот хук, а не `sessionfinish`: только здесь строка попадает в
    итог прогона. Файл, о котором знают лишь те, кто о нём и так знал, —
    половина пользы.
    """
    if not _failures:
        return
    try:
        _REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        path = _REPORTS / f"{stamp}_{uuid.uuid4().hex}.md"

        head = [
            f"# Падения прогона · {stamp}",
            "",
            f"- всего упало: **{len(_failures)}**",
            f"- команда: `{' '.join(sys.argv)}`",
            f"- python {platform.python_version()} на {platform.system()} {platform.release()}",
            f"- рабочая папка: `{os.getcwd()}`",
            "",
            "Мигающий тест воспроизводится не всегда, поэтому текст сохранён с",
            "первого раза. Прежде чем гоняться за повтором — прочитать здесь.",
            "",
        ]
        body = []
        for f in _failures:
            body += [
                "---",
                "",
                f"## {f['id']}",
                "",
                f"Упало на этапе **{f['when']}**, {f['seconds']} с.",
                "",
                "```",
                (f["text"] or "").rstrip(),
                "```",
                "",
            ]
            for title, content in f["sections"]:
                body += [f"### {title}", "", "```", str(content).rstrip(), "```", ""]
        # Exclusive creation prevents one run from replacing another report.
        with path.open("x", encoding="utf-8") as output:
            output.write("\n".join(head + body))
        _trim(path)
        terminalreporter.write_line(f"текст падений сохранён: {path}", bold=True)
    except Exception:
        pass          # см. выше: отчётность не ломает прогон
