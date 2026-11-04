"""ACL-2243: a failed read of the confirmed-targeting store is not an empty store."""
import pytest

from src.fb_api import targeting_resolver as tr


def test_missing_file_is_an_empty_store(tmp_path):
    store = tr.ConfirmedStore(tmp_path / "confirmed.json")
    assert store.entries() == []
    assert store.get("yoga") is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"entries": 5}'])
def test_damaged_file_is_a_read_failure(tmp_path, content):
    path = tmp_path / "confirmed.json"
    path.write_text(content, encoding="utf-8")
    store = tr.ConfirmedStore(path)
    with pytest.raises(tr.ConfirmedStoreUnreadable) as err:
        store.entries()
    assert "could not be read" in str(err.value)
    with pytest.raises(tr.ConfirmedStoreUnreadable):
        store.get_all("yoga")


def test_unreadable_file_is_not_overwritten_by_a_save(tmp_path):
    path = tmp_path / "confirmed.json"
    path.write_text("{broken", encoding="utf-8")
    store = tr.ConfirmedStore(path)
    with pytest.raises(tr.ConfirmedStoreUnreadable):
        store.put("yoga", id="123", type="interests", name="Yoga")
    with pytest.raises(tr.ConfirmedStoreUnreadable):
        store.remove("yoga")
    with pytest.raises(tr.ConfirmedStoreUnreadable):
        store.clear()
    assert path.read_text(encoding="utf-8") == "{broken"


def test_unreadable_path_is_a_read_failure(tmp_path):
    path = tmp_path / "confirmed.json"
    path.mkdir()     # reading a directory fails with an OS error, not "missing"
    with pytest.raises(tr.ConfirmedStoreUnreadable):
        tr.ConfirmedStore(path).entries()


def test_resolution_stops_when_store_state_is_unknown(tmp_path):
    path = tmp_path / "confirmed.json"
    path.write_text("{broken", encoding="utf-8")

    class NoCalls:
        def get_all(self, *a, **k):
            raise AssertionError("must not guess")

    resolver = tr.TermResolver(NoCalls(), tr.ConfirmedStore(path))
    with pytest.raises(tr.ConfirmedStoreUnreadable):
        resolver.prefetch(["yoga"])
    with pytest.raises(tr.ConfirmedStoreUnreadable):
        resolver.resolve("yoga")
