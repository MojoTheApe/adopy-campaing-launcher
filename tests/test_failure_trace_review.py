"""Real pytest subprocesses protect failure-report persistence boundaries."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'conftest.py'


def run(folder, body, extra=''):
    (folder / 'conftest.py').write_text(SOURCE.read_text(encoding='utf-8') + '\n' + extra, encoding='utf-8')
    (folder / 'test_sample.py').write_text(body, encoding='utf-8')
    return subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_sample.py'],
                          cwd=folder, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)


def reports(folder):
    return sorted((folder / 'output' / 'test-failures').glob('*.md'))


def test_two_runs_in_one_second_keep_both_reports(tmp_path):
    fixed = '''
class FixedClock(_dt.datetime):
    @classmethod
    def now(cls, tz=None): return cls(2026, 9, 13, 12, 0, 0)
_dt.datetime = FixedClock
'''
    assert run(tmp_path, "def test_one():\n    assert False, 'first failure'\n", fixed).returncode == 1
    assert run(tmp_path, "def test_two():\n    assert False, 'second distinct failure'\n", fixed).returncode == 1
    files = reports(tmp_path)
    assert len(files) == 2
    text = '\n'.join(p.read_text(encoding='utf-8') for p in files)
    assert 'first failure' in text and 'second distinct failure' in text


def test_captured_diagnostics_survive_the_terminal(tmp_path):
    result = run(tmp_path, "import sys, logging\ndef test_x():\n    print('stdout diagnostic')\n    print('stderr diagnostic', file=sys.stderr)\n    logging.warning('log diagnostic')\n    assert False\n")
    assert result.returncode == 1
    text = reports(tmp_path)[0].read_text(encoding='utf-8')
    for value in ('Captured stdout', 'stdout diagnostic', 'Captured stderr', 'stderr diagnostic', 'Captured log', 'log diagnostic'):
        assert value in text


def test_teardown_failure_is_saved(tmp_path):
    result = run(tmp_path, "import pytest\n@pytest.fixture\ndef f():\n    yield\n    raise RuntimeError('teardown exploded')\ndef test_x(f):\n    pass\n")
    assert result.returncode == 1
    text = reports(tmp_path)[0].read_text(encoding='utf-8')
    assert 'teardown' in text and 'teardown exploded' in text


def test_collection_failure_is_saved(tmp_path):
    result = run(tmp_path, "raise RuntimeError('collection exploded')\n")
    assert result.returncode == 2
    text = reports(tmp_path)[0].read_text(encoding='utf-8')
    assert 'collect' in text and 'collection exploded' in text


@pytest.mark.parametrize('fails', [True, False])
def test_unwritable_reports_do_not_change_exit_status(tmp_path, fails):
    (tmp_path / 'output').write_text('not a directory')
    result = run(tmp_path, 'def test_x():\n    assert ' + str(not fails) + '\n')
    assert result.returncode == (1 if fails else 0)
    assert not reports(tmp_path)


def test_retention_actually_removes_old_reports(tmp_path):
    target = tmp_path / 'output' / 'test-failures'
    target.mkdir(parents=True)
    for i in range(25):
        (target / f'2020-01-01_00-00-{i:02}.md').write_text('old report')
    result = run(tmp_path, 'def test_x():\n    assert False\n')
    assert result.returncode == 1
    assert len(reports(tmp_path)) == 20
    assert not (target / '2020-01-01_00-00-00.md').exists()
    assert any('test_sample.py::test_x' in p.read_text(encoding='utf-8') for p in reports(tmp_path))


def test_new_session_does_not_repeat_old_failures():
    spec = importlib.util.spec_from_file_location('failure_plugin_for_review', SOURCE)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    plugin.pytest_runtest_logreport(SimpleNamespace(failed=True, when='call', nodeid='old', longreprtext='old failure', sections=[]))
    assert plugin._failures
    plugin.pytest_sessionstart(SimpleNamespace())
    assert plugin._failures == []
