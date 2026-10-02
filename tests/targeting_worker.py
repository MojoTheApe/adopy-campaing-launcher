"""Child-process body for `test_multiple_processes_do_not_lose_writes` (ACL-1419).

Lives in its own module on purpose: a `spawn` child imports the module that
holds its target, and `test_targeting_integration` pulls in `src.web.app`
(~3 s on a quiet laptop, far more on a loaded CI runner). Three children doing
that at once was what pushed a worker past the test's join timeout. This
module imports only the registry.
"""
from pathlib import Path

from src.analysis_agent import targeting_registry as tr


def add_eight(path: str, prefix: int) -> None:
    tr._store = lambda: Path(path)
    for i in range(8):
        tr.add(f'{prefix}-{i}', f'Interest {prefix} {i}')
