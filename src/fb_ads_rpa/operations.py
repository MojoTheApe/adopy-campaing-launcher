"""Serialize browser operations in the ACL server, including read-only visits.

Reentrant because the job runner and the publisher both enforce the boundary.
Separate ACL processes must not operate the same live Multilogin profile.
"""
from threading import RLock
from functools import wraps

browser_operation = RLock()


def serialized_browser(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with browser_operation:
            return fn(*args, **kwargs)
    return wrapped
