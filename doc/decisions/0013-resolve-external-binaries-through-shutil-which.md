# 0013 — Resolve external binaries through `shutil.which` and pass the resolved path to the subprocess

- **Status:** proposed
- **Date:** 2026-10-08
- **Source:** commit `3af9003` (Windows portability), following `c5e0fce` (harness-eval resolution)
- **Footprint:** `.opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/doctor.py:285-296`

## Context

`_doctor_watch_repos` ran a bare `"gh"` as `subprocess.run`'s first argument. On
Linux and macOS that works, because `execvp` applies `PATH` lookup with `PATHEXT`
semantics. On Windows it does not: `CreateProcess` does not apply `PATHEXT`, so the
`gh.cmd` shim could not be found and the call raised `FileNotFoundError`.

The failure was not Windows-shaped in the output — it surfaced as a crash inside a
diagnostic tool, which is the worst place for a platform difference to appear. It was
found by the CI `windows-latest` leg, which is the only reason it was found at all.

The same class of bug appeared twice more in the same commit: `_declared_run_filename`
rejected any declaration containing a backslash *before* testing absoluteness, so
absolute Windows artefact paths were silently ignored rather than validated.

## Decision

Resolve the binary with `shutil.which("gh")` first, warn and return early if it is
`None`, and pass the **resolved path** to `subprocess.run` rather than the bare name.
Apply the traversal guard in `_declared_run_filename` only to relative items.

## Consequences

This buys a diagnostic that runs identically on all three platforms, and an early
`None` check that turns a crash into a warning: `watch_repos configuré mais gh absent
du PATH`. The code comment at `doctor.py:296` states the reason for passing the
resolved path — a bare argv is not resolved the same way under Windows.

The cost is that `shutil.which` has to run on every check, and the pattern has to be
repeated per binary. The commit notes it deliberately mirrors the existing
`_doctor_opencode_version` handling, so the convention already existed in the file —
this decision extends an established pattern rather than introducing one.

The rejected alternative was to keep the bare name and let the OS resolve it. It was
rejected on the platform evidence: `CreateProcess` does not apply `PATHEXT`.

A second rejected alternative was to guard with `sys.platform == "win32"` and build a
`gh.cmd` name explicitly. It was rejected as a maintenance burden that grows with every
binary, and it hardcodes the assumption that `gh` is installed under that exact name.

## Evidence

```
rg -n '_doctor_watch_repos' -A14 \
  .opencode/plugins/weekly-advisor-engine/weekly_telemetry_aggregator/doctor.py
```

`doctor.py:289` is the `shutil.which("gh")` call; `doctor.py:290-294` is the early
return with its warning; `doctor.py:296` carries the comment explaining why the
resolved path is passed on.

The companion decision on the traversal guard is in the same commit and concerns
`_declared_run_filename` in `report.py`.

Regression coverage for the platform difference lives in `tests/test_windows_compat.py`;
the commit's own verification is recorded as 2059 passed, 1 skipped, 0 failed.

Provenance: `git show 3af9003` and `git show c5e0fce`.
