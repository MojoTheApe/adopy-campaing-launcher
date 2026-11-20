"""ACL-2787: нативная кнопка Ads Manager «Use Facebook Page» внутри профиля аккаунта.

Публичное ребро POST для Page-backed Instagram отклонено (ACL-2221), поэтому создать идентичность можно
только нативным контролом Ads Manager: новое неотправленное объявление -> блок «Instagram profile» ->
«Use Facebook Page» для ровно этой Страницы -> черновик отбрасывается, ничего не публикуется.

СЕЙЧАС драйвер НЕ ВКЛЮЧЁН: живой DOM контрола и поток после клика не сняты, а в
config/ads_manager_selectors.yaml селекторов блока идентичности нет. `press` ничего не открывает и не
кликает и возвращает причину; creator тогда показывает прежнее сообщение с ручными шагами.
Что нужно проверить живьём (тестовый профиль, не боевой аккаунт), перед включением:
- блок «Instagram profile» нового объявления: заголовок (role=heading) и рядом span «Use Facebook Page»
  (так в tests/fixtures/ads_manager/simple_create_2026_09_27.html);
- что происходит после клика (диалог/выбор Страницы), как отбрасывается черновик без Publish и без
  диалога «Leave site?»;
- что вкладка открывается только в профиле этого аккаунта (рабочая вкладка src/fb_api/light_tab.py).
"""
from __future__ import annotations

#: Причина для журнала/прогресса, пока драйвер не включён.
NOT_LOCATED = "the 'Use Facebook Page' control is not wired to a verified Ads Manager selector yet"


def press(act: str, page_id: str) -> str:
    """Нажать «Use Facebook Page» для Страницы. Возвращает "" при успехе, иначе причину отказа.

    Ничего не делает, пока селектор и поток не подтверждены живой проверкой (см. docstring модуля).
    """
    return NOT_LOCATED
