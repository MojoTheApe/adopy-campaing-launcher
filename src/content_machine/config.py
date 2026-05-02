"""Content Machine configuration.

Telegram integration uses Bot API by default (safe, auditable).
MTProto/userbot is reserved as a future adapter only if needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TelegramConfig:
    """Bot API settings — the only supported transport for v1."""

    bot_token: str = ""
    review_chat_id: str = ""
    publish_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(
            bot_token=os.environ.get("TG_BOT_TOKEN", ""),
            review_chat_id=os.environ.get("TG_REVIEW_CHAT_ID", ""),
            publish_chat_id=os.environ.get("TG_PUBLISH_CHAT_ID", ""),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.bot_token:
            errors.append("TG_BOT_TOKEN is required")
        if not self.review_chat_id:
            errors.append("TG_REVIEW_CHAT_ID is required (private chat for draft review)")
        if not self.publish_chat_id:
            errors.append("TG_PUBLISH_CHAT_ID is required (channel/group to publish into)")
        return errors


@dataclass(frozen=True)
class LLMConfig:
    """LLM provider settings for agent calls."""

    provider: str = "openai"
    api_key: str = ""
    model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 4096

    @classmethod
    def from_env(cls) -> "LLMConfig":
        provider = os.environ.get("CM_LLM_PROVIDER", "openai").strip().lower()
        default_model = "gemini-2.5-flash" if provider == "gemini" else "gpt-4o"
        api_key_env = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
        return cls(
            provider=provider,
            api_key=os.environ.get(api_key_env, "") or os.environ.get("GOOGLE_API_KEY", ""),
            model=os.environ.get("CM_LLM_MODEL", default_model),
            temperature=float(os.environ.get("CM_LLM_TEMPERATURE", "0.7")),
            max_tokens=int(os.environ.get("CM_LLM_MAX_TOKENS", "4096")),
        )


@dataclass(frozen=True)
class ContentMachineConfig:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    db_path: str = ""
    timezone: str = "Europe/Moscow"
    shadow_mode: bool = True

    @classmethod
    def from_env(cls) -> "ContentMachineConfig":
        default_db = str(_ROOT / "data" / "content_machine.db")
        return cls(
            telegram=TelegramConfig.from_env(),
            llm=LLMConfig.from_env(),
            db_path=os.environ.get("CM_DB_PATH", default_db),
            timezone=os.environ.get("CM_TIMEZONE", "Europe/Moscow"),
            shadow_mode=os.environ.get("CM_SHADOW_MODE", "1").strip() in ("1", "true", "yes"),
        )

    def validate(self) -> list[str]:
        errors = self.telegram.validate()
        if not self.llm.api_key:
            key_name = "GEMINI_API_KEY" if self.llm.provider == "gemini" else "OPENAI_API_KEY"
            errors.append(f"{key_name} is required for LLM agents")
        if self.llm.provider not in {"openai", "gemini"}:
            errors.append("CM_LLM_PROVIDER must be either 'openai' or 'gemini'")
        return errors
