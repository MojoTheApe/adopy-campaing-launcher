from src.acl_event_log.clean import clean_text, clean_value, link_fingerprint, plan_hash


def test_ids_survive():
    out = clean_value({"campaign_id": "120212345678901234", "act": "act_1234567890", "n": 120212345678901234})
    assert out == {"campaign_id": "120212345678901234", "act": "act_1234567890", "n": 120212345678901234}


def test_secret_keys_hidden_any_case_and_nested():
    out = clean_value({"Proxy_Pass": "x", "a": [{"Authorization": "y"}], "cookies": ["z"]})
    assert out == {"Proxy_Pass": "<hidden>", "a": [{"Authorization": "<hidden>"}], "cookies": "<hidden>"}


def test_token_removed_in_text():
    assert "EAAB" not in clean_value({"m": "bad EAAB" + "a" * 30})["m"]


def test_url_values_and_emails_cut_ids_kept():
    text = "fail 120212345678901234 https://t.example/c?sub=partner7&x=1 for a.b@c.io"
    out = clean_text(text)
    assert "120212345678901234" in out
    assert "partner7" not in out and "a.b@c.io" not in out
    assert "sub=<v>" in out and "<email>" in out


def test_link_fingerprint():
    fp = link_fingerprint("https://t.example/go/lp?sub=partner7&x=1")
    assert fp["domain"] == "t.example" and fp["path"] == "/go/lp"
    assert fp["param_names"] == ["sub", "x"]
    assert len(fp["param_hashes"]["sub"]) == 12
    assert "partner7" not in str(fp)


def test_link_fingerprint_bad_input_is_empty_not_error():
    assert link_fingerprint("") == {"domain": "", "path": "", "param_names": [], "param_hashes": {}}
    assert link_fingerprint(None)["domain"] == ""


def test_plan_hash_ignores_key_order():
    assert plan_hash("IT", [{"a": 1, "b": 2}]) == plan_hash("IT", [{"b": 2, "a": 1}])
