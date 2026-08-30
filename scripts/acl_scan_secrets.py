"""Refuse to ship a tree that carries someone's real identifiers or a secret.

ACL is pushed to three repositories, one of which outside developers read, and
it is copied onto every media buyer's machine. Two different things must never
travel with it: credentials, and the identifiers that say which real ad
accounts and browser profiles a person works in.

Scans TRACKED files only — untracked working files are that machine's own
business, and the point here is what git carries.

This does not clean history; `config/ad_account_map.csv` lived in git for
months and its old contents are still in old commits. Rewriting shared history
is a separate, coordinated decision (audit finding F-18). What this guards is
the tree and every commit from here on.

    python scripts/acl_scan_secrets.py          # exits 1 on a finding
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows consoles are not UTF-8 by default. This script died on CI printing
# its own all-clear line — the scan passed, the build went red. See
# scripts/_console.py.
from scripts._console import use_utf8  # noqa: E402

use_utf8()

# Files that legitimately talk ABOUT identifiers without containing real ones:
# documentation quoting a URL shape, tests using obviously-fake short ids.
_ALLOW_PATHS = (
    "scripts/acl_scan_secrets.py",
    "config/ad_account_map.example.csv",
)

# Placeholders are allowed BY VALUE, not by file. Exempting whole files would
# mean a real id pasted into a documentation example never gets caught — which
# is exactly how the two handoff docs ended up carrying live cabinets.
_PLACEHOLDERS = {
    "act_1234567890",
    "act_1234567890123456",
    "act=1234567890",
    "act=1234567890123456",
    # The canonical example UUIDs from RFC 4122 and every UUID tutorial.
    "550e8400-e29b-41d4-a716-446655440000",
    "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "00000000-0000-0000-0000-000000000000",
}

_RULES: list[tuple[str, str, str]] = [
    # (name, regex, why it matters)
    (
        "ad account id",
        # Both spellings: the URL form act=<id> is the same cabinet, and
        # copying an Ads Manager link into a doc is the likeliest way it leaks.
        r"act[_=]\d{10,}",
        "настоящий кабинет — по нему видно, в каких аккаунтах работает компания",
    ),
    (
        "Multilogin profile id",
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "id профиля Multilogin — связывает аккаунт с конкретной браузерной личностью",
    ),
    (
        "private key",
        r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----",
        "приватный ключ",
    ),
    (
        "service account key",
        r'"type"\s*:\s*"service_account"',
        "ключ сервисного аккаунта Google",
    ),
    (
        # The key BODY, not just the prefix: help text telling someone to export
        # an "sk-ant-oat01-… token" is documentation, not a leak.
        "API key",
        r"\bsk-(?:ant|proj)-[A-Za-z0-9_-]{24,}|\bsk-[A-Za-z0-9]{32,}",
        "ключ доступа к платному API",
    ),
    (
        "Multilogin token",
        r"MULTILOGIN_AUTOMATION_TOKEN[_A-Z]*\s*=\s*ey[A-Za-z0-9_-]{10,}",
        "рабочий токен Multilogin",
    ),
]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=str(_ROOT), capture_output=True, text=True, timeout=60
    )
    if out.returncode != 0:
        raise RuntimeError(
            "не удалось получить список файлов из git — сканировать нечего.\n"
            "Проверка, которая молча рапортует «чисто», когда она ничего не "
            "смотрела, опаснее её отсутствия: зелёная галочка появляется, а "
            "гарантии за ней нет."
        )
    return [f for f in out.stdout.splitlines() if f.strip()]


def main() -> int:
    findings: list[str] = []

    try:
        tracked = _tracked_files()
    except RuntimeError as exc:
        print(exc)
        return 1

    for rel in tracked:
        if rel in _ALLOW_PATHS:
            continue
        path = _ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue  # binary or unreadable — nothing to match anyway

        for name, pattern, why in _RULES:
            for m in re.finditer(pattern, text):
                found = m.group(0)
                if found in _PLACEHOLDERS:
                    continue
                line = text[: m.start()].count("\n") + 1
                if len(found) > 24:
                    found = found[:24] + "…"
                findings.append(f"{rel}:{line}  {name}: {found}  — {why}")

    if findings:
        print("В отслеживаемых файлах найдены идентификаторы или секреты:\n")
        for f in findings:
            print(f"  {f}")
        print(
            "\nЭти файлы уезжают и в рабочий репозиторий, который читают посторонние\n"
            "разработчики, и на машину каждого байера. Выведите файл из-под гита\n"
            "(git rm --cached <файл>) и добавьте его в .gitignore."
        )
        return 1

    print("Секретов и настоящих идентификаторов в отслеживаемых файлах нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
