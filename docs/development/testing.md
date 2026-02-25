# Testing

Every PR should pass this checklist before merge.

## 1. Test Suite (required)

```bash
python3 -m pytest tests/ -x -q
```

- Zero new failures (existing known failures documented below)
- New features must include tests

**Known failures:**

- `test_fast_classifier.py` — requires `transformers` or `optimum+onnxruntime` (guardian ML deps). Not a blocker.

## 2. Import Check (required)

All new modules must import cleanly:

```bash
python3 -c "from <new_module> import <main_function>"
```

No missing dependencies, no circular imports.

## 3. Docker Build (if applicable)

New Dockerfiles must build:

```bash
docker build -t <image-name> executors/<executor_name>/
```

- Image builds without errors
- Image size is reasonable (minimal base ~10MB, full ~500MB max)

## 4. Smoke Test (required)

Manually exercise the new feature. Examples by category:

### Executors

```bash
# Inline test
python3 -c "from executors.<name>.executor import <func>; print(<func>(<args>))"

# Container test (if Docker available)
docker run --rm <image> <args>
```

### Bridge Endpoints

```bash
# Start bridge
python3 runner.py bridge &

# Hit endpoint
curl -s -X POST http://localhost:8099/<tool>/<action> \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"key": "value"}'

# Test auth rejection
curl -s -X POST http://localhost:8099/<tool>/<action> \
  -H "Authorization: Bearer wrongtoken"
```

### Config/Model Changes

```bash
# Verify YAML parsing
python3 -c "from creel.models import load_task; t = load_task('agent.yaml'); print(t)"
```

### CLI Commands

```bash
python3 runner.py --help
python3 runner.py <new_command> --help
```

## 5. Error Paths (required for new features)

Test at least:

- Missing/invalid input — clear error message
- Missing dependencies — helpful error, not stack trace
- Timeouts — handled gracefully
- Auth failures — rejected with 401/403

## 6. Integration (required)

- New executor wired into `orchestrator.py` dispatch?
- New tool defined in `agent.yaml`?
- Existing tests still pass? (no regressions from rename/refactor)

## 7. Security (required)

- [ ] No `shell=True` in subprocess calls
- [ ] No string interpolation in shell commands — use argument lists
- [ ] No secrets in log output or error messages
- [ ] Auth tokens validated on all endpoints
- [ ] Container security flags preserved (`--read-only`, `--cap-drop=ALL`, `--no-new-privileges`)

## 8. Cross-branch Dependencies

Before merging, check:

- Does this branch depend on another unmerged PR?
- Will this break other open PRs? (especially shared files like `orchestrator.py`, `models.py`, `agent.yaml`)
- After merge, do other branches need rebasing?

## macOS Tool Testing

Apple ecosystem tools need permissions pre-granted. First-time setup:

```bash
# Grant Full Disk Access to Terminal (or your terminal app)
# System Settings → Privacy & Security → Full Disk Access → add Terminal

# Test CLIs directly first
memo notes                    # Apple Notes
remindctl today              # Apple Reminders
things inbox --limit 5       # Things 3
imsg --help                  # iMessage
```

If any CLI hangs or shows a permission dialog, fix permissions before testing the executor/bridge.

## CI (GitHub Actions)

The CI workflow runs `python3 -m pytest tests/ -x -q` automatically on push. Guardian ML tests are expected to fail in CI (no model weights). Everything else should pass.

## Running Full Validation

Quick one-liner to validate a branch:

```bash
# Run tests + import check + security scan
python3 -m pytest tests/ -x -q && \
python3 -c "import ast, pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('.').rglob('*.py')]" && \
grep -rn "shell=True" executors/ creel/ bridge/ 2>/dev/null && echo "⚠️  shell=True found!" || echo "✅ No shell=True"
```
