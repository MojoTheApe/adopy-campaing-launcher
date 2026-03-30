"""Knowledge Agent — слой знаний: таргеты, офферы, LP, история тестов."""
from .knowledge_base import (
    get_target_context,
    get_offer_context,
    get_all_targets,
    get_all_offers,
    load_markdown_section,
    resolve_target_code,
    log_unknown_target,
    format_target_context_line,
    reload_cache,
)

__all__ = [
    "get_target_context",
    "get_offer_context",
    "get_all_targets",
    "get_all_offers",
    "load_markdown_section",
    "resolve_target_code",
    "log_unknown_target",
    "format_target_context_line",
    "reload_cache",
]
