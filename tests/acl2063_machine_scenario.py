"""Shared assertions for real processes and real Windows standard users."""
from pathlib import Path
import time

from tests.acl2063_machine_person import write
from tests.synthetic_json_ipc import read_json


class MachinePeople:
    def __init__(self, ipc, people, diagnostic=None):
        self.ipc, self.people = Path(ipc), people
        self.sequence = 0
        self.diagnostic = diagnostic or (lambda person: {})

    def read(self, person, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            path = self.ipc / (person + '.json')
            if path.exists():
                value = read_json(path)
                assert value.get('phase') != 'error', value
                if predicate(value):
                    return value
            time.sleep(.02)
        raise AssertionError(('controlled person did not reach required state', person, self.diagnostic(person)))

    def send(self, person, action):
        self.sequence += 1
        write(self.ipc / (person + '.command.json'), {'sequence': self.sequence, 'action': action})
        return self.sequence

    def result(self, person, sequence):
        return self.read(person, lambda value: value.get('sequence') == sequence)

    def command(self, person, action):
        return self.result(person, self.send(person, action))

    def exercise(self):
        first, second = self.people
        initial = [self.read(p, lambda v: v['phase'] == 'prepared') for p in self.people]
        assert initial[0]['config'] != initial[1]['config']
        assert initial[0]['machine'] == initial[1]['machine']
        admits = [self.command(p, 'schedule')['admitted'] for p in self.people]
        assert [v['sweep'] for v in admits] == ['started', 'joined']
        sequences = [self.send(p, 'drain') for p in self.people]
        drained = [self.result(p, seq) for p, seq in zip(self.people, sequences)]
        actual_reads = sum(v['reads'] for v in drained)
        # Like the real worker's bounded polling, retry only still-queued work
        # after the other process releases the nonblocking OS lease.
        deadline = time.monotonic() + 10
        while any(v['state']['queued'] for v in drained) and time.monotonic() < deadline:
            sequences = [self.send(p, 'drain') for p in self.people]
            drained = [self.result(p, seq) for p, seq in zip(self.people, sequences)]
            actual_reads += sum(v['reads'] for v in drained)
        assert not any(v['state']['queued'] for v in drained), drained
        assert actual_reads == 3
        trace = read_json(self.ipc / 'trace.json')
        assert trace['max'] == 1 and trace['active'] == 0
        assert sorted(row[1] for row in trace['reads']) == ['A', 'B', 'C']
        for p in self.people:
            state = self.command(p, 'state')
            assert all(v['snapshot'] for v in state['visible'].values())
            assert state['state']['sweep']['done'] == state['state']['sweep']['total'] == 2
            assert state['private_unchanged']
        assert self.command(first, 'checkpoint')['state']['halt']['kind'] == 'checkpoint'
        assert self.command(second, 'state')['state']['halt']['kind'] == 'checkpoint'
        assert self.command(second, 'restart')['state']['halt']['kind'] == 'checkpoint'
        assert self.command(second, 'drain')['reads'] == 0
        assert self.command(second, 'retry')['state']['halt'] is None
        assert self.command(first, 'state')['state']['halt'] is None
        assert all(self.command(p, 'schedule')['admitted']['queued'] == 0 for p in self.people)
        return initial
