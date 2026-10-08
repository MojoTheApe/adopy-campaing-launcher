"""ACL-1729: сопоставление алиаса не должно считаться на каждую строку книги."""
import pandas as pd

from src.analysis_agent import account_names
from src.analysis_agent.analyze_fb_stats import alias_matches


def test_alias_matches_computes_key_once_per_unique_alias(monkeypatch):
    calls = []
    real = account_names.account_key

    def counting(name):
        calls.append(name)
        return real(name)

    monkeypatch.setattr(account_names, "account_key", counting)
    aliases = [f"FacebookAcc{i % 50}" for i in range(20000)]
    series = pd.Series(aliases)
    mask = alias_matches(series, "FacebookAcc7")
    assert int(mask.sum()) == 400
    # 50 уникальных значений + сама цель, а не 20000 вызовов
    assert len(calls) <= 51


def test_alias_matches_ignores_suffix_and_case():
    series = pd.Series(["FacebookX1", "facebookx1", "FacebookX2", None])
    assert alias_matches(series, "FACEBOOKX1 EC").tolist() == [True, True, False, False]
