# Architecture Overview

## Why Creel?

Agentic LLM systems give the model access to tools, credentials, and untrusted input all at once. Creel enforces **per-tool isolation** — each executor runs in its own container with only the credentials it needs:

| Component | Has access to | Does NOT have |
|-----------|--------------|---------------|
| Each executor | Only its own credential (one OAuth scope, one API key) | LLM, other tools' credentials |
| Bridge executors | Scoped HTTP token for one macOS app | LLM, other bridge endpoints |
| LLM Runner | Anthropic API key only | Any tool credentials |
| Orchestrator | All secrets, LLM output | Untrusted external input |

Even if prompt injection occurs (e.g., via a calendar event title), the LLM container has nothing to exfiltrate except its own API key. A compromised executor can only access its one scoped credential — not your email, not your files, not your messages.

## System Architecture

```mermaid
flowchart TD
    subgraph orch["Orchestrator"]
        direction TB
        schedule["Scheduler / Agent Loop"]
        guardian["Guardian Pipeline"]
        template["Prompt Builder"]
        output["Output Router"]
    end

    subgraph containers["Docker Executors (isolated)"]
        direction TB
        google["Google Suite\n📅 Calendar · 📧 Gmail · 📁 Drive"]
        web["Web Tools\n🔍 Search · 🌐 Fetch · 🌤 Weather"]
        exec["Shell / Exec\n⚙️ Mounted paths only"]
    end

    subgraph bridge["Host Bridge (macOS native)"]
        direction TB
        bridge_api["FastAPI Server"]
        native["📝 Notes · ✅ Reminders\n📋 Things 3 · 💬 iMessage"]
    end

    subgraph llm_container["LLM Container"]
        llm["Claude\n🔑 Anthropic API key only"]
    end

    subgraph channels["Channels"]
        cli["TUI / CLI"]
        imsg["iMessage"]
    end

    channels -- "message" --> orch
    schedule -- "tool call" --> guardian
    guardian -- "approved" --> containers
    guardian -- "approved" --> bridge_api
    bridge_api --> native
    containers -- "JSON result" --> template
    bridge_api -- "JSON result" --> template
    template -- "prompt\n(no secrets)" --> llm
    llm -- "response" --> output
    output --> channels

    style containers fill:#2d333b,stroke:#f47067,stroke-width:2px,color:#f0f0f0
    style bridge fill:#2d333b,stroke:#fd7e14,stroke-width:2px,color:#f0f0f0
    style llm_container fill:#2d333b,stroke:#f47067,stroke-width:2px,color:#f0f0f0
    style orch fill:#2d333b,stroke:#58a6ff,stroke-width:2px,color:#f0f0f0
    style channels fill:#2d333b,stroke:#3fb950,stroke-width:2px,color:#f0f0f0
```

!!! info "Key insight"
    Each red box is a separate security boundary. The LLM never sees credentials. Executors only get their own scoped secret. Even a compromised tool can't reach other tools' data.
