# SPEC: Sub-Agents

## What It Does

Sub-agents let the main agent spawn child agent runs for parallel or background work. Instead of doing everything sequentially in one conversation, the agent can kick off isolated tasks and get results back automatically.

Use cases:
- "Research these 3 topics" → spawn 3 sub-agents in parallel, collect results
- "Refactor this codebase" → spawn a long-running coding agent, get notified when done
- "Summarize this document while I keep chatting" → background task, no interruption
- Agent self-delegates: breaks a complex task into subtasks

## How It Works

```
┌──────────────────┐
│   Main Agent     │
│   (main session) │
└───────┬──────────┘
        │ spawn(task, ...)
        │
   ┌────┼──────────────┐
   ▼    ▼              ▼
┌──────┐ ┌──────┐ ┌──────┐
│Sub 1 │ │Sub 2 │ │Sub 3 │   Each runs in an isolated session
│      │ │      │ │      │   with its own agent loop + tools
└──┬───┘ └──┬───┘ └──┬───┘
   │        │        │
   ▼        ▼        ▼
   Results announced back to main session
   (or delivered to a channel)
```

### Execution model

Each sub-agent:
- Runs in its **own isolated session** (separate conversation history)
- Has access to the **same tools** as the main agent (executors, browser, etc.)
- Can optionally use a **different model** (e.g., cheaper model for simple tasks)
- Has a **timeout** (default 5 minutes, configurable)
- **Reports back** when done — result is injected into the main session as a system event

Sub-agents run **concurrently** in background threads. The main agent doesn't block — it can keep chatting or spawn more sub-agents.

### Lifecycle

1. Main agent calls `spawn(task="...", label="research", model="...", timeout=300)`
2. Scheduler creates an isolated session, starts an agent loop with the task as the prompt
3. Sub-agent runs tools, produces a result
4. On completion: result summary is injected back into the main session
5. On failure/timeout: error is injected back into the main session
6. Session is cleaned up (deleted by default, or kept for debugging)

### Management

The main agent (or user via CLI) can:
- **List** running sub-agents and their status
- **Steer** a running sub-agent by injecting a follow-up message
- **Kill** a sub-agent that's stuck or no longer needed

## Config Surface

### Agent tool

The agent gets a `spawn` tool:

```
spawn:
  task: "Research the top 5 competitors in the AI agent space"
  label: "competitor-research"     # optional human-readable label
  model: "anthropic/claude-haiku"  # optional model override
  timeout_seconds: 300             # optional, default 300
  cleanup: "delete"                # "delete" (default) or "keep"
```

And a `subagents` tool for management:

```
subagents:
  action: "list"     # list | kill | steer
  target: "competitor-research"   # label or session ID (for kill/steer)
  message: "Also include pricing info"  # for steer only
```

### CLI commands

```
creel subagents list                    # show running sub-agents
creel subagents kill <label-or-id>      # terminate a sub-agent
creel subagents steer <label-or-id> "new instruction"
```

### Global config

```yaml
subagents:
  max_concurrent: 5          # prevent runaway spawning
  default_timeout: 300       # seconds
  default_cleanup: delete    # delete | keep
  default_model: null        # null = same as main agent
```

## Acceptance Criteria

### Spawning
- [ ] Agent spawns a sub-agent → it runs in an isolated session with its own history
- [ ] Sub-agent has access to all configured executors and tools
- [ ] Sub-agent with model override uses the specified model
- [ ] Multiple sub-agents run concurrently (not sequentially)
- [ ] Main agent continues to respond to messages while sub-agents run

### Result delivery
- [ ] Sub-agent completes → result summary injected into main session
- [ ] Sub-agent fails → error message injected into main session
- [ ] Sub-agent times out → timeout error injected into main session
- [ ] Result includes the label so the main agent knows which task finished

### Management
- [ ] `subagents list` shows all running sub-agents with label, status, runtime
- [ ] `subagents kill <label>` terminates a running sub-agent
- [ ] `subagents steer <label> "message"` injects a follow-up into the sub-agent's session
- [ ] Killed sub-agent reports back that it was cancelled

### Guardrails
- [ ] Max concurrent limit enforced — spawn fails with clear error if limit hit
- [ ] Sub-agents cannot spawn their own sub-agents (no recursive spawning)
- [ ] Sub-agent sessions are cleaned up after completion (unless `keep` is set)
- [ ] Sub-agent inherits Guardian pipeline protections (coherence, drift, credential scanning)

### Edge cases
- [ ] Daemon restarts while sub-agent is running → sub-agent is lost (acceptable), main session gets a "sub-agent interrupted" notice on next turn
- [ ] Sub-agent tries to send a message to a channel → allowed (useful for "research and report" patterns)
- [ ] Two sub-agents with the same label → second one gets a suffix (e.g., `research-2`)
- [ ] Sub-agent produces very long output → truncated to reasonable size in the result summary
