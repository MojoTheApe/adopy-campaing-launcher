"""Actual separate processes/configs, not shared Python queue instances."""
import json
import os
from pathlib import Path
import subprocess
import sys

from tests.acl2063_machine_scenario import MachinePeople


def test_two_real_processes_union_relay_halt_recovery_and_restart(tmp_path):
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'ipc': str(tmp_path), 'machine': str(tmp_path / 'machine'),
                                    'run': 'portable', 'maps': {'one': ['A', 'B'], 'two': ['B', 'C']}}))
    helper = Path(__file__).with_name('acl2063_machine_person.py')
    processes = [subprocess.Popen([sys.executable, str(helper), str(settings), person, 'person'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, 'ACL_CONFIG_DIR': str(tmp_path / person)})
                 for person in ('one', 'two')]
    scenario = MachinePeople(tmp_path, ['one', 'two'])
    try:
        scenario.exercise()
        scenario.command('one', 'crash')
        out, err = processes[0].communicate(timeout=10)
        assert processes[0].returncode == 77, out + err
        recovered = scenario.command('two', 'state')
        assert recovered['visible']['B']['snapshot']
        assert 'interrupted' in recovered['visible']['B']['attempt']['error']
        assert scenario.command('two', 'drain')['reads'] == 0
        assert scenario.command('two', 'retry')['visible']['B']['attempt'] is None
        scenario.command('two', 'exit')
        out, err = processes[1].communicate(timeout=10)
        assert processes[1].returncode == 0, out + err
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill(); process.communicate(timeout=10)
