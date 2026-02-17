# Creel Next Phase Proposal

*Generated: 2026-02-16*

---

## 1. Easier Installation & Onboarding

### Current State

The install flow requires **8+ manual steps**: brew install (pyenv, uv, age), git clone, pyenv install, uv venv, uv pip install, age keygen, encrypt secrets, configure agent.yaml. For Google services, add OAuth setup on top. This is a 30-minute process for an experienced developer; it's a wall for anyone else.

### Competitor Analysis

| Framework | Install | Time to "Hello World" |
|-----------|---------|----------------------|
| **CrewAI** | `pip install crewai && crewai create crew my_crew` | ~2 min |
| **OpenClaw** | `brew install openclaw` → `openclaw gateway start` | ~3 min |
| **AutoGPT** | Docker Compose + `.env` file | ~10 min |
| **LangChain** | `pip install langchain` + config | ~5 min |
| **Creel** | 8+ steps, secrets encryption, Docker builds | ~30 min |

### Proposals (Priority Order)

#### P1: `creel init` Interactive Wizard

The single highest-impact change. After install, the user runs one command that handles everything:

```bash
creel init

# Output:
🦞 Welcome to Creel!

? Anthropic API key: sk-ant-...  ✓ Verified
? Enable Google Calendar? (y/N) y
  → Opening browser for OAuth...  ✓ Connected
? Enable Gmail? (y/N) y
  → Using same Google credentials  ✓ Connected
? Enable web search (Brave)? (y/N) n
? Enable Apple integrations (Notes, Reminders, Things)? (Y/n) y

✓ Created agent.yaml
✓ Encrypted 2 secrets
✓ Generated age key at ~/.age/key.txt

Run `creel daemon start` to start your agent!
Or `creel attach` for interactive chat.
```

**Implementation:**
- New `taskrunner/cli_init.py` module
- Uses `questionary` or simple `input()` prompts
- Generates `agent.yaml` from template with only enabled tools
- Handles age key generation automatically
- Validates API keys before saving
- ~500 LOC, 1-2 days of work

#### P2: Reduce Prerequisites to Two

Current: pyenv + uv + age + Docker + git (5 tools).
Target: **uv + Docker** (2 tools, both already common).

- **Drop pyenv**: `uv` can manage Python versions directly (`uv python install 3.12`)
- **Bundle age**: Use `pyrage` (already a dependency!) for encryption instead of shelling out to `age` CLI
- **Docker**: Keep as optional for container isolation; non-container mode works without it

```bash
# New install flow:
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/creel-ai/creel.git && cd creel
uv run creel init
```

That's **3 commands** from zero to configured.

#### P3: Pre-built Docker Images on GHCR

Stop making users build executor images locally.

```yaml
# GitHub Actions workflow
name: Build Executor Images
on:
  push:
    tags: ['v*']
jobs:
  build:
    strategy:
      matrix:
        executor: [calendar, gmail, brave_search, weather, fetch_url, exec]
    steps:
      - uses: docker/build-push-action@v5
        with:
          tags: ghcr.io/creel-ai/creel-${{ matrix.executor }}:latest
```

Users pull instead of building. `creel init` can offer to pull images.

#### P4: Docker Compose for Full Stack

For users who want everything containerized:

```yaml
# docker-compose.yml
version: '3.8'
services:
  creel:
    image: ghcr.io/creel-ai/creel:latest
    ports:
      - "8765:8765"  # daemon API
    volumes:
      - ./agent.yaml:/app/agent.yaml
      - ./secrets:/app/secrets
      - creel-sessions:/app/sessions
    environment:
      - AGE_KEY_FILE=/app/secrets/key.txt

  bridge:
    image: ghcr.io/creel-ai/creel-bridge:latest
    network_mode: host  # needs macOS access
    # Only on macOS for Apple integrations

volumes:
  creel-sessions:
```

```bash
# One command to run everything
docker compose up -d
```

#### P5: Homebrew Formula

```bash
brew tap creel-ai/tap
brew install creel
creel init
```

Formula installs Python, creel package, and handles PATH. This is the gold standard for macOS users.

#### P6: Revamp Getting Started Guide

The current quickstart is decent but buries the lead. Restructure:

1. **30-second version** (just chat, no tools): `uv run creel chat` with just an API key
2. **5-minute version** (with tools): `creel init` wizard
3. **Full setup** (container isolation, Google OAuth, iMessage): detailed guide
4. **Video walkthrough** (2-min screencast)

### Priority Ranking

| Priority | Item | Impact | Effort |
|----------|------|--------|--------|
| 🔴 P1 | `creel init` wizard | Massive — removes 80% of friction | 2 days |
| 🔴 P1 | Drop pyenv, use uv for Python | Easy win | 0.5 day |
| 🟡 P2 | Use pyrage instead of age CLI | Removes a prerequisite | 0.5 day |
| 🟡 P2 | Pre-built Docker images | Removes build step | 1 day |
| 🟡 P2 | Revamp getting started guide | First impression matters | 1 day |
| 🟢 P3 | Docker Compose full stack | Power users | 1 day |
| 🟢 P3 | Homebrew formula | Polish | 1 day |

---

## 2. Making Creel More Useful

### Current Tool Inventory

Strong: Google Suite (Calendar, Gmail, Drive), Apple ecosystem (Notes, Reminders, Things 3), Web (Search, Fetch), Browser, iMessage, Exec.

Missing: Everything social, dev tools, smart home, music, finance.

### High-Impact Integrations

#### Tier 1: Daily Driver (makes Creel indispensable)

**1. GitHub Integration** — Priority 1
```yaml
github_issues:
  executor: github
  secrets: secrets/github.env.enc
  description: "List, create, and manage GitHub issues"
```
- PR review summaries, CI status monitoring, issue triage
- Developers live in GitHub; connecting it makes Creel a dev companion
- OpenClaw has a `gh` CLI skill — similar approach works

**2. Slack Integration** — Priority 1
- Read/send messages, react, summarize channels
- "Summarize what I missed in #engineering today"
- Critical for anyone who works on a team

**3. Home Assistant** — Priority 2
- Control lights, locks, thermostats, cameras
- "Turn off the garage lights" via iMessage
- OpenClaw has `openhue` skill — broader HA integration is better
- Makes Creel useful for the whole household, not just work

**4. Spotify / Music Control** — Priority 2
- Play/pause/skip, queue management, playlist creation
- "Play something chill" — low-effort, high-delight interaction
- OpenClaw has `spotify-player` skill

**5. Notion Integration** — Priority 2
- Already have the API patterns from this project's own Notion usage
- Database queries, page creation, project tracking
- Bridges the gap between task management and knowledge management

#### Tier 2: Power User

**6. Linear/Jira** — Project management for dev teams
**7. Financial tracking** — Plaid API for bank accounts, expense categorization
**8. Todoist** — Cross-platform alternative to Things 3
**9. 1Password** — Secure credential lookup (OpenClaw has this skill)
**10. Obsidian** — Vault search and note creation for PKM users

### Killer Workflows

These are what make someone *need* Creel daily:

#### Morning Briefing Pipeline
```
6:30 AM trigger →
  1. Check calendar (next 12 hours)
  2. Check weather
  3. Scan unread emails (flag urgent)
  4. Check GitHub notifications
  5. Summarize Slack overnight activity
  → Deliver via iMessage as a single formatted briefing
```
*Already partially built with task YAML files. Needs: Slack + GitHub + better formatting.*

#### Email Triage → Task Creation
```
User: "Triage my inbox"
  1. Scan unread emails
  2. Categorize: urgent / action-needed / FYI / trash
  3. For action items: create Things 3 tasks with context
  4. Archive FYI emails, flag urgent
  5. Report summary
```
*Most pieces exist. Needs: smarter categorization, batch operations.*

#### Dev Standup Generator
```
Daily 8:55 AM →
  1. Check GitHub: PRs merged, PRs reviewed, commits pushed
  2. Check Linear/Jira: tickets moved
  3. Check calendar: meetings attended
  → Generate standup update, post to Slack #standups
```

#### Expense Tracking
```
User forwards receipt email →
  1. Parse vendor, amount, date
  2. Categorize (matching existing categories)
  3. Append to Google Sheets
  4. Archive email
```
*Already implemented for RK Customs! Generalize into a reusable workflow.*

### The "Aha Moment"

The moment a new user thinks "oh, this is actually useful" — it needs to happen in the **first 5 minutes**.

Best candidates:
1. **"What's on my calendar today?"** → Instant value, zero setup friction (just needs Google OAuth)
2. **"Summarize my unread emails"** → Shows the agent is actually useful, not a toy
3. **Morning briefing delivery** → Waking up to a personalized summary feels like the future

### Making Creel Sticky

What makes someone miss Creel when it's gone:

1. **Memory** — It remembers your preferences, past conversations, context. Starting over with a new tool means re-teaching everything.
2. **Workflows** — Once morning briefings and email triage are habit, losing them hurts.
3. **iMessage interface** — No app to open. It's just there in your messages, always accessible.
4. **Accumulation** — The more tools connected, the more powerful cross-tool workflows become. Switching cost increases over time.

---

## 3. New Platform Features

### Existing Roadmap (from Notion)

Already planned:
- Multi-model support (P20, Phase 4)
- Sub-agent / multi-agent sessions (P20, Phase 4)  
- Skills/plugin system (P20, Phase 4)
- Token usage/cost tracking (P14, Phase 4)
- Schedule from chat (P9, Phase 3)
- Notification tool / proactive iMessage (P7, Phase 3)

### New Feature Proposals

#### P1: Plugin/Skill System — Priority 1, Phase 4

The single most important platform feature. Without it, every new tool requires code changes to Creel core.

```
creel-plugins/
├── creel-github/
│   ├── plugin.yaml          # tool definitions
│   ├── executor.py          # or Dockerfile
│   └── README.md
├── creel-slack/
└── creel-homeassistant/
```

```yaml
# plugin.yaml
name: github
version: 1.0.0
tools:
  list_issues:
    description: "List GitHub issues"
    parameters:
      repo: { type: string, required: true }
    secrets: [GITHUB_TOKEN]
    executor: python
    script: executor.py
```

```bash
creel plugin install creel-github
creel plugin list
creel plugin remove creel-github
```

**Architecture:** Plugins are just directories with a `plugin.yaml` and executor code. `creel init` can offer popular plugins. Community can contribute via GitHub repos.

#### P2: Webhooks / Event Triggers — Priority 1, Phase 5

Let external services trigger the agent:

```yaml
# agent.yaml
webhooks:
  enabled: true
  port: 8780
  endpoints:
    github-push:
      secret: $WEBHOOK_SECRET
      action: "Summarize this GitHub push and notify me: {payload}"
    stripe-payment:
      secret: $STRIPE_WEBHOOK
      action: "New payment received: {payload.amount}. Log to expenses."
```

```bash
# Expose via ngrok or Cloudflare Tunnel
creel webhooks start
```

This transforms Creel from pull-only to push+pull. Huge for automation.

#### P3: Web UI — Priority 2, Phase 5

Not everyone wants a CLI. A lightweight web interface:

```
┌─────────────────────────────────────────┐
│ 🦞 Creel                    ⚙️ Settings │
├─────────────────────────────────────────┤
│                                         │
│  You: What's on my calendar today?      │
│                                         │
│  Creel: You have 3 events:              │
│  • 9:00 AM — Standup                    │
│  • 11:00 AM — Design Review             │
│  • 2:00 PM — 1:1 with Sarah             │
│                                         │
│  [________________________________] Send │
└─────────────────────────────────────────┘
```

**Implementation:** FastAPI already runs the daemon. Add a `/ui` route serving a simple SPA (htmx or React). SSE for streaming. The daemon API already supports everything needed.

Effort: ~3-5 days for a basic but functional UI.

#### P4: RAG / Knowledge Base — Priority 2, Phase 5

Let Creel search over user documents:

```bash
creel knowledge add ~/Documents/work/
creel knowledge add --url https://docs.mycompany.com
```

- Index with embeddings (local or API)
- Auto-tool: `search_knowledge` available to the agent
- Enables "What did we decide about the pricing model?" type queries
- SQLite + FAISS for local-first approach

#### P5: Proactive Agent Behavior — Priority 1, Phase 4

Already partially planned (notification tool). Expand to:

```yaml
# agent.yaml
monitors:
  - name: email_urgent
    check: "check_email query='is:unread is:important newer_than:1h'"
    interval: 30m
    condition: "results.count > 0"
    action: "notify via iMessage: You have {count} urgent unread emails"

  - name: pr_reviews
    check: "github_prs filter='review-requested'"
    interval: 1h
    condition: "results.count > 0"
    action: "notify: {count} PRs need your review"
```

This is the leap from "tool I use" to "assistant that works for me."

#### P6: Multi-User Support — Priority 3, Phase 5

Family or team sharing:

```yaml
users:
  ross:
    channels: [imessage:+1555...]
    tools: [all]
    role: admin
  partner:
    channels: [imessage:+1555...]
    tools: [calendar, reminders, home_assistant]
    role: user
```

Each user gets their own session, memory, and tool permissions. Critical for household use (shared Home Assistant, family calendar).

#### P7: Multi-Model with Failover — Priority 2, Phase 4

Already on roadmap but needs concrete design:

```yaml
llm:
  primary:
    provider: anthropic
    model: claude-sonnet-4-20250514
  fallback:
    provider: openai
    model: gpt-4o
  local:
    provider: ollama
    model: llama3.2
    use_for: [fast_classifier, summarization]  # offload cheap tasks
```

Use local models for the guardian classifier instead of the ONNX approach — simpler, more flexible.

#### P8: Automation / Workflow Engine — Priority 2, Phase 5

IFTTT-style declarative workflows:

```yaml
# workflows/expense-tracking.yaml
name: Auto Expense Tracking
trigger:
  type: email
  filter: "from:receipts@ OR from:noreply@stripe.com"
steps:
  - tool: read_email
    extract: [vendor, amount, date]
  - tool: categorize
    model: local  # cheap classification
  - tool: append_sheet
    sheet: "Expenses 2026"
  - tool: trash_email
```

```bash
creel workflow list
creel workflow run expense-tracking
creel workflow enable expense-tracking  # auto-trigger
```

### Feature Priority Matrix

| Priority | Feature | Impact | Effort | Phase |
|----------|---------|--------|--------|-------|
| 🔴 1 | Plugin/skill system | Unlocks everything | 1 week | 4: Growth |
| 🔴 1 | `creel init` wizard | 10x onboarding | 2 days | 4: Growth |
| 🔴 1 | Proactive monitors | Agent → Assistant | 3 days | 4: Growth |
| 🔴 1 | Webhooks/event triggers | Push + Pull | 3 days | 5: Platform |
| 🟡 2 | GitHub integration | Dev daily driver | 2 days | 4: Growth |
| 🟡 2 | Web UI | Broader audience | 5 days | 5: Platform |
| 🟡 2 | RAG/knowledge base | Deep personalization | 1 week | 5: Platform |
| 🟡 2 | Multi-model failover | Resilience + cost | 3 days | 4: Growth |
| 🟡 2 | Workflow engine | Automation power | 1 week | 5: Platform |
| 🟢 3 | Multi-user support | Household/team | 1 week | 5: Platform |
| 🟢 3 | Slack integration | Team use | 2 days | 4: Growth |
| 🟢 3 | Home Assistant | Household | 2 days | 4: Growth |
| 🟢 3 | Homebrew formula | Polish | 1 day | 4: Growth |

---

## Summary: Top 10 Most Impactful Ideas

1. **`creel init` wizard** — Removes 80% of setup friction. Do this first.
2. **Plugin/skill system** — Unlocks community contributions and rapid tool addition.
3. **Proactive monitors** — Transforms Creel from reactive tool to proactive assistant.
4. **GitHub integration** — Makes Creel a dev daily driver.
5. **Webhooks/event triggers** — External events can trigger agent actions.
6. **Pre-built Docker images + reduced prerequisites** — Install in 3 commands, not 15.
7. **Web UI** — Opens Creel to non-CLI users.
8. **Morning briefing pipeline** (multi-tool workflow) — The "aha moment" for new users.
9. **RAG/knowledge base** — Ask questions about your own documents.
10. **Multi-model failover** — Resilience, cost optimization, local model offloading.

---

*Next step: Create Notion tickets for each of these items.*
