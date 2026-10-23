"""Physical-profile ownership shared by ACM processes of the same OS user.

Only the outer acquisition opens the shared OS lock. Nested calls in its
thread reenter; another thread/process waits. Never unlink a lease file:
replacing its inode would create a second, unrelated lock on POSIX.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import math
from pathlib import Path
import threading
import time
from uuid import UUID

from config.paths import machine_file_lock
from src.fb_ads_rpa import job_cancel


class ProfileLeaseError(RuntimeError):
    """The operation must stop before touching a browser."""


WAIT_SECONDS = 120.0
#: Publish jobs queue behind each other and behind reads for as long as a real
#: publish takes (the old RLock waited without limit). Still cancellable; used
#: only where a caller asks for it explicitly (`publish_lease(..., cancelled=)`).
PUBLISH_WAIT_SECONDS = 1800.0
POLL_SECONDS = 0.05


def physical_profile_key(profile_id: str, *, provider: str = "multilogin") -> str:
    """Normalize a physical identity; labels/endpoints are never lock keys."""
    value = str(profile_id or "").strip()
    provider = str(provider or "").strip().lower()
    if provider == "dolphin":
        if len(value) > 64 or not value.isascii() or not value.isdecimal() or int(value) <= 0:
            raise ProfileLeaseError("Dolphin profile_id must be a positive numeric ID.")
        return "dolphin:" + str(int(value))
    # Same accepted names as browser.get_browser_provider; aliases do not
    # create separate physical identities.
    if provider not in {"multilogin", "multilogin_api", "multilogin_attach", "attach", "attach_only",
                        "multilogin_direct", "direct", "direct_cdp", "playwright_profile", "pw_profile"}:
        raise ProfileLeaseError("Unknown browser provider; profile ownership cannot be established.")
    try:
        parsed = UUID(value)
        if not parsed.int:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ProfileLeaseError("Multilogin profile_id must be a valid, non-empty UUID in the account map.") from None
    return str(parsed)


def _canonical_key(key: str) -> str:
    value = str(key or "").strip()
    if value.startswith("dolphin:"):
        return physical_profile_key(value[8:], provider="dolphin")
    return physical_profile_key(value)


def _lease_root() -> Path:
    # Deliberately independent of ACL_CONFIG_DIR, port, provider endpoint and
    # Multilogin instance alias. This is a same-user, same-machine contract.
    return Path.home() / ".acl-profile-leases"


def _lock_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("ascii")).hexdigest()
    return _lease_root() / (digest + ".lock")


class ProfileLease:
    def __init__(self, key: str) -> None:
        self.key = key
        self._thread_lock = threading.RLock()
        self._owner: int | None = None
        self._depth = 0
        self._file_hold = None

    def held_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident() and self._depth > 0

    def acquire(self, blocking: bool = True, timeout: float = -1, *, cancelled=None) -> bool:
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        if not math.isfinite(timeout) or (timeout < 0 and timeout != -1):
            raise ValueError("timeout must be non-negative")
        limit = WAIT_SECONDS if timeout == -1 else min(float(timeout), max(WAIT_SECONDS, PUBLISH_WAIT_SECONDS))
        deadline = time.monotonic() + (limit if blocking else 0)

        def check_cancel() -> None:
            job_cancel.check()
            if cancelled is not None and cancelled():
                raise job_cancel.JobCancelled("Stopped by the user")

        while True:
            check_cancel()
            if self._thread_lock.acquire(blocking=False):
                keep = False
                try:
                    if self.held_by_current_thread():
                        check_cancel()
                        self._depth += 1
                        keep = True
                        return True
                    hold = machine_file_lock(_lock_path(self.key), timeout=0)
                    try:
                        hold.__enter__()
                    except TimeoutError:
                        pass
                    except OSError:
                        raise ProfileLeaseError("Cannot acquire the profile lease; check access to the user lease directory.") from None
                    else:
                        try:
                            check_cancel()
                        except BaseException:
                            hold.__exit__(None, None, None)
                            raise
                        self._file_hold = hold
                        self._owner = threading.get_ident()
                        self._depth = 1
                        keep = True
                        return True
                finally:
                    if not keep:
                        self._thread_lock.release()
            remaining = deadline - time.monotonic()
            if not blocking or remaining <= 0:
                return False
            job_cancel.heartbeat()
            time.sleep(min(POLL_SECONDS, remaining))

    def release(self) -> None:
        if not self.held_by_current_thread():
            raise RuntimeError("cannot release an un-acquired profile lease")
        try:
            self._depth -= 1
            if not self._depth:
                hold, self._file_hold = self._file_hold, None
                self._owner = None
                hold.__exit__(None, None, None)
        finally:
            self._thread_lock.release()

    @contextmanager
    def holding(self, *, cancelled=None, timeout: float = -1):
        if not self.acquire(cancelled=cancelled, timeout=timeout):
            raise ProfileLeaseError("This profile is busy in another ACM operation; try again after it finishes.")
        try:
            yield self
        finally:
            self.release()

    def __enter__(self):
        if not self.acquire():
            raise ProfileLeaseError("This profile is busy in another ACM operation; try again after it finishes.")
        return self

    def __exit__(self, *_exc):
        self.release()
        return False


_profile_locks: dict[str, ProfileLease] = {}
_profile_guard = threading.Lock()


def profile_lock(key: str) -> ProfileLease:
    name = _canonical_key(key)
    with _profile_guard:
        if name not in _profile_locks:
            _profile_locks[name] = ProfileLease(name)
        return _profile_locks[name]


def require_profile_lease(key: str) -> None:
    """Reject a changed map/provider before a browser side effect."""
    if not profile_lock(key).held_by_current_thread():
        raise ProfileLeaseError("The resolved profile does not match the held profile lease; check the account map and retry.")


def check_nested_profile(key: str) -> None:
    """A nested browser operation cannot silently switch its outer profile."""
    lease = profile_lock(key)
    with _profile_guard:
        if any(lock.held_by_current_thread() for lock in _profile_locks.values()):
            if not lease.held_by_current_thread():
                raise ProfileLeaseError("The resolved profile changed during this operation; check the account map and retry.")
