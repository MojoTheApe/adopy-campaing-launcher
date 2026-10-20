# Changelog fragments

Do not edit the top of `../CHANGELOG.md` in a task PR: every merge into main would
make all other open PRs conflict there. Add one file per task instead:

`docs/system/changelog.d/ACL-NNNN.md`

```
## ACL-NNNN — Short title — YYYY-MM-DD

What changed and why, a few lines.
[Task](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/NNN).
```

A second entry for the same task uses `ACL-NNNN-suffix.md` (e.g. `ACL-NNNN-followup.md`).
The heading format is the same as in `CHANGELOG.md`; the number must match the file
name. Batch integration or release folds all fragments into `CHANGELOG.md`
(newest first) and deletes them:

```
python scripts/fold_changelog.py          # fold and delete
python scripts/fold_changelog.py --check  # validate only
```

Process-only changes still go to `docs/repository/CHANGELOG.md`.
