# OpenClaw Migration

Use the migration scripts to import an existing OpenClaw setup into Creel.

## What gets migrated

Default migration runs phases `1,2`:

- Phase 1
  - `SOUL.md` -> `workspace/SOUL.md`
  - `MEMORY.md` + `memory/*.md` -> `workspace/MEMORY.md` + `workspace/memory/*.md`
  - `USER.md` -> `workspace/USER.md`
  - `AGENTS.md` -> `workspace/AGENTS.md`
  - Conversation history -> `sessions/*.json` + `sessions/_active.json`
- Phase 2
  - OpenClaw config -> `migrations/openclaw/agent.overlay.yaml`
  - Merged agent config preview -> `migrations/openclaw/agent.migrated.yaml`
  - Cron jobs -> generated task YAML files in `tasks/`

Phase `3` (skills migration) is opt-in and not run by default.

## Dry-run first

```bash
python scripts/migrate-openclaw.py \
  --source ~/.openclaw \
  --report-json /tmp/openclaw-migration-report.json
```

Dry-run shows exactly what would be written, plus warnings/manual actions.

## Apply migration

```bash
python scripts/migrate-openclaw.py --source ~/.openclaw --apply
```

By default this writes into the current repo:

- `workspace/`
- `sessions/`
- `tasks/`
- `migrations/openclaw/`
- `.migration_backups/` (for overwritten files)

To apply into a separate target directory:

```bash
python scripts/migrate-openclaw.py \
  --source ~/.openclaw \
  --target-root /tmp/creel-migrated \
  --apply
```

## Include skills migration (optional)

```bash
python scripts/migrate-openclaw.py \
  --source ~/.openclaw \
  --phases 1,2,3 \
  --apply
```

Or run only phase 3:

```bash
python scripts/migrate-openclaw-phase3.py --source ~/.openclaw --apply
```

## Useful flags

- `--phases 1,2` select migration phases (default: `1,2`)
- `--overwrite / --no-overwrite` control conflict behavior
- `--apply-agent-config` write merged `agent.migrated.yaml` back to `agent.yaml`
- `--sender-id` fallback sender ID for imported sessions
- `--report-json <path>` write a machine-readable report

## Notes for ~/.openclaw users

- Workspace files are auto-detected from `~/.openclaw/workspace`.
- Session history is auto-detected from `~/.openclaw/agents/main/sessions`.
- If a source `.jsonl` file has malformed lines, those lines are skipped and reported as warnings.
