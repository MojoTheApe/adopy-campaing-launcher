## ACL-2668 — No limit on heavy account actions at once — 2026-11-16

The "Heavy actions at once" limit (default 1, `ACL_WARM_MAX_HEAVY`) is removed by the owner's decision on audit row 3:
publish, Check plan, commands and reads on different accounts no longer wait for each other. How many run at once is
decided only by the memory gate and "Profiles at once". Two actions on the same profile still never run together
(the profile lock). The field is gone from Settings → Run limits; an old `ACL_WARM_MAX_HEAVY` value in `.env` is ignored.
[Task](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/1668).
