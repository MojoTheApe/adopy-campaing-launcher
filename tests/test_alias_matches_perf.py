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


def test_rules_file_is_not_stat_ed_on_every_call(monkeypatch, tmp_path):
    """ACL-1729: сотни тысяч вызовов account_key не должны давать сотни тысяч stat."""
    cfg = tmp_path / "rules.json"
    cfg.write_text('{"rules": []}', encoding="utf-8")
    monkeypatch.setattr(account_names, "_RULES_PATH", cfg)
    account_names._CACHE["stamp"] = None
    stats = []
    real_stat = type(cfg).stat

    def counting_stat(self, *a, **kw):
        stats.append(1)
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(type(cfg), "stat", counting_stat)
    try:
        for _ in range(5000):
            account_names.account_key("FacebookAcc1")
        assert len(stats) <= 2
    finally:
        account_names._CACHE["stamp"] = None


def test_edited_rules_file_is_picked_up_after_the_throttle_window(monkeypatch, tmp_path):
    """Горячая перезагрузка ACL-1653 сохраняется: правка видна после окна."""
    cfg = tmp_path / "rules.json"
    cfg.write_text('{"rules": []}', encoding="utf-8")
    monkeypatch.setattr(account_names, "_RULES_PATH", cfg)
    account_names._CACHE["stamp"] = None
    try:
        assert account_names.account_key("Acme_X1") == "acme_x1"
        cfg.write_text('{"rules": [{"prefixes": ["Acme_", "FacebookACME"]}]}', encoding="utf-8")
        # Окно ещё не истекло — прежние правила.
        assert account_names.account_key("Acme_X1") == "acme_x1"
        # Окно истекло — правка подхвачена.
        account_names._CACHE["checked"] -= account_names._RESTAT_EVERY_S + 1
        assert account_names.account_key("Acme_X1") == account_names.account_key("FacebookACMEX1")
        assert account_names.account_key("Acme_X1").startswith("rule0:")
    finally:
        account_names._CACHE["stamp"] = None


def test_workbook_alias_index_is_built_once_per_country(monkeypatch):
    from src.web import app as web_app

    df = pd.DataFrame({
        "Country Final": ["Kuwait"] * 3 + ["Peru"],
        "Account Alias": ["FacebookA1", "FacebookA2", "FacebookA1", "FacebookB1"],
    })
    web_app._ALIAS_INDEX["df"] = None
    for label in ("FacebookA1", "FacebookA2", "FacebookZ9", "FacebookA1"):
        web_app.workbook_alias_for(df, "Kuwait", label)
        web_app._workbook_has_account(df, "Kuwait", label)
    assert set(web_app._ALIAS_INDEX["by_country"]) == {"Kuwait"}
    assert web_app._workbook_has_account(df, "Kuwait", "FacebookA2") is True
    assert web_app._workbook_has_account(df, "Kuwait", "FacebookB1") is False
    assert web_app._workbook_has_account(df, "Nowhere", "FacebookA1") is False
    assert web_app.workbook_alias_for(df, "Nowhere", "FacebookA1") == "FacebookA1"
