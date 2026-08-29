"""Guards for what a fresh ACL install inherits from the repository.

ACL is copied onto every media buyer's machine, so anything person-specific
left in a tracked template travels with it. That already happened: `.env.example`
carried a named employee's e-mail, CM name and absolute `C:\\Users\\<name>` paths,
so a new install started life with someone else's identity and pointed at paths
that do not exist on it (audit finding F-10, 2026-08-26).

These tests are the standing check that it does not come back. They read the
files as data — no imports, no side effects — so they stay fast and can run in
CI before anything is built.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE = _ROOT / ".env.example"
_ACCOUNT_MAP_EXAMPLE = _ROOT / "config" / "ad_account_map.example.csv"

# Values that identify a person or one particular machine. Kept as patterns
# rather than a literal name so a different colleague's data is caught too.
_PERSONAL_PATTERNS = [
    (r"[A-Za-z0-9._%+-]+@adopy\.com", "рабочая почта конкретного человека"),
    (r"[Cc]:\\Users\\(?!<)[A-Za-z0-9._-]+", "абсолютный путь с чужим именем пользователя"),
    (r"/Users/(?!<)[A-Za-z0-9._-]+", "абсолютный путь с чужим именем пользователя"),
]

_PLACEHOLDER_OK = {"you@adopy.com", "name@example.com", "user@example.com"}


def _uncommented_values(text: str) -> list[tuple[int, str]]:
    """(line number, value) for every KEY=VALUE that is actually set."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        _key, _, value = stripped.partition("=")
        if value.strip():
            out.append((n, value.strip()))
    return out


def test_env_example_exists():
    assert _ENV_EXAMPLE.is_file(), ".env.example пропал — новой машине нечего копировать"


def test_env_example_has_no_personal_values():
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    problems = []
    for pattern, why in _PERSONAL_PATTERNS:
        for m in re.finditer(pattern, text):
            found = m.group(0)
            if found in _PLACEHOLDER_OK:
                continue
            line = text[: m.start()].count("\n") + 1
            problems.append(f"строка {line}: {found!r} — {why}")
    assert not problems, (
        "В .env.example попали личные данные. Свежая установка унаследовала бы "
        "чужую личность:\n  " + "\n  ".join(problems)
    )


def test_env_example_does_not_hardcode_a_cm_name():
    """MY_CM_NAME decides whose accounts the dashboard shows — it must be blank."""
    for line_no, value in _uncommented_values(_ENV_EXAMPLE.read_text(encoding="utf-8")):
        if value and line_no and _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()[
            line_no - 1
        ].startswith("MY_CM_NAME="):
            pytest.fail(
                f"MY_CM_NAME задан в шаблоне ({value!r}). Новая установка показывала бы "
                "аккаунты чужого байера, пока никто этого не заметит."
            )


def test_telemetry_docs_name_the_transport_that_works():
    """One story about telemetry auth, not two (audit finding F-11).

    The runtime picks user OAuth because Workspace policy blocks the
    alternatives; a template still instructing a service account sends the
    installer down a path that cannot work.
    """
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    if "GSHEET_TELEMETRY_ID" not in text:
        pytest.skip("в шаблоне нет раздела телеметрии")
    assert "OAuth" in text, "шаблон не называет способ доступа, который действительно работает"
    assert "GSHEET_TELEMETRY_SA_JSON" not in text, (
        "шаблон всё ещё предлагает сервисный аккаунт — этот путь закрыт политикой "
        "Workspace, и установщик потратит на него время впустую"
    )


def test_account_map_example_carries_no_real_ids():
    """The example map must not ship anyone's real ad accounts or profiles."""
    if not _ACCOUNT_MAP_EXAMPLE.is_file():
        pytest.skip("нет config/ad_account_map.example.csv")
    text = _ACCOUNT_MAP_EXAMPLE.read_text(encoding="utf-8")
    # Real Meta ad account ids are long digit runs; the example uses a short
    # obviously-fake one.
    real_looking = re.findall(r"act_(\d{12,})", text)
    assert not real_looking, (
        f"в примере карты аккаунтов настоящие идентификаторы кабинетов: {real_looking}"
    )
