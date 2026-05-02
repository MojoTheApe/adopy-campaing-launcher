from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_build_context_packs_cli_exports_offer_country_pack(tmp_path: Path) -> None:
    kb_json = tmp_path / "knowledge_base.json"
    kb_json.write_text(
        json.dumps(
            {
                "offers": {
                    "Favorita": {
                        "audience": "EC shoppers",
                        "usp": "Local trusted brand",
                        "top_countries": ["EC"],
                        "notes": "Known winner",
                    }
                },
                "targets": {
                    "Trading": {
                        "label": "Trading",
                        "audience": "Active traders",
                        "fb_interest": "Day trading",
                        "top_countries": ["EC"],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "kb_context.db"
    out_dir = tmp_path / "packs"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_context_packs.py",
            "--kb-json",
            str(kb_json),
            "--db",
            str(db_path),
            "--output-dir",
            str(out_dir),
            "--offer",
            "Favorita",
            "--country",
            "EC",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    exported = out_dir / "offer_country_Favorita_EC.md"
    assert exported.is_file()
    text = exported.read_text(encoding="utf-8")
    assert "Offer + Country: Favorita / EC" in text
    assert "Local trusted brand" in text
