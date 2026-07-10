from __future__ import annotations

from pathlib import Path


class CreativeNotFoundError(FileNotFoundError):
    pass


class AmbiguousCreativeError(ValueError):
    pass


def find_creative_file(root: str | Path, filename: str) -> Path:
    """
    Recursively search root for filename (basename match, case-insensitive).

    If filename contains path separators (e.g. 'Latam/Local/CL/file.mp4'), a
    path-suffix match is used so callers can disambiguate duplicates.

    Raises CreativeNotFoundError or AmbiguousCreativeError.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise FileNotFoundError(f"Creatives root not found: {root_path}")

    name = (filename or "").strip()
    if not name:
        raise CreativeNotFoundError("Creative filename is empty")

    # Normalize separators so both / and \ work
    name_norm = name.replace("\\", "/")
    has_path = "/" in name_norm

    target_basename = Path(name).name.lower()
    matches: list[Path] = []

    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() != target_basename:
            continue
        if has_path:
            # Path-suffix match: the relative path must end with the provided subpath
            rel = path.relative_to(root_path).as_posix().lower()
            if rel.endswith(name_norm.lower()):
                matches.append(path)
        else:
            matches.append(path)

    if not matches:
        raise CreativeNotFoundError(f"Creative not found under {root_path}: {name}")
    if len(matches) > 1:
        rel = ", ".join(str(m.relative_to(root_path)) for m in matches[:5])
        suffix = " …" if len(matches) > 5 else ""
        raise AmbiguousCreativeError(
            f"Multiple files named {name!r} under {root_path}: {rel}{suffix}"
        )
    return matches[0]
