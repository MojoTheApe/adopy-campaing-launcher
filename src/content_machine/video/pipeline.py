"""Video Generation Pipeline.

Async job system that turns a VideoScript into a rendered short video.

Architecture:
  1. Script → scene visuals (image/video generation per scene)
  2. Voiceover generation (TTS per scene)
  3. Caption/subtitle rendering
  4. Assembly with ffmpeg/MoviePy
  5. Preview artifact → human approval → attach to ContentItem

This module provides the pipeline skeleton and a local assembly path
using MoviePy. Visual/voiceover generation hooks are pluggable.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.content_machine.video.script_agent import VideoScene, VideoScript

log = logging.getLogger(__name__)

_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "output" / "videos"


@dataclass
class VideoJob:
    job_id: str = ""
    script: VideoScript | None = None
    status: str = "pending"  # pending | generating | assembling | preview | approved | failed
    scene_assets: list[str] = field(default_factory=list)
    voiceover_paths: list[str] = field(default_factory=list)
    output_path: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Pluggable hooks (replace with real APIs) ─────────────────────────────────

async def _generate_scene_visual(scene: VideoScene, output_dir: Path) -> str:
    """Placeholder: generate or source a visual asset for one scene.

    In production, call an image/video generation API (e.g. Stability AI,
    Kling, Pexels stock). Returns path to the generated file.
    """
    placeholder = output_dir / f"scene_{scene.scene_number}.png"
    placeholder.write_text(
        f"[PLACEHOLDER] Visual prompt: {scene.visual_prompt}\n"
        f"Duration: {scene.duration_seconds}s"
    )
    log.info("Scene %d visual placeholder created: %s", scene.scene_number, placeholder)
    return str(placeholder)


async def _generate_voiceover(scene: VideoScene, output_dir: Path) -> str:
    """Placeholder: generate TTS audio for one scene.

    In production, call ElevenLabs, OpenAI TTS, or Edge TTS.
    Returns path to the audio file.
    """
    placeholder = output_dir / f"voice_{scene.scene_number}.txt"
    placeholder.write_text(f"[PLACEHOLDER] Voiceover: {scene.voiceover_text}")
    log.info("Scene %d voiceover placeholder created: %s", scene.scene_number, placeholder)
    return str(placeholder)


def _assemble_video(job: VideoJob, output_dir: Path) -> str:
    """Placeholder: assemble scenes into final video using MoviePy/ffmpeg.

    In production:
      1. Load scene images/clips with MoviePy
      2. Set durations from script
      3. Overlay captions using TextClip
      4. Concatenate scenes
      5. Mix in voiceover audio and background music
      6. Export as MP4 (9:16, 1080x1920)
    """
    output_path = output_dir / f"video_{job.job_id}.mp4"

    manifest = {
        "job_id": job.job_id,
        "scenes": len(job.scene_assets),
        "voiceovers": len(job.voiceover_paths),
        "note": "Assembly placeholder — install moviepy and replace _assemble_video for real rendering",
    }
    output_path.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False)
    )

    log.info("Video assembly placeholder created: %s", output_path.with_suffix(".json"))
    return str(output_path.with_suffix(".json"))


# ── Pipeline entry point ─────────────────────────────────────────────────────

async def run_video_job(script: VideoScript) -> VideoJob:
    """Execute the full video generation pipeline for a script.

    Returns a VideoJob with status and output path.
    """
    import uuid

    job = VideoJob(
        job_id=uuid.uuid4().hex[:8],
        script=script,
        status="generating",
    )

    output_dir = _OUTPUT_DIR / job.job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Phase 1: generate visuals and voiceovers in parallel (per scene)
        for scene in script.scenes:
            visual_path = await _generate_scene_visual(scene, output_dir)
            job.scene_assets.append(visual_path)

            voice_path = await _generate_voiceover(scene, output_dir)
            job.voiceover_paths.append(voice_path)

        # Phase 2: assemble
        job.status = "assembling"
        job.output_path = _assemble_video(job, output_dir)

        job.status = "preview"
        log.info("Video job %s ready for preview: %s", job.job_id, job.output_path)

    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        log.exception("Video job %s failed", job.job_id)

    return job
