"""ACL-1797: токены страницы так, как их видит браузер, — со всеми `var()` раскрытыми.

Палитра лежит в `src/web/templates/adopy-tokens.css`, а старые имена шаблона
(`--bg`, `--accent`…) ссылаются на неё через `var(--adopy-*)`. Проверки
контраста раньше разбирали шаблон регулярным выражением и ждали готовые
цвета; теперь они берут итоговые значения отсюда.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS_CSS = ROOT / "src" / "web" / "templates" / "adopy-tokens.css"
TEMPLATE = ROOT / "src" / "web" / "templates" / "report.html"

LIGHT = ":root {"
DARK = ':root[data-theme="dark"] {'
_DECL = re.compile(r"(--[\w-]+):\s*([^;]+);")


def _block(text: str, selector: str) -> dict[str, str]:
    at = text.index(selector)
    # Закрывающая скобка блока — первая «}» в начале строки (в CSS-комментариях скобок нет).
    end = re.compile(r"^\s*\}", re.M).search(text, at).start()
    return dict(_DECL.findall(text[at:end]))


def tokens_css() -> str:
    """Текст файла токенов — для тестов, которые собирают страницу из кусков CSS шаблона."""
    return TOKENS_CSS.read_text(encoding="utf-8")


def tokens(theme: str) -> dict[str, str]:
    """Значения всех кастомных свойств темы; `var(--x)` раскрыт до цвета."""
    css = TOKENS_CSS.read_text(encoding="utf-8")
    tpl = TEMPLATE.read_text(encoding="utf-8")
    tpl = tpl[tpl.index("<style>"): tpl.index("</style>")]
    merged = {**_block(css, LIGHT), **_block(tpl, LIGHT)}
    if theme == "dark":
        merged.update({**_block(css, DARK), **_block(tpl, DARK)})

    def resolve(value: str, depth: int = 0) -> str:
        assert depth < 10, f"цикл в токенах: {value}"
        m = re.search(r"var\(\s*(--[\w-]+)\s*(?:,[^)]*)?\)", value)
        if not m:
            return value.strip()
        return resolve(value.replace(m.group(0), merged[m.group(1)].strip()), depth + 1)

    return {k: resolve(v) for k, v in merged.items()}
