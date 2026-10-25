"""Scoped admission for only this test's newly created standard accounts.

No impersonation, process creation, group edits or privilege enablement. A deny
assignment is never removed. Native Password/Limited ScheduledTasks still run
the two actual people and prove profile/session/application behavior.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import re

BATCH = "SeBatchLogonRight"
DENY_BATCH = "SeDenyBatchLogonRight"


@contextmanager
def admit_owned_batch(policy):
    """Grant one missing allow, prove real batch admission, restore just it."""
    before = policy.logon(4, missing_ok=True)
    identity = before if before is not None else policy.logon(2)
    # Membership comes from a real token. Replace only the documented logon-
    # class groups when inspecting prospective batch admission before grant.
    allow = {identity["sid"]}
    deny = {identity["sid"]}
    for sid, attributes in identity["groups"]:
        if sid in {"S-1-5-4", "S-1-2-1"} or sid.startswith("S-1-5-5-"):
            continue  # Interactive/console/logon-instance groups are not batch.
        if attributes & 4 and not attributes & 16:
            allow.add(sid)
        if attributes & (4 | 16):
            deny.add(sid)
    allow.add("S-1-5-3")
    deny.add("S-1-5-3")
    refused = deny & policy.assignments(DENY_BATCH)
    assert not refused, f"Batch logon explicitly denied to this test identity: {sorted(refused)}"
    inherited_allow = allow & policy.assignments(BATCH)
    assert before is not None or not inherited_allow, (
        "Native batch admission refused despite an existing allow; no policy bypass", sorted(inherited_allow))
    added = False
    try:
        if before is None:
            policy.change(BATCH, add=True)
            added = True
        after = policy.logon(4)
        assert after["sid"] == identity["sid"]
        yield {"sid": identity["sid"], "groups": identity["groups"],
               "batch_before": before is not None, "batch_after": True,
               "added_own_allow": added, "matched_deny": []}
    finally:
        if added:
            policy.change(BATCH, add=False)
            assert identity["sid"] not in policy.assignments(BATCH), "Own batch allow was not restored"


class WindowsBatchPolicy:
    """LSA writes are fenced to the SID of an account just created by this test."""

    def __init__(self, user, password, created):
        assert user in created and re.fullmatch(r"acl1309[0-9a-f]{12}[12]", user)
        self.user, self.password = user, password
        self.sid_buffer = None
        self.sid = None
        self.policy = wintypes.HANDLE()
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        void, uint = ctypes.c_void_p, ctypes.c_uint32

        class Unicode(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_uint16), ("MaximumLength", ctypes.c_uint16),
                        ("Buffer", wintypes.LPWSTR)]

        class Attributes(ctypes.Structure):
            _fields_ = [("Length", uint), ("RootDirectory", void),
                        ("ObjectName", void), ("Attributes", uint),
                        ("SecurityDescriptor", void), ("SecurityQualityOfService", void)]

        class SidAttributes(ctypes.Structure):
            _fields_ = [("Sid", void), ("Attributes", uint)]

        class Groups(ctypes.Structure):
            _fields_ = [("Count", uint), ("Groups", SidAttributes * 1)]

        self.Unicode, self.Attributes = Unicode, Attributes
        self.SidAttributes, self.Groups = SidAttributes, Groups
        signatures = {
            "LsaOpenPolicy": ([void, ctypes.POINTER(Attributes), uint, ctypes.POINTER(wintypes.HANDLE)], ctypes.c_int32),
            "LsaClose": ([wintypes.HANDLE], ctypes.c_int32),
            "LsaFreeMemory": ([void], ctypes.c_int32),
            "LsaNtStatusToWinError": ([ctypes.c_int32], uint),
            "LsaEnumerateAccountsWithUserRight": ([wintypes.HANDLE, ctypes.POINTER(Unicode), ctypes.POINTER(void), ctypes.POINTER(uint)], ctypes.c_int32),
            "LsaAddAccountRights": ([wintypes.HANDLE, void, ctypes.POINTER(Unicode), uint], ctypes.c_int32),
            "LsaRemoveAccountRights": ([wintypes.HANDLE, void, ctypes.c_ubyte, ctypes.POINTER(Unicode), uint], ctypes.c_int32),
            "LogonUserW": ([wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, uint, uint, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
            "GetTokenInformation": ([wintypes.HANDLE, ctypes.c_int, void, uint, ctypes.POINTER(uint)], wintypes.BOOL),
            "GetLengthSid": ([void], uint),
            "ConvertSidToStringSidW": ([void, ctypes.POINTER(wintypes.LPWSTR)], wintypes.BOOL),
            "LookupAccountSidW": ([wintypes.LPCWSTR, void, wintypes.LPWSTR, ctypes.POINTER(uint), wintypes.LPWSTR, ctypes.POINTER(uint), ctypes.POINTER(uint)], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [void]
        self.kernel.LocalFree.restype = void

    def check_status(self, status):
        if status:
            raise ctypes.WinError(self.api.LsaNtStatusToWinError(status))

    def __enter__(self):
        attributes = self.Attributes()
        attributes.Length = ctypes.sizeof(attributes)
        # VIEW_LOCAL_INFORMATION | CREATE_ACCOUNT | LOOKUP_NAMES; no full access.
        self.check_status(self.api.LsaOpenPolicy(None, ctypes.byref(attributes), 0x811,
                                                ctypes.byref(self.policy)))
        return self

    def __exit__(self, *unused):
        self.password = None
        self.check_status(self.api.LsaClose(self.policy))

    def sid_text(self, sid):
        text = wintypes.LPWSTR()
        if not self.api.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            self.kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def token_info(self, token, kind):
        size = ctypes.c_uint32()
        self.api.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        assert size.value and ctypes.get_last_error() == 122
        data = ctypes.create_string_buffer(size.value)
        if not self.api.GetTokenInformation(token, kind, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return data

    def logon(self, kind, missing_ok=False):
        token = wintypes.HANDLE()
        if not self.api.LogonUserW(self.user, ".", self.password, kind, 0, ctypes.byref(token)):
            error = ctypes.get_last_error()
            if missing_ok and error == 1385:  # ERROR_LOGON_TYPE_NOT_GRANTED only.
                return None
            raise ctypes.WinError(error)
        try:
            data = self.token_info(token, 1)  # TokenUser.
            user_sid = self.SidAttributes.from_buffer(data).Sid
            sid = self.sid_text(user_sid)
            name_size, domain_size, use = (ctypes.c_uint32() for _ in range(3))
            self.api.LookupAccountSidW(None, user_sid, None, ctypes.byref(name_size),
                                       None, ctypes.byref(domain_size), ctypes.byref(use))
            assert name_size.value and domain_size.value
            name = ctypes.create_unicode_buffer(name_size.value)
            domain = ctypes.create_unicode_buffer(domain_size.value)
            assert self.api.LookupAccountSidW(None, user_sid, name, ctypes.byref(name_size),
                                              domain, ctypes.byref(domain_size), ctypes.byref(use))
            assert use.value == 1 and name.value.casefold() == self.user.casefold()
            assert domain.value.casefold() == os.environ["COMPUTERNAME"].casefold()
            if self.sid is not None:
                assert sid == self.sid, "Created account identity changed"
            else:
                self.sid = sid
                self.sid_buffer = ctypes.create_string_buffer(
                    ctypes.string_at(user_sid, self.api.GetLengthSid(user_sid)))
            data = self.token_info(token, 2)  # TokenGroups; preserve deny-only groups.
            count = ctypes.c_uint32.from_buffer(data).value
            entries = (self.SidAttributes * count).from_buffer(data, self.Groups.Groups.offset)
            groups = [(self.sid_text(v.Sid), int(v.Attributes)) for v in entries]
            assert not any(s == "S-1-5-32-544" for s, _ in groups), "Expected a standard account"
            return {"sid": sid, "groups": groups}
        finally:
            self.kernel.CloseHandle(token)

    def right_string(self, right):
        assert right in {BATCH, DENY_BATCH}
        # The wchar buffer stays owned by the returned structure during the call.
        text = ctypes.create_unicode_buffer(right)
        length = len(right.encode("utf-16-le"))
        return self.Unicode(length, length + 2, ctypes.cast(text, wintypes.LPWSTR))

    def assignments(self, right):
        name = self.right_string(right)
        buffer, count = ctypes.c_void_p(), ctypes.c_uint32()
        status = self.api.LsaEnumerateAccountsWithUserRight(
            self.policy, ctypes.byref(name), ctypes.byref(buffer), ctypes.byref(count))
        if status & 0xFFFFFFFF == 0x8000001A:  # STATUS_NO_MORE_ENTRIES.
            return set()
        self.check_status(status)
        try:
            entries = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))
            return {self.sid_text(entries[n]) for n in range(count.value)}
        finally:
            self.api.LsaFreeMemory(buffer)

    def change(self, right, *, add):
        assert right == BATCH and self.sid_buffer is not None
        name = self.right_string(right)
        if add:
            status = self.api.LsaAddAccountRights(self.policy, self.sid_buffer, ctypes.byref(name), 1)
        else:
            status = self.api.LsaRemoveAccountRights(self.policy, self.sid_buffer, False, ctypes.byref(name), 1)
        self.check_status(status)
