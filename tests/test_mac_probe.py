"""Зонд Mimic: отчёт безопасно вставлять в чат (ни путей дома, ни значений флагов)."""
import importlib.util
import json
from pathlib import Path

import psutil
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "acl_mac_probe", Path(__file__).resolve().parents[1] / "scripts" / "acl_mac_probe.py"
)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def test_scrub_args_hides_values_keeps_flag_names():
    out = probe.scrub_args(
        ["--client-session-id=abc", "--user-data-dir=/Users/me/x", "--headless", "/Users/me/x"]
    )
    assert out == [
        "--client-session-id=<hidden>",
        "--user-data-dir=<hidden>",
        "--headless",
        "<arg>",
    ]


def test_scrub_args_two_token_form_hides_value():
    assert probe.scrub_args(["--proxy-server", "1.2.3.4:80"]) == ["--proxy-server", "<arg>"]


def test_scrub_args_empty_and_none():
    assert probe.scrub_args([]) == []
    assert probe.scrub_args(None) == []


def test_scrub_path_under_home():
    assert probe.scrub_path("/Users/me/mlx/a", home="/Users/me") == "~/mlx/a"


def test_scrub_path_equal_to_home():
    assert probe.scrub_path("/Users/me", home="/Users/me") == "~"
    assert probe.scrub_path("/Users/me/", home="/Users/me") == "~"


def test_scrub_path_sibling_prefix_not_home():
    out = probe.scrub_path("/Users/me2/mlx", home="/Users/me")
    assert out == "/Users/<user>/mlx"  # не «~2/mlx»: чужой каталог, имя скрыто


def test_scrub_path_none_and_outside():
    assert probe.scrub_path(None, home="/Users/me") is None
    assert probe.scrub_path("/Applications/X.app/Contents/MacOS/x", home="/Users/me") == (
        "/Applications/X.app/Contents/MacOS/x"
    )


def test_scrub_path_foreign_user_name_hidden():
    # имя пользователя не должно утекать, даже если путь не под нашим home
    assert "someone" not in probe.scrub_path("/Users/someone/mlx", home="/Users/me")


def test_mlx_homes_env_goes_through_scrub(tmp_path):
    (tmp_path / "mlx").mkdir()
    env_dir = tmp_path / "custom"
    env_dir.mkdir()
    rep = probe.collect_mlx_homes(str(tmp_path), {"MLX_HOME": str(env_dir), "SECRET": "x"})
    assert rep["found"] == ["~/mlx"]
    assert rep["MLX_HOME"] == "~/custom"
    assert "SECRET" not in json.dumps(rep)


def test_mlx_homes_no_multilogin(tmp_path):
    rep = probe.collect_mlx_homes(str(tmp_path), {})
    assert rep == {"found": [], "MLX_HOME": None}


def test_deps_two_levels_and_executables(tmp_path):
    home = tmp_path / "mlx"
    exe_dir = home / "deps" / "mimic" / "125.0" / "Mimic.app" / "Contents" / "MacOS"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Mimic").write_text("x")
    # профиль рядом с deps: в отчёт попадать не должен
    (home / "profiles" / "SECRET-PROFILE-ID").mkdir(parents=True)
    rep = probe.collect_deps(str(home), str(tmp_path))
    assert rep["names"] == ["mimic", "mimic/125.0"]
    assert rep["executables"] == ["~/mlx/deps/mimic/125.0/Mimic.app/Contents/MacOS/Mimic"]
    assert "SECRET-PROFILE-ID" not in json.dumps(rep)


def test_deps_missing(tmp_path):
    assert probe.collect_deps(str(tmp_path / "nope"), str(tmp_path)) == {
        "names": [],
        "executables": [],
    }


class _FakeProc:
    def __init__(self, name, exe, cmd, exc=None):
        self._exc = exc
        self.info = None
        self._vals = {"name": name, "exe": exe, "cmdline": cmd}

    def as_dict(self, attrs=None):
        if self._exc:
            raise self._exc
        return dict(self._vals)


def test_processes_filter_scrub_and_skip_errors():
    procs = [
        _FakeProc("Mimic", "/Users/me/mlx/deps/Mimic.app/Contents/MacOS/Mimic",
                  ["Mimic", "--user-data-dir=/Users/me/p", "--x", "val"]),
        _FakeProc("bash", "/bin/bash", ["bash"]),
        _FakeProc("chrome", None, None),
        _FakeProc("mimic", "/x", [], exc=psutil.AccessDenied()),
        _FakeProc("mimic", "/x", [], exc=psutil.NoSuchProcess(1)),
    ]
    rep = probe.collect_processes(procs, home="/Users/me")
    assert [r["name"] for r in rep] == ["Mimic", "chrome"]
    assert rep[0]["exe"] == "~/mlx/deps/Mimic.app/Contents/MacOS/Mimic"
    assert rep[0]["args"] == ["<arg>", "--user-data-dir=<hidden>", "<arg>", "<arg>"]
    assert rep[1]["exe"] is None and rep[1]["args"] == []
    assert "/Users/me" not in json.dumps(rep)


def test_main_prints_json_and_returns_zero(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # linux-ok: expanduser на Windows-раннере читает USERPROFILE
    monkeypatch.delenv("MLX_HOME", raising=False)
    assert probe.main() == 0
    rep = json.loads(capsys.readouterr().out)
    assert set(rep) == {"mlx_homes", "deps", "processes"}
    assert rep["mlx_homes"]["found"] == []
    assert rep["deps"] == []
    assert isinstance(rep["processes"], list)


_UUID = "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.parametrize(
    "arg",
    [
        "-javaagent:/Users/me/mlx/a.jar",
        "-Xbootclasspath/a:/Users/me/x",
        "--token:abc",
        "-1.2.3.4:80",
        "--bad/name=1",
        "-pSECRETTOKEN",
        "-usuper-secret-token",
        "-Uzorbaq",
        "--user-zorbaq-pass-hunter2",
        "--socks5-127-0-0-1-1080",
        "-p123e4567e89b12d3a456426614174000",
        "-p" + _UUID,
        "--unknown-flag=secret",
    ],
)
def test_scrub_args_unknown_tokens_are_masked(arg):
    assert probe.scrub_args([arg]) == ["<arg>"]


@pytest.mark.parametrize("flag", sorted(probe.KNOWN_FLAGS))
def test_scrub_args_known_flags_survive(flag):
    assert probe.scrub_args([flag]) == [flag]
    assert probe.scrub_args([flag + "=v"]) == [flag + "=<hidden>"]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/System/Volumes/Data/Users/zorbaq/x", "/System/Volumes/Data/Users/<user>/x"),
        ("/private/Users/zorbaq/x", "/private/Users/<user>/x"),
        ("/Volumes/SecretDisk/mlx", "/Volumes/<volume>/mlx"),
        ("/Users//other/x", "/Users/<user>/x"),
        ("/volumes/Secret/x", "/Volumes/<volume>/x"),
        ("/USERS/Other/x", "/USERS/<user>/x"),
    ],
)
def test_scrub_path_unanchored_users_and_volumes(path, expected):
    assert probe.scrub_path(path, home="/Users/me") == expected


def test_report_has_no_sentinels_anywhere(tmp_path):
    home = tmp_path / "zorbaq"
    deps = home / "mlx" / "deps"
    (deps / ("zorbaq-" + _UUID) / "1.0" / "Mimic.app" / "Contents" / "MacOS").mkdir(parents=True)
    (deps / ("zorbaq-" + _UUID) / "1.0" / "Mimic.app" / "Contents" / "MacOS" / "Mimic").write_text("x")
    procs = [
        _FakeProc(
            "zorbaq-" + _UUID,
            "/System/Volumes/Data/Users/zorbaq/mlx/profiles/" + _UUID + "/Mimic",
            ["Mimic", "-javaagent:/Users/zorbaq/a.jar", "-p" + _UUID, "--user-data-dir=/Volumes/SecretDisk/x"],
        )
    ]
    env = {"MLX_HOME": "/Volumes/SecretDisk/" + _UUID}
    rep = probe.build_report(str(home), env, procs)
    blob = json.dumps(rep)
    for sentinel in ("zorbaq", _UUID, "SecretDisk"):
        assert sentinel not in blob, sentinel
    # раскладка deps подписана корнем (нужна Задаче 4)
    assert rep["deps"][0]["root"] == "~/mlx"
    assert rep["deps"][0]["executables"]


def test_scrub_path_under_home_still_hides_volumes_in_rest():
    assert probe.scrub_path("/Users/me/Volumes/Secret/x", home="/Users/me") == "~/Volumes/<volume>/x"


def test_user_name_pass_is_case_insensitive_and_glued(tmp_path):
    home = tmp_path / "zorbaq"
    home.mkdir()
    procs = [_FakeProc(n, "/opt/xzorbaq/mlx/ZORBAQ-backup/Mimic", [])
             for n in ("zorbaq2-chrome", "ZORBAQ-backup", "Zorbaq-chrome")]
    blob = json.dumps(probe.build_report(str(home), {}, procs), ensure_ascii=False).lower()
    assert "zorbaq" not in blob


def test_short_or_layout_user_name_is_not_redacted(tmp_path):
    for name in ("bob", "mlx", "chrome"):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        rep = probe.build_report(str(home), {}, [_FakeProc("chrome", None, [])])
        assert rep["processes"][0]["name"] == "chrome"
