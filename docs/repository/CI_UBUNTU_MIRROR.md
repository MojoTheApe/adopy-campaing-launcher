# Hosted Ubuntu mirror preparation

The Linux Chromium preparation in `ci.yml` runs `scripts/ci_ubuntu_mirror.py`
before either existing cache-hit or fresh-browser installation path. It handles
the hosted `URIs: mirror+file:/etc/apt/apt-mirrors.txt` indirection as well as
direct Ubuntu archive sources. The earlier literal replacement in source files
left the indirect Azure archive active; ACL-2146 fixes that bounded CI failure.
The [APT mirror transport documentation](https://manpages.debian.org/trixie/apt/apt-transport-mirror.1.en.html)
describes local mirror lists and per-line URI/metadata selection.

The helper considers the existing `sources.list.d/ubuntu.sources` and
`sources.list`, plus local mirror lists actually referenced by their active
URI fields. Only exact `azure.archive.ubuntu.com/ubuntu` archive URI tokens
change to `archive.ubuntu.com`; their scheme/trailing slash, list metadata,
security URLs, signing fields, suites and unrelated repositories remain intact.
Disabled/commented entries are preserved. Local references cannot escape the apt
directory. A failed preparation stops the step rather than silently continuing.

`tests/test_ci_ubuntu_mirror.py` uses temporary source/mirror fixtures and invokes
no apt/download command. The helper's `--apt-root` option exists for those offline
fixtures; the CI default is `/etc/apt`. The actual hosted Linux install log must
still demonstrate archive downloads and progression to pytest. Fixture success
does not establish native runtime, main admission or process delivery.

This helper is CI-only. It is not imported by the application, bundled in the
Windows installer or used by the updater. No application, dependency selection,
matrix, retry/timeout limit, publication workflow, policy or frozen batch changes.
Independent review must verify that isolation and the supported delivery proof;
this note grants no installation receipt or process-completion exception.
