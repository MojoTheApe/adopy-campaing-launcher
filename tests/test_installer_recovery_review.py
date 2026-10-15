"""Exercise cmd's recovery block, including replacement of its running file."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(reason="runs install_acl.bat in the Windows cmd interpreter")


@pytest.mark.skipif(os.name != 'nt', reason='executes the Windows cmd interpreter')
@pytest.mark.parametrize('outcome', [0, 1, 10])
def test_recovery_does_not_reenter_or_continue_replaced_batch(tmp_path, outcome):
    source = (Path(__file__).resolve().parents[1] / 'install_acl.bat').read_text()
    # Run the real recovery commands without installing packages or opening ACL.
    start = source.index('REM Recovery may replace')
    block = source[start:source.index(':dependencies', start)]
    installer = tmp_path / 'install_acl.bat'
    installer.write_text('@echo off\nset "PY=' + sys.executable + '"\n' + block
                         + '\necho dependencies> continued.txt\nexit /b 0\n')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / 'acl_runtime_manager.py').write_text(
        'from pathlib import Path\nimport sys\n'
        + ('Path("install_acl.bat").write_text("@echo off\\necho reentered> reentered.txt\\nexit /b 0\\n")\n'
           if outcome == 10 else '')
        + f'sys.exit({outcome})\n'
    )
    result = subprocess.run(['cmd.exe', '/d', '/c', str(installer)], cwd=tmp_path,
                            input='\n', text=True, capture_output=True, timeout=20)
    assert (tmp_path / 'continued.txt').exists() is (outcome == 0)
    assert not (tmp_path / 'reentered.txt').exists()
    assert result.returncode == outcome, result.stdout + result.stderr
    if outcome == 10:
        assert 'Run install_acl.bat again' in result.stdout
