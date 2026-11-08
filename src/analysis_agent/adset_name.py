"""ACL-2250: счётчик повтора в конце имени набора (`X copy 3`) не часть имени для разбора статистики."""
from __future__ import annotations

import re

_COPY_TAIL_RE = re.compile(r"\s+copy\s+\d+\s*$")


def strip_copy_counter(name):
    """`..._35 copy 1` -> `..._35`; без счётчика или не строка - как есть. Копия группируется с оригиналом."""
    if not isinstance(name, str):
        return name
    return _COPY_TAIL_RE.sub("", name)
