"""Video Script Agent.

Converts an approved content brief into a structured video script:
scenes, visual prompts, voiceover text, captions, and CTA overlay.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from src.content_machine.config import LLMConfig
from src.content_machine.content_language import get_content_language
from src.content_machine.llm import call_llm_json
from src.content_machine.schema import ContentItem

log = logging.getLogger(__name__)


@dataclass
class VideoScene:
    scene_number: int = 0
    duration_seconds: float = 3.0
    visual_prompt: str = ""
    voiceover_text: str = ""
    caption_text: str = ""
    overlay_text: str = ""


@dataclass
class VideoScript:
    item_id: str = ""
    title: str = ""
    total_duration: float = 0.0
    scenes: list[VideoScene] = field(default_factory=list)
    cta_overlay: str = ""
    music_mood: str = "energetic"
    aspect_ratio: str = "9:16"


def _system_prompt() -> str:
    lang = get_content_language()
    if lang.is_spanish:
        return """\
Eres guionista de videos cortos (15-60s) para un canal de trading en Telegram.

Te dan brief y tema. Crea un guion en JSON:

{
  "title": "...",
  "total_duration": 30,
  "music_mood": "energetic|calm|dramatic|upbeat",
  "scenes": [
    {
      "scene_number": 1,
      "duration_seconds": 5,
      "visual_prompt": "descripción en inglés para modelos de generación",
      "voiceover_text": "voz en off en español",
      "caption_text": "subtítulos (español)",
      "overlay_text": "texto en pantalla (si aplica, español)"
    }
  ],
  "cta_overlay": "CTA final (español)"
}

Reglas:
- 4-8 escenas, 3-8s cada una
- `visual_prompt` en inglés; voz y subtítulos en español
- No simules capturas falsas de plataformas
- No prometas ganancias
- Última escena: CTA
"""
    if lang.is_russian:
        return """\
Ты — сценарист коротких видео (15-60 сек) для трейдинг Telegram-канала.

Тебе дают бриф и тему. Создай сценарий в JSON:

{{
  "title": "...",
  "total_duration": 30,
  "music_mood": "energetic|calm|dramatic|upbeat",
  "scenes": [
    {{
      "scene_number": 1,
      "duration_seconds": 5,
      "visual_prompt": "описание визуала для генерации (на английском для AI моделей)",
      "voiceover_text": "текст озвучки (на русском)",
      "caption_text": "субтитры",
      "overlay_text": "текст поверх видео (если нужен)"
    }}
  ],
  "cta_overlay": "текст CTA в конце видео"
}}

Правила:
- 4-8 сцен, каждая 3-8 секунд.
- Visual prompts на английском, voiceover на русском.
- Не используй фейковые скриншоты торговых терминалов.
- Не обещай прибыль в сценарии.
- Последняя сцена — CTA.
"""
    return """\
You write short (15-60s) video scripts for a trading Telegram channel.

You receive a brief and a topic. Output JSON:

{
  "title": "...",
  "total_duration": 30,
  "music_mood": "energetic|calm|dramatic|upbeat",
  "scenes": [
    {
      "scene_number": 1,
      "duration_seconds": 5,
      "visual_prompt": "visual description in English for generation models",
      "voiceover_text": "voiceover in English",
      "caption_text": "captions (English)",
      "overlay_text": "on-screen text if needed (English)"
    }
  ],
  "cta_overlay": "final CTA text (English)"
}

Rules:
- 4-8 scenes, 3-8s each
- `visual_prompt` in English; voiceover/captions in English
- No fake platform screenshots
- No profit promises
- Last scene: CTA
"""


def generate_script(config: LLMConfig, item: ContentItem) -> VideoScript:
    """Generate a video script from a content brief."""
    lang = get_content_language()
    if lang.is_spanish:
        user_msg = (
            f"Tipo: {item.content_type}\n"
            f"Tema: {item.topic}\n"
            f"Brief: {item.brief}\n"
            f"Texto del post (si existe):\n{item.body[:1000] if item.body else '(ninguno)'}\n\n"
            "Crea el guion del video corto."
        )
    elif lang.is_russian:
        user_msg = (
            f"Тип контента: {item.content_type}\n"
            f"Тема: {item.topic}\n"
            f"Бриф: {item.brief}\n"
            f"Текст поста (если есть):\n{item.body[:1000] if item.body else '(нет)'}\n\n"
            "Создай сценарий короткого видео."
        )
    else:
        user_msg = (
            f"Content type: {item.content_type}\n"
            f"Topic: {item.topic}\n"
            f"Brief: {item.brief}\n"
            f"Post text (if any):\n{item.body[:1000] if item.body else '(none)'}\n\n"
            "Create a short video script."
        )

    data = call_llm_json(config, _system_prompt(), user_msg)

    scenes = [
        VideoScene(**{k: s.get(k, "") for k in VideoScene.__dataclass_fields__})
        for s in data.get("scenes", [])
    ]

    script = VideoScript(
        item_id=item.id,
        title=data.get("title", item.topic),
        total_duration=data.get("total_duration", sum(s.duration_seconds for s in scenes)),
        scenes=scenes,
        cta_overlay=data.get("cta_overlay", ""),
        music_mood=data.get("music_mood", "energetic"),
    )

    log.info("Video script generated for item %s: %d scenes, %.0fs total",
             item.id, len(scenes), script.total_duration)
    return script
