"""Redirect the hosted Ubuntu Azure archive before Chromium's apt preparation.

Only active source URIs and their local mirror lists are considered. No apt
command or network request is made here; --apt-root supports offline fixtures.
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit, urlunsplit


def _archive_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if (parsed.scheme in ("http", "https")
            and parsed.netloc == "azure.archive.ubuntu.com"
            and parsed.path in ("/ubuntu", "/ubuntu/")
            and not parsed.query and not parsed.fragment):
        return urlunsplit(parsed._replace(netloc="archive.ubuntu.com"))
    return uri


def redirect_sources(apt_root: Path) -> list[Path]:
    """Change only Ubuntu archive URI tokens; preserve signing/options/comments."""
    apt_root = apt_root.resolve()
    mirrors: set[Path] = set()

    def contained(path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(apt_root):
            raise ValueError("Refusing an apt source or mirror list outside the apt directory")
        return resolved

    def source_uri(uri: str) -> str:
        if uri.startswith("mirror+file:"):
            local = urlsplit(uri[len("mirror+"):])
            if local.netloc or local.query or local.fragment:
                raise ValueError("Expected a local apt mirror list URI")
            try:
                relative = PurePosixPath(local.path).relative_to("/etc/apt")
            except ValueError as exc:
                raise ValueError("Expected a mirror list inside /etc/apt") from exc
            mirrors.add(contained(apt_root / relative))
        return _archive_uri(uri)

    def uri_tokens(value: str) -> str:
        # Keep comments and their examples byte-for-byte as well.
        body, mark, comment = value.partition("#")
        return re.sub(r"\S+", lambda m: source_uri(m[0]), body) + mark + comment

    def deb822(text: str) -> str:
        out = []
        for paragraph in re.split(r"(\r?\n[ \t]*\r?\n)", text):
            if re.search(r"(?im)^Enabled:[ \t]*no[ \t]*\r?$", paragraph):
                out.append(paragraph)
                continue
            in_uris = False
            for line in paragraph.splitlines(keepends=True):
                field = re.match(r"(URIs:[ \t]*)(.*)", line, flags=re.I)
                if field:
                    in_uris = True
                    # Use the original suffix so CRLF/newline bytes are retained.
                    line = field.group(1) + uri_tokens(line[len(field.group(1)):])
                elif line[:1] in (" ", "\t") and in_uris:
                    line = uri_tokens(line)
                elif not line.lstrip().startswith("#"):
                    in_uris = False
                out.append(line)
        return "".join(out)

    def legacy(text: str) -> str:
        return re.sub(r"(?m)^([ \t]*deb(?:-src)?[ \t]+(?:\[[^]\r\n]*\][ \t]+)?)(\S+)",
                      lambda m: m.group(1) + source_uri(m[2]), text)

    def update(path: Path, transform) -> None:
        path = contained(path)
        before = path.read_bytes().decode("utf-8")
        after = transform(before)
        if after != before:
            path.write_bytes(after.encode("utf-8"))
            changed.append(path)
            print(f"Ubuntu archive mirror updated: {path}")
    changed: list[Path] = []
    for path in (apt_root / "sources.list.d/ubuntu.sources", apt_root / "sources.list"):
        if path.exists():
            update(path, deb822 if path.suffix == ".sources" else legacy)
    for path in sorted(mirrors):
        # In a mirror list only the first token is its URI; metadata is preserved.
        update(path, lambda text: re.sub(r"(?m)^([ \t]*)([^#\s]\S*)",
                                      lambda m: m[1] + _archive_uri(m[2]), text))
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apt-root", type=Path, default=Path("/etc/apt"))
    redirect_sources(parser.parse_args().apt_root)


if __name__ == "__main__":
    main()
