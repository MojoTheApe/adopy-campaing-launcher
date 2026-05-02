"""Output language for generated content and bot UX strings."""

from __future__ import annotations

import os
from dataclasses import dataclass

_ENV_KEYS = (
    "CM_CONTENT_LANG",  # preferred
    "CM_OUTPUT_LANG",     # alias
    "CM_LANG",            # alias
)


@dataclass(frozen=True)
class ContentLanguage:
    code: str  # BCP-47-ish: es, ru, en, ...

    @property
    def is_spanish(self) -> bool:
        return self.code.lower().startswith("es")

    @property
    def is_russian(self) -> bool:
        return self.code.lower().startswith("ru")

    @property
    def is_english(self) -> bool:
        return self.code.lower().startswith("en")


def get_content_language() -> ContentLanguage:
    for key in _ENV_KEYS:
        raw = os.environ.get(key, "").strip()
        if raw:
            code = raw.split("-", 1)[0].strip()
            return ContentLanguage(code=code.lower() or "en")
    return ContentLanguage(code="en")
