"""Тёмная тема: токены, переключатель и то, что перекрашивать нельзя.

Палитра собрана по разбору Material 3, IBM Carbon g100 и GitHub Primer dark и
проверена расчётом контраста, а не на глаз. Проверки ниже держат решения,
которые легко потерять при следующей правке стилей.
"""
from __future__ import annotations

import re
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _design_tokens import tokens  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _block(selector: str) -> str:
    t = TPL.read_text(encoding="utf-8")
    at = t.index(selector)
    return t[at: t.index("}", at)]


def _tokens(selector: str) -> dict[str, str]:
    # ACL-1797: палитра в adopy-tokens.css, имена шаблона — ссылки на неё.
    return tokens("dark" if "dark" in selector else "light")


def _lum(hexs: str) -> float:
    h = hexs.strip().lstrip("#")
    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_no_token_points_at_itself():
    """`--accent: var(--accent)` — цвет гаснет молча, без единой жалобы.

    Так и вышло при переводе зашитых цветов в токены: замена по всему файлу
    задела и само определение. Браузер на такое не ругается — свойство просто
    перестаёт иметь значение.
    """
    t = TPL.read_text(encoding="utf-8")
    bad = re.findall(r"(--[\w-]+):\s*var\(\s*\1\s*[,)]", t)
    assert not bad, "токен ссылается сам на себя: " + ", ".join(bad)


def test_the_dark_palette_is_readable():
    """Каждая пара текста считается, а не оценивается на глаз."""
    d = _tokens(':root[data-theme="dark"]')
    checks = [
        ("текст на фоне", d["--text"], d["--bg"], 7.0),
        ("текст на карточке", d["--text"], d["--bg-elev"], 7.0),
        ("приглушённый на карточке", d["--muted"], d["--bg-elev"], 4.5),
        ("акцент на карточке", d["--accent"], d["--bg-elev"], 4.5),
        ("плохо на карточке", d["--bad"], d["--bg-elev"], 4.5),
        ("хорошо на карточке", d["--good"], d["--bg-elev"], 4.5),
        ("внимание на карточке", d["--warn"], d["--bg-elev"], 4.5),
    ]
    for name, fg, bg, need in checks:
        got = _ratio(fg, bg)
        assert got >= need, f"{name}: {got:.2f}:1, нужно {need}"


def test_the_ground_is_not_pure_black():
    """Чистый чёрный даёт ореол вокруг букв на OLED, а тени на нём не видны."""
    d = _tokens(':root[data-theme="dark"]')
    assert d["--bg"].strip().lower() not in ("#000", "#000000"), "основа стала чистым чёрным"
    assert _lum(d["--bg"]) > 0.002, "основа слишком близка к чёрному"


def test_the_text_is_not_pure_white():
    d = _tokens(':root[data-theme="dark"]')
    assert d["--text"].strip().lower() not in ("#fff", "#ffffff"), "текст стал чистым белым"


def test_elevated_surfaces_are_lighter_than_the_ground():
    """Возвышение на тёмном показывается светлотой, а не тенью."""
    d = _tokens(':root[data-theme="dark"]')
    assert _lum(d["--bg-elev"]) > _lum(d["--bg"]), "карточка не светлее фона"


def test_the_facebook_preview_is_left_alone():
    """Это изображение чужой ленты, а не наш интерфейс.

    Перекрасить его в тёмное значит показать байеру то, чего он никогда не
    увидит у себя в Ads Manager.
    """
    t = TPL.read_text(encoding="utf-8")
    for colour in ("#65676b", "#050505", "#e4e6e9"):
        assert colour in t, f"цвет ленты {colour} исчез — макет перекрасили"


def test_the_old_hardcoded_ui_colours_are_gone():
    """Пятна, которые остались бы светлыми в тёмной теме."""
    t = TPL.read_text(encoding="utf-8")
    for colour in ("#b91c1c", "#a4362f", "#4caf50", "#1d4ed8"):
        outside = t.count(colour) - _block(":root").count(colour)
        assert outside <= 0, f"{colour} снова зашит мимо токенов"


def test_the_theme_is_set_before_the_page_paints():
    """Иначе ночью экран на мгновение вспыхивает светлым.

    Ровно то, от чего тёмную тему и включают.
    """
    t = TPL.read_text(encoding="utf-8")
    head = t[: t.index("</head>")]
    assert "acl.theme" in head, "тема выставляется не в <head> — экран мигнёт светлым"
    assert "data-theme" in head


def test_the_switch_exists_in_the_header():
    t = TPL.read_text(encoding="utf-8")
    header = t[t.index('<header class="page-header">'): t.index("</header>")]
    assert 'id="theme-toggle"' in header, "переключателя нет в шапке"
    assert "aria-label" in header, "у переключателя нет имени для чтения с экрана"
    assert 'role="switch"' in header, (
        "это снова кнопка, а не переключатель: состояние важнее действия"
    )
    assert "ts-knob" in header, "у переключателя нет бегунка — положение не видно"


def test_an_unreadable_storage_does_not_break_the_page():
    """Приватное окно и запрет на хранилище — обычное дело, а не отказ."""
    t = TPL.read_text(encoding="utf-8")
    at = t.index("localStorage.getItem('acl.theme')")
    around = t[at - 200: at + 400]
    assert "catch" in around, "чтение темы не обёрнуто — страница упадёт без хранилища"
