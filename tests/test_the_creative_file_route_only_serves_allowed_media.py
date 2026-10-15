"""ACL-1342 (аудит безопасности, S02): `/creative-file` отдаёт только то,
что действительно картинка или видео из разрешённого списка — и только из
`get_creatives_root()`.

До фикса `FileResponse(resolved)` отдавал что угодно, что `find_creative_file`
нашло по имени — расширение никто не проверял, и содержимое тоже. Тест
кладёт `.html` файл (с картиночным расширением тоже — переименованное имя
не должно спасать) рядом с настоящими jpg/png и проверяет, что сервер
смотрит на байты, а не на имя.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web import app as web  # noqa: E402

# Минимальные, но настоящие по сигнатуре заголовки.
_JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32
_PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_HTML_BODY = b"<html><script>alert(document.cookie)</script></html>"


def _call(path: str):
    return asyncio.run(web.creative_file(path=path))


@pytest.fixture
def creatives_root(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tmp_path))
    return tmp_path


def test_a_real_jpeg_is_served_with_the_right_content_type(creatives_root):
    (creatives_root / "pic.jpg").write_bytes(_JPEG_HEAD)
    resp = _call("pic.jpg")
    assert resp.status_code == 200
    assert resp.media_type == "image/jpeg"


def test_a_real_png_is_served_with_the_right_content_type(creatives_root):
    (creatives_root / "pic.png").write_bytes(_PNG_HEAD)
    resp = _call("pic.png")
    assert resp.status_code == 200
    assert resp.media_type == "image/png"


def test_an_html_file_is_rejected_even_with_a_media_extension(creatives_root):
    (creatives_root / "evil.jpg").write_bytes(_HTML_BODY)
    resp = _call("evil.jpg")
    assert resp.status_code == 415


def test_a_plain_html_file_is_rejected(creatives_root):
    (creatives_root / "evil.html").write_bytes(_HTML_BODY)
    resp = _call("evil.html")
    assert resp.status_code == 415


def test_a_symlink_escaping_the_creatives_root_is_not_served(creatives_root):
    outside = creatives_root.parent / "outside_secret"
    outside.mkdir()
    secret = outside / "secret.jpg"
    secret.write_bytes(_JPEG_HEAD)  # real jpeg bytes — extension/content aren't the issue here

    link = creatives_root / "escape.jpg"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("нет прав на создание symlink в этом окружении")

    resp = _call("escape.jpg")
    assert resp.status_code == 404


def test_a_symlink_inside_the_root_still_serves(creatives_root):
    real = creatives_root / "sub"
    real.mkdir()
    real_file = real / "real.jpg"
    real_file.write_bytes(_JPEG_HEAD)

    link = creatives_root / "alias.jpg"
    try:
        os.symlink(real_file, link)
    except (OSError, NotImplementedError):
        pytest.skip("нет прав на создание symlink в этом окружении")

    resp = _call("alias.jpg")
    assert resp.status_code == 200
    assert resp.media_type == "image/jpeg"
