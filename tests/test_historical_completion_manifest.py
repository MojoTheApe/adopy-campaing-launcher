"""Guard the owner-approved historical-completion manifest in tracker/config.json.

The 74 rows approved in the ACL-1248 adoption (2026-09-21) are frozen. ACL-1250
was added later by a separate owner-approved amendment (ACL-2200). Any further
addition needs its own explicit owner decision and a reviewed update here.
"""
import hashlib
import json
import re
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / 'tracker' / 'config.json'
ORIGINAL_74_SHA256 = 'bf0dec047d5e06b2a6928654446998246de0ee3847ac69e7ea6ced627c8b93cc'
AMENDMENTS = {'250': 'ACL-1250'}


def manifest():
    return json.loads(CONFIG.read_text(encoding='utf-8'))['historical_completion']


def test_original_74_rows_are_unchanged():
    tasks = manifest()['tasks']
    original = {k: v for k, v in tasks.items() if k not in AMENDMENTS}
    assert len(original) == 74
    digest = hashlib.sha256(json.dumps(original, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert digest == ORIGINAL_74_SHA256


def test_only_the_approved_amendment_was_added():
    tasks = manifest()['tasks']
    assert len(tasks) == 75
    for number, identity in AMENDMENTS.items():
        assert tasks[number]['id'] == identity
        assert re.fullmatch('[0-9a-f]{64}', tasks[number]['issue_sha256'])
        assert set(tasks[number]) == {'id', 'issue_sha256'}


def test_provenance_points_at_the_pre_policy_configuration():
    value = manifest()
    assert re.fullmatch('[0-9a-f]{40}', value['source_revision'])
    assert value['source_config_sha256'] == '0c19cf33cae0555059eb6af9ab2d0a344a65b4916572de934c9b685404b61d18'
    assert 'ACL-1250' in value['approved_by'] and 'ACL-1248' in value['approved_by']
