"""Analyze a creative file (image or video frame) with GPT-4o Vision.

Returns structured visual analysis:
  scene, emotion, hook, cta, style, colors, has_people, visual_pattern, weaknesses
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}

_ANALYSIS_PROMPT = """\
Analyze this Facebook ad creative image as a professional media buyer. Be concise and specific.

Return a JSON object with exactly these keys:
{
  "scene": "what is shown visually (1 sentence)",
  "emotion": "primary emotion evoked (1 word or short phrase)",
  "hook": "the visual hook or attention-grabbing element",
  "cta": "call-to-action if visible, else empty string",
  "style": "one of: lifestyle, product, text-heavy, testimonial, news-style, animated, mixed",
  "colors": "dominant color palette (2-3 colors)",
  "has_people": true or false,
  "visual_pattern": "what visual storytelling pattern is used (e.g. before/after, authority figure, social proof, urgency, curiosity gap)",
  "strengths": "what works well visually for conversion (1-2 sentences)",
  "weaknesses": "what could be improved (1-2 sentences)"
}

Respond ONLY with the JSON object, no markdown fences."""


def _extract_video_frame(video_path: Path) -> tuple[Optional[bytes], str]:
    """First keyframe of a video as JPEG bytes, plus a reason when there is none.

    A fifth of the creatives library is video (1020 files of 4952 on
    2026-08-31), so this is not an edge case. The caller used to turn every
    failure here into "Cannot encode file: name.mp4" — which names the file and
    not the cause, and nobody would ever connect that to a missing package.
    """
    try:
        import cv2  # type: ignore
    except ImportError:
        return None, (
            "разбор видео требует opencv: pip install opencv-python-headless"
        )
    try:
        cap = cv2.VideoCapture(str(video_path))
        success, frame = cap.read()
        cap.release()
        if not success:
            return None, "не удалось прочитать первый кадр — файл повреждён или пуст"
        _, buf = cv2.imencode(".jpg", frame)
        return buf.tobytes(), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _file_to_base64(path: Path) -> tuple[Optional[tuple[str, str]], str]:
    """((base64, mime), "") for an image or video frame, or (None, reason).

    The reason travels with the failure so the operator is told what to do
    about it, instead of reading "Cannot encode file" and guessing.
    """
    ext = path.suffix.lower()

    if ext in SUPPORTED_IMAGE_EXTS:
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
        }
        mime = mime_map.get(ext, "image/jpeg")
        with open(path, "rb") as f:
            return (base64.b64encode(f.read()).decode(), mime), ""

    if ext in SUPPORTED_VIDEO_EXTS:
        frame_bytes, why = _extract_video_frame(path)
        if frame_bytes:
            return (base64.b64encode(frame_bytes).decode(), "image/jpeg"), ""
        return None, why

    # Кавычки внутри f-строки только одинарные: вложенные двойные разрешены
    # с Python 3.12, а установщик объявляет минимумом 3.11.
    return None, f"формат {ext or 'без расширения'} не поддерживается"


def analyze(
    file_path: Path,
    *,
    api_key: Optional[str] = None,
    model: str = "gpt-4o-mini",
) -> Dict[str, Any]:
    """Analyze a creative file with GPT-4o Vision.

    Returns a dict with visual analysis fields.
    Falls back to empty analysis dict when API key is missing or call fails.
    """
    key = (api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not key:
        return _empty_analysis("OPENAI_API_KEY not set")

    encoded, why = _file_to_base64(file_path)
    if encoded is None:
        return _empty_analysis(f"{file_path.name}: {why}")

    b64_data, mime_type = encoded

    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        return _empty_analysis("openai package not installed — run: pip install openai")

    client = OpenAI(api_key=key)

    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=600,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_data}",
                                "detail": "low",
                            },
                        },
                        {"type": "text", "text": _ANALYSIS_PROMPT},
                    ],
                }
            ],
        )
    except Exception as e:
        return _empty_analysis(f"OpenAI API error: {e}")

    raw = response.choices[0].message.content or ""
    return _parse_vision_response(raw)


def _parse_vision_response(raw: str) -> Dict[str, Any]:
    import json
    text = raw.strip()
    # Strip markdown fences if model added them anyway
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            l for l in lines
            if not l.strip().startswith("```")
        ).strip()
    try:
        data = json.loads(text)
        return {
            "scene": str(data.get("scene", "")),
            "emotion": str(data.get("emotion", "")),
            "hook": str(data.get("hook", "")),
            "cta": str(data.get("cta", "")),
            "style": str(data.get("style", "")),
            "colors": str(data.get("colors", "")),
            "has_people": bool(data.get("has_people", False)),
            "visual_pattern": str(data.get("visual_pattern", "")),
            "strengths": str(data.get("strengths", "")),
            "weaknesses": str(data.get("weaknesses", "")),
            "error": "",
        }
    except (json.JSONDecodeError, ValueError):
        return _empty_analysis(f"JSON parse failed: {raw[:120]}")


def _empty_analysis(reason: str = "") -> Dict[str, Any]:
    return {
        "scene": "", "emotion": "", "hook": "", "cta": "",
        "style": "", "colors": "", "has_people": False,
        "visual_pattern": "", "strengths": "", "weaknesses": "",
        "error": reason,
    }
