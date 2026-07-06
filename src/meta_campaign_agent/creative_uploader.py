from __future__ import annotations

import json
from pathlib import Path

from src.meta_campaign_agent.meta_client import MetaClient, MetaGraphError


def _cache_key(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"


class CreativeUploader:
    """Upload local image/video files; cache hash/video_id by path+mtime."""

    def __init__(self, client: MetaClient, cache_path: str | Path | None = None):
        self.client = client
        self.cache_path = Path(cache_path or "output/meta_creative_cache.json")
        self._cache: dict[str, dict[str, str]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.is_file():
            self._cache = {}
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._cache = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            self._cache = {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")

    def upload_image(self, ad_account_id: str, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))

        key = _cache_key(path)
        cached = self._cache.get(key, {})
        if cached.get("type") == "image" and cached.get("hash"):
            return cached["hash"]

        with path.open("rb") as f:
            resp = self.client.post_multipart(
                f"{ad_account_id}/adimages",
                data={},
                files={"filename": (path.name, f, "application/octet-stream")},
            )

        image_hash: str | None = None
        images = resp.get("images")
        if isinstance(images, dict):
            for entry in images.values():
                if isinstance(entry, dict) and entry.get("hash"):
                    image_hash = str(entry["hash"])
                    break
        if not image_hash:
            raise MetaGraphError(f"Image upload returned no hash: {resp}")

        self._cache[key] = {"type": "image", "hash": image_hash, "file": str(path)}
        self._save_cache()
        return image_hash

    def upload_video(self, ad_account_id: str, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))

        key = _cache_key(path)
        cached = self._cache.get(key, {})
        if cached.get("type") == "video" and cached.get("video_id"):
            return cached["video_id"]

        with path.open("rb") as f:
            resp = self.client.post_multipart(
                f"{ad_account_id}/advideos",
                data={"title": path.stem},
                files={"source": (path.name, f, "application/octet-stream")},
            )

        video_id = resp.get("id")
        if not video_id:
            raise MetaGraphError(f"Video upload returned no id: {resp}")

        self._cache[key] = {"type": "video", "video_id": str(video_id), "file": str(path)}
        self._save_cache()
        return str(video_id)
