# Dashboard connections

Read, Check and Publish adopt an existing debug-enabled profile into the same
managed-session registry and reuse its owned Ads Manager tab. They never claim
a foreign owner or bypass the physical-profile lease, profile binding, memory
queue or Multilogin synchronization policy.

A profile opened by hand without debugging needs an explicit account-specific
confirmation before a graceful stop and automation start. The local dashboard
setting `auto_reopen_manual` opts this computer in; it defaults to false on other
installations. Decline, failed stop, still-running profile, foreign ownership
and busy publication stop before restart. No process killing is used.

Readiness has a shared 45-second maximum across tab creation, helper setup,
navigation and readiness probes. This does not include separately guarded queue,
Multilogin startup or synchronization waits. An unresponsive owned target may
be replaced once within that budget. A Graph GET has a 15-second network deadline
and at most one network recovery retry per client after a healthy page probe.
Login, checkpoint and policy errors stop. Previously sent Meta writes are never
replayed. An uncertain dashboard submission resends the same request ID to find
its existing job without preparing or restarting its busy profile again.

Local fixture tests do not prove the proxy or Meta is available. Installation
acceptance must record the actual source composition and live read-only checks.
