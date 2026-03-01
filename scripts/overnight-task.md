# Overnight Task — Serial Claude Code Sessions for High-Priority Creel Issues

## Instructions (from Ross)
Run Claude Code sessions in serial for all high-priority Creel tickets. For each:
1. Write a spec
2. Implement via Claude Code (`--dangerously-skip-permissions --print`)
3. Push the branch
4. Open a PR
5. Move to the next ticket

**Branch each off the previous** so Ross gets a chain of PRs to review in the morning.

## High-Priority Issues (filtered — excluding ones with existing PRs)

| # | Title | Status |
|---|-------|--------|
| 168 | Add coding tool | TODO |
| 161 | Speed up launch | TODO |
| 151 | Simplify channel plugins | TODO |
| 146 | Sub-agents (parallel background tasks) | TODO |
| 131 | GitHub integration (issues, PRs, CI) | TODO |

### Already have PRs (skip these):
- #128 — creel init + pipx (PR #165)
- #152 — Keep LLM container warm (PR #157)
- #154 — Media messages (PR #167)
- #164 — Dashboard (PR #166)

## Execution Order (by dependency + impact)

1. **#161 — Speed up launch** — Foundational, affects everything. Move startup work to background threads, bind uvicorn early.
2. **#151 — Simplify channel plugins** — Clean architecture win, makes future work easier.
3. **#131 — GitHub integration** — New executor/tool, standalone.
4. **#168 — Add coding tool** — New executor/tool, standalone.
5. **#146 — Sub-agents** — Most complex, benefits from cleaner architecture.

## Branch Chain
```
main
  └── fix/speed-up-launch (#161)
       └── feat/simplify-channels (#151)
            └── feat/github-integration (#131)
                 └── feat/coding-tool (#168)
                      └── feat/sub-agents (#146)
```

## Base Branch
Start from: `main` (latest)
