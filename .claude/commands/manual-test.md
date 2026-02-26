# Manual Test — Creel Smoke Suite

Run the Creel smoke test suite and provide full analysis of results.

## Arguments

Parse `$ARGUMENTS` to determine which flags to pass to the smoke runner:

| Argument | Maps to |
|----------|---------|
| `quick` | `--quick` |
| `foundation` | `--phase foundation` |
| `security` | `--phase security` |
| `extras` | `--phase extras` |
| `all` | (default, no phase flag) |
| `--include-extras` | `--include-extras` (don't use `--no-extras`) |
| `--containers` | `--containers` |
| `--live-llm` | `--live-llm` |
| `--live-docker` | `--live-docker` |
| Any case ID (e.g. `foundation-1-llm-retry`) | `--case <id>` (can repeat) |

If `$ARGUMENTS` is empty, run with default flags (all foundation + security + quick phases).

## Step 1: Run the smoke suite

```bash
.venv/bin/python scripts/smoke_runner.py <flags>
```

Capture the full output. The run will create artifacts under `.smoke-runs/<timestamp>/`.

## Step 2: Parse results

Find the latest run directory:

```bash
ls -1d .smoke-runs/*/ | sort | tail -1
```

Read `summary.json` from that directory. Extract:
- Total / pass / fail / skip counts
- Per-phase breakdown
- List of failed cases with their details

## Step 3: Report results

Present a clear summary:

1. **Overall**: X passed, Y failed, Z skipped out of N total
2. **Per-phase table**: show pass/fail/skip counts for each phase
3. **Failures**: for each failed case, show:
   - Case ID and title
   - Status detail from summary.json
   - Exit code if available

## Step 4: Analyze failures

For each failed case:

1. Read the case's `.log` file from the run's `logs/` directory (path is in `log_file` field of summary.json)
2. Look at stdout/stderr in the log
3. Diagnose the root cause (missing dependency, test assertion failure, timeout, import error, etc.)
4. Suggest a fix or next step

## Step 5: Coverage gap analysis

Compare smoke-covered test files against all files in `tests/`:

1. List all test files: `ls tests/test_*.py`
2. Extract test file paths from `scripts/smoke_cases.yaml` (the `command` fields)
3. Report which test files have NO smoke case coverage
4. Recommend specific new smoke cases to fill the gaps

## Notes

- The smoke runner already handles all case execution — do NOT run individual pytest commands yourself
- If the runner exits non-zero, there were failures — this is expected, still parse and report
- Timeout for the full suite can be long (15+ minutes with all phases); for quick feedback use `quick` or a specific phase
