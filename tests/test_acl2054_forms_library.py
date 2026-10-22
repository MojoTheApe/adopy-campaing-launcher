"""ACL-2054: общая библиотека шаблонов Instant Form (третий вид team_library).

Две «машины» — два каталога конфигурации над одним хранилищем в памяти. То же,
что у таргетов и текстов: правка сразу в репозиторий, sha-проверка с повтором,
«Recently deleted» с Restore, копия для чтения на машине. Главное отличие:
запись, не прошедшая form_templates.validate, не выбрасывается и не стирается —
она остаётся в библиотеке и помечается в списке как invalid.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.analysis_agent import team_library as tl
from src.fb_api import form_templates as ft

TEMPLATE = {
    "name": "Demo form", "headline": "Get more information", "intro": "",
    "contact_fields": ["name", "email"],
    "questions": [{"label": "Budget", "type": "multiple_choice", "options": ["Low", "High"]}],
    "privacy_url": "https://example.com/privacy", "thank_you_text": "Thank you. We will contact you.",
    "website_url": "", "locale": "EN_US",
}
BROKEN = {**TEMPLATE, "id": "aaaaaaaaaaaa", "name": "Broken form", "privacy_url": "http://insecure.example.com"}


def seeded(backend):
    for kind, (path, _) in tl.FILES.items():
        backend.files[path] = (tl._dump(tl.empty(kind)), f"seed-{kind}")
    return backend


@pytest.fixture
def team(tmp_path, monkeypatch):
    backend = seeded(tl.MemoryBackend())
    tl.use_backend(backend)
    monkeypatch.setattr(tl, "_status", {})

    def on(machine: str) -> None:
        d = tmp_path / machine
        d.mkdir(exist_ok=True)
        (d / "selected_cm.txt").write_text(machine, encoding="utf-8")
        monkeypatch.setenv("ACL_CONFIG_DIR", str(d))
        tl._pulled.clear()

    yield backend, on
    tl.use_backend(None)


@pytest.fixture
def local(tmp_path, monkeypatch):
    tl.use_backend(None)
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "local"))
    return tmp_path / "local"


def lib_data(backend) -> dict:
    return json.loads(backend.files["form_templates.json"][0])


def put_raw(backend, templates=None, deleted=None):
    data = {"version": 1, "templates": templates or {}, "deleted": deleted or {}}
    sha = backend.files["form_templates.json"][1]
    backend.put("form_templates.json", tl._dump(data), sha, "seed raw")


# ── общая библиотека ──────────────────────────────────────────────────────

def test_a_template_saved_on_one_machine_reaches_the_other(team):
    backend, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    assert lib_data(backend)["templates"][saved["id"]]["added_by"] == "Anna"
    assert backend.commits and backend.commits[-1].endswith("— Anna")
    on("Boris")
    assert ft.load() == [saved]            # load() подтягивает копию сам
    assert ft.get(saved["id"]) == saved    # публикация читает то же самое


def test_the_author_and_time_are_kept_outside_the_template_content(team):
    _, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    assert "added_by" not in saved and "updated_at" not in saved
    on("Boris")
    other = ft.save({**TEMPLATE, "id": saved["id"], "headline": "Changed"})
    [row] = ft.listing()["rows"]
    assert row["author"] == "Boris" and row["changed_at"]
    assert row["added_by"] == "Anna" and row["updated_by"] == "Boris"
    # имя формы на Странице зависит от содержания, а не от того, кто и когда правил
    assert ft.form_name(other) == ft.form_name(ft.get(saved["id"]))


def test_an_edit_replaces_by_id_and_a_duplicate_name_is_refused(team):
    _, on = team
    on("Anna")
    first = ft.save(TEMPLATE)
    second = ft.save({**TEMPLATE, "name": "Other"})
    with pytest.raises(ft.TemplateError, match="already exists"):
        ft.save({**TEMPLATE, "id": second["id"], "name": "demo FORM"})
    ft.save({**TEMPLATE, "id": first["id"], "headline": "Changed"})
    assert sorted(t["name"] for t in ft.load()) == ["Demo form", "Other"]
    assert ft.get(first["id"])["headline"] == "Changed"


def test_delete_is_soft_and_restore_brings_it_back(team):
    backend, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    on("Boris")
    assert ft.delete(saved["id"]) is True and ft.delete(saved["id"]) is False
    assert ft.load() == []
    [gone] = ft.recently_deleted()
    assert gone["id"] == saved["id"] and gone["name"] == "Demo form"
    assert gone["deleted_by"] == "Boris" and gone["deleted_at"]
    assert lib_data(backend)["deleted"][saved["id"]]["data"]["name"] == "Demo form"
    on("Anna")
    ft.restore(saved["id"])
    assert ft.get(saved["id"]) == saved and ft.recently_deleted() == []
    with pytest.raises(ft.TemplateError, match="not in Recently deleted"):
        ft.restore(saved["id"])


def test_restore_is_refused_while_the_name_is_taken(team):
    _, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    ft.delete(saved["id"])
    ft.save(TEMPLATE)
    with pytest.raises(ft.TemplateError, match="already exists"):
        ft.restore(saved["id"])


def test_an_edit_of_a_template_deleted_elsewhere_does_not_resurrect_it(team):
    _, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    ft.delete(saved["id"])
    with pytest.raises(ft.TemplateError, match="Recently deleted"):
        ft.save({**TEMPLATE, "id": saved["id"], "headline": "Stale edit"})
    assert ft.load() == []


def test_a_sha_conflict_is_retried_on_the_fresh_file(team):
    backend, on = team
    on("Anna")
    other = {**TEMPLATE, "name": "Written meanwhile"}
    real_put = backend.put
    state = {"raced": False}

    def racing_put(path, text, sha, message):
        if not state["raced"]:
            state["raced"] = True
            # Другой менеджер успел записать между нашим чтением и записью.
            data = json.loads(backend.files[path][0])
            clean, _ = ft.validate(other)
            clean["id"] = "bbbbbbbbbbbb"
            data["templates"]["bbbbbbbbbbbb"] = clean
            real_put(path, tl._dump(data), backend.files[path][1], "meanwhile")
        return real_put(path, text, sha, message)

    backend.put = racing_put
    ft.save(TEMPLATE)
    assert sorted(lib_data(backend)["templates"][k]["name"] for k in lib_data(backend)["templates"]) == \
        ["Demo form", "Written meanwhile"]


def test_no_connection_saves_nothing_and_says_so(team):
    backend, on = team
    on("Anna")

    def down(*_a, **_k):
        raise tl.LibraryError("GitHub is unreachable. Try again when the connection is restored.")

    backend.get = down
    with pytest.raises(ft.TemplateError, match="Nothing was saved"):
        ft.save(TEMPLATE)
    assert not (ft.library_path().exists())     # «сохранено только у меня» не бывает


# ── запись, не прошедшая проверку, не теряется ────────────────────────────

def test_an_invalid_library_entry_is_listed_flagged_and_kept(team):
    backend, on = team
    on("Anna")
    put_raw(backend, {BROKEN["id"]: BROKEN})
    rows = ft.listing()["rows"]
    [row] = rows
    assert row["id"] == BROKEN["id"] and row["invalid"] is True
    assert any("https://" in e for e in row["errors"])
    assert ft.load() == [] and ft.get(BROKEN["id"]) is None   # публикация её не берёт


def test_the_next_write_does_not_erase_or_rewrite_an_invalid_entry(team):
    backend, on = team
    on("Anna")
    odd = {**BROKEN, "future_field": {"keep": "me"}}
    put_raw(backend, {BROKEN["id"]: odd})
    saved = ft.save(TEMPLATE)
    ft.delete(saved["id"])
    ft.restore(saved["id"])
    ft.save({**TEMPLATE, "id": saved["id"], "headline": "Again"})
    assert lib_data(backend)["templates"][BROKEN["id"]] == odd
    [good] = ft.load()
    assert good["id"] == saved["id"]
    assert {r["id"]: r["invalid"] for r in ft.listing()["rows"]} == {BROKEN["id"]: True, saved["id"]: False}


def test_an_invalid_entry_can_be_fixed_deleted_and_restored(team):
    backend, on = team
    on("Anna")
    put_raw(backend, {BROKEN["id"]: BROKEN})
    ft.delete(BROKEN["id"])
    assert BROKEN["id"] not in lib_data(backend)["templates"]
    ft.restore(BROKEN["id"])
    restored = lib_data(backend)["templates"][BROKEN["id"]]
    assert {k: v for k, v in restored.items() if not k.startswith("updated_")} == BROKEN
    with pytest.raises(ft.TemplateError, match="https://"):
        ft.save({**BROKEN})                               # исправление проверяется как обычно
    fixed = ft.save({**BROKEN, "privacy_url": "https://example.com/privacy"})
    assert fixed["id"] == BROKEN["id"] and ft.get(BROKEN["id"]) == fixed
    assert ft.listing()["rows"][0]["invalid"] is False


def test_a_rubbish_record_does_not_make_the_whole_file_damaged(team):
    backend, on = team
    on("Anna")
    put_raw(backend, {"zzzz": {"name": 7}, "cccccccccccc": BROKEN})
    ft.save(TEMPLATE)
    raw = lib_data(backend)["templates"]
    assert raw["zzzz"] == {"name": 7} and raw["cccccccccccc"] == BROKEN
    assert len([r for r in ft.listing()["rows"] if r["invalid"]]) == 2


# ── без библиотеки: файл на машине, как раньше ────────────────────────────

def test_without_a_library_everything_works_on_the_local_file(local):
    saved = ft.save(TEMPLATE)
    assert ft.library_path().parent == local and ft.load() == [saved]
    assert ft.listing()["library"]["connected"] is False
    assert ft.delete(saved["id"]) is True and ft.load() == []
    [gone] = ft.recently_deleted()
    assert gone["id"] == saved["id"]
    ft.restore(saved["id"])
    assert ft.get(saved["id"]) == saved


def test_a_local_invalid_row_survives_a_local_write(local):
    ft.library_path().parent.mkdir(parents=True, exist_ok=True)
    ft.library_path().write_text(json.dumps({"templates": [BROKEN, {"name": "no id at all"}]}), encoding="utf-8")
    ft.save(TEMPLATE)
    rows = json.loads(ft.library_path().read_text(encoding="utf-8"))["templates"]
    assert BROKEN in rows and {"name": "no id at all"} in rows and len(rows) == 3
    assert sum(1 for r in ft.listing()["rows"] if r["invalid"]) == 2


def test_duplicate_makes_a_copy_with_a_free_name(team):
    _, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    one = ft.duplicate(saved["id"])
    two = ft.duplicate(saved["id"])
    assert one["name"] == "Demo form (copy)" and two["name"] == "Demo form (copy 2)"
    assert len({saved["id"], one["id"], two["id"]}) == 3
    assert one["headline"] == saved["headline"]
    long = ft.save({**TEMPLATE, "name": "x" * 60})
    assert len(ft.duplicate(long["id"])["name"]) <= ft.MAX_NAME
    with pytest.raises(ft.TemplateError, match="not found"):
        ft.duplicate("ffffffffffff")


# ── перенос локальных шаблонов ────────────────────────────────────────────

def _local_file(tmp_path, rows):
    # Локальный файл машины лежит рядом с копией библиотеки; пишем его напрямую.
    p = ft.library_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"templates": rows}), encoding="utf-8")
    return p


def test_local_templates_are_offered_and_uploaded_once_without_duplicates(team, tmp_path):
    backend, on = team
    on("Anna")
    same = ft.save(TEMPLATE)                                     # уже в библиотеке
    conflict = ft.validate({**TEMPLATE, "id": "111111111111", "headline": "Different content"})[0]
    new_a = ft.validate({**TEMPLATE, "id": "222222222222", "name": "Local A"})[0]
    new_b = ft.validate({**TEMPLATE, "id": same["id"], "name": "Local B"})[0]   # занятый id
    twin = ft.validate({**TEMPLATE, "id": "333333333333"})[0]    # то же имя и содержание
    path = _local_file(tmp_path, [conflict, new_a, new_b, twin, BROKEN])
    before = path.read_text(encoding="utf-8")

    st = ft.migration_status()
    assert st["available"] is True and st["uploadable"] == 2
    reasons = {}
    for s in st["skipped"]:
        reasons.setdefault(s["name"], []).append(s["reason"])
    assert sorted(reasons["Demo form"]) == ["A template with this name and different content is already in the team library",
                                            "Already in the team library"]
    assert "invalid" in reasons["Broken form"][0].lower()

    result = ft.migrate_local()
    assert result["uploaded"] == 2 and len(result["skipped"]) == len(st["skipped"])
    assert sorted(t["name"] for t in ft.load()) == ["Demo form", "Local A", "Local B"]
    ids = [r["id"] for r in ft.listing()["rows"]]
    assert len(ids) == len(set(ids)) == 3                       # занятый id получил новый
    assert path.read_text(encoding="utf-8") == before            # локальный файл не тронут
    assert ft.migration_status()["uploadable"] == 0              # повтор ничего не грузит
    assert ft.migrate_local()["uploaded"] == 0
    [local_b] = [t for t in ft.load() if t["name"] == "Local B"]
    assert local_b["id"] != same["id"] and local_b["headline"] == TEMPLATE["headline"]
    assert lib_data(backend)["templates"][local_b["id"]]["added_by"] == "Anna"


def test_a_name_taken_with_different_content_is_skipped_and_named(team, tmp_path):
    _, on = team
    on("Anna")
    ft.save(TEMPLATE)
    other = ft.validate({**TEMPLATE, "id": "444444444444", "headline": "Different content"})[0]
    _local_file(tmp_path, [other])
    [skip] = ft.migration_status()["skipped"]
    assert skip["name"] == "Demo form" and "different content" in skip["reason"]
    assert ft.migrate_local()["uploaded"] == 0


def test_migration_is_not_offered_without_a_library(local):
    _local_file(local, [ft.validate({**TEMPLATE, "id": "555555555555"})[0]])
    assert ft.migration_status() == {"available": False, "uploadable": 0, "skipped": []}
    with pytest.raises(ft.TemplateError, match="not connected"):
        ft.migrate_local()


def test_the_local_file_is_never_the_source_of_publishing_once_the_library_is_on(team, tmp_path):
    _, on = team
    on("Anna")
    local_tpl = ft.validate({**TEMPLATE, "id": "666666666666"})[0]
    _local_file(tmp_path, [local_tpl])
    assert ft.get("666666666666") is None and ft.load() == []


# ── эндпоинты ─────────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body=None):
        self._b = body

    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


def test_endpoints_cover_list_save_duplicate_delete_restore_and_migrate(team, tmp_path):
    from src.web import app as web
    _, on = team
    on("Anna")
    saved = json.loads(asyncio.run(web.api_form_templates_save(_Req(TEMPLATE))).body)["template"]
    listed = json.loads(web.api_form_templates_list(force=True).body)
    assert [t["id"] for t in listed["templates"]] == [saved["id"]]          # форма ответа билдера прежняя
    assert listed["rows"][0]["summary"] == "Name, Email · 1 question"
    assert listed["library"]["connected"] is True and listed["migration"]["uploadable"] == 0
    dup = json.loads(asyncio.run(web.api_form_templates_duplicate(_Req({"id": saved["id"]}))).body)
    assert dup["ok"] and dup["template"]["name"] == "Demo form (copy)"
    assert json.loads(asyncio.run(web.api_form_templates_delete(_Req({"id": saved["id"]}))).body) == {"ok": True}
    listed = json.loads(web.api_form_templates_list().body)
    assert [d["id"] for d in listed["deleted"]] == [saved["id"]]
    assert json.loads(asyncio.run(web.api_form_templates_restore(_Req({"id": saved["id"]}))).body)["ok"]
    assert asyncio.run(web.api_form_templates_restore(_Req({"id": saved["id"]}))).status_code == 400
    _local_file(tmp_path, [ft.validate({**TEMPLATE, "id": "777777777777", "name": "Local"})[0]])
    out = json.loads(asyncio.run(web.api_form_templates_migrate(_Req({}))).body)
    assert out["ok"] and out["uploaded"] == 1


def test_an_unreachable_library_is_a_503_with_the_reason(team):
    from src.web import app as web
    backend, on = team
    on("Anna")

    def down(*_a, **_k):
        raise tl.LibraryError("GitHub is unreachable. Try again when the connection is restored.")

    backend.get = down
    resp = asyncio.run(web.api_form_templates_save(_Req(TEMPLATE)))
    assert resp.status_code == 503 and "Nothing was saved" in json.loads(resp.body)["errors"][0]


# ── ревью-правки ──────────────────────────────────────────────────────────

@pytest.fixture
def unseeded(tmp_path, monkeypatch):
    """Репозиторий данных с targets/ad_texts, но без form_templates.json (как в проде)."""
    backend = tl.MemoryBackend()
    for kind in ("targets", "ad_texts"):
        backend.files[tl.FILES[kind][0]] = (tl._dump(tl.empty(kind)), f"seed-{kind}")
    tl.use_backend(backend)
    monkeypatch.setattr(tl, "_status", {})
    d = tmp_path / "Anna"
    d.mkdir()
    (d / "selected_cm.txt").write_text("Anna", encoding="utf-8")
    monkeypatch.setenv("ACL_CONFIG_DIR", str(d))
    tl._pulled.clear()
    yield backend
    tl.use_backend(None)


def test_a_library_without_the_forms_file_reads_as_empty_and_the_first_write_creates_it(unseeded, tmp_path):
    assert ft.load() == [] and ft.get("aaaaaaaaaaaa") is None
    view = ft.listing(force=True)
    assert view["rows"] == [] and view["library"]["ok"] is True
    assert "form_templates.json" not in unseeded.files
    saved = ft.save(TEMPLATE)
    assert saved["id"] in lib_data(unseeded)["templates"]
    assert ft.get(saved["id"]) == saved


def test_migration_works_against_a_library_without_the_forms_file(unseeded, tmp_path):
    _local_file(tmp_path, [ft.validate({**TEMPLATE, "id": "111111111111"})[0]])
    assert ft.migration_status()["uploadable"] == 1
    assert ft.migrate_local()["uploaded"] == 1
    assert [t["name"] for t in ft.load()] == ["Demo form"]


def test_the_seed_script_knows_the_forms_kind():
    import importlib.util
    spec = importlib.util.spec_from_file_location("seed_tl", "scripts/seed_team_library.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "form_templates" in mod.KINDS


def test_migration_does_not_resurrect_a_template_deleted_in_the_library(team, tmp_path):
    _, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    ft.delete(saved["id"])
    _local_file(tmp_path, [ft.validate({**TEMPLATE, "id": "888888888888"})[0]])
    st = ft.migration_status()
    assert st["uploadable"] == 0
    assert "eleted in the team library" in st["skipped"][0]["reason"]
    assert ft.migrate_local()["uploaded"] == 0 and ft.load() == []


def test_a_missing_template_error_points_at_the_upload(team, tmp_path):
    from src.fb_api import creator
    _, on = team
    on("Anna")
    src = open(creator.__file__, encoding="utf-8").read()
    assert "If it was saved on this computer before the library was connected, upload it from the Forms tab" in src


def test_mixed_case_ids_still_load_and_stay_editable(team):
    backend, on = team
    on("Anna")
    rec = {**TEMPLATE, "id": "AbCdEf012345"}
    put_raw(backend, {"AbCdEf012345": rec})
    [row] = ft.listing()["rows"]
    assert row["invalid"] is False and row["id"] == "abcdef012345"
    assert ft.get("AbCdEf012345")["name"] == "Demo form"
    ft.save({**TEMPLATE, "id": "AbCdEf012345", "headline": "Edited"})
    assert [t["headline"] for t in ft.load()] == ["Edited"]
    assert len(lib_data(backend)["templates"]) == 1
    assert ft.delete("AbCdEf012345") is True


def test_a_key_both_active_and_deleted_does_not_block_the_team(team):
    backend, on = team
    on("Anna")
    put_raw(backend, {BROKEN["id"]: {**BROKEN, "privacy_url": "https://example.com/p"}},
            {BROKEN["id"]: {"data": BROKEN, "deleted_by": "x", "deleted_at": "t"}})
    assert [t["id"] for t in ft.load()] == [BROKEN["id"]]
    ft.save(TEMPLATE)
    assert len(ft.load()) == 2


def test_editing_keeps_fields_the_editor_does_not_know(team):
    backend, on = team
    on("Anna")
    saved = ft.save(TEMPLATE)
    data = lib_data(backend)
    data["templates"][saved["id"]]["future_field"] = {"keep": 1}
    put_raw(backend, data["templates"])
    ft.save({**TEMPLATE, "id": saved["id"], "headline": "Edited"})
    assert lib_data(backend)["templates"][saved["id"]]["future_field"] == {"keep": 1}


def test_the_list_endpoint_turns_a_local_read_failure_into_a_503(team, monkeypatch):
    from src.web import app as web
    monkeypatch.setattr(ft, "listing", lambda force=False: (_ for _ in ()).throw(OSError("locked")))
    resp = web.api_form_templates_list()
    assert resp.status_code == 503 and "could not be read" in json.loads(resp.body)["errors"][0]


def test_local_rows_without_an_id_count_for_the_duplicate_name_check(local):
    ft.library_path().parent.mkdir(parents=True, exist_ok=True)
    ft.library_path().write_text(json.dumps({"templates": [{"name": "Demo form"}]}), encoding="utf-8")
    with pytest.raises(ft.TemplateError, match="already exists"):
        ft.save(TEMPLATE)


def test_a_library_entry_with_a_legacy_locale_is_usable_and_stays_editable(team):
    # ACL-2053 + ACL-2054: язык вне закрытого списка у сохранённой записи не делает её битой.
    backend, on = team
    on("Anna")
    rec = {**TEMPLATE, "id": "bbbbbbbbbbbb", "locale": "ES_MX"}
    put_raw(backend, {"bbbbbbbbbbbb": rec})
    [row] = ft.listing()["rows"]
    assert row["invalid"] is False and row["locale"] == "ES_MX"
    assert ft.get("bbbbbbbbbbbb")["locale"] == "ES_MX"
    ft.save({**rec, "headline": "Edited"})
    assert ft.get("bbbbbbbbbbbb")["headline"] == "Edited"
    with pytest.raises(ft.TemplateError):
        ft.save({**TEMPLATE, "name": "New one", "locale": "ES_MX"})
