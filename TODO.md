# Creel — TODO

Roadmap and task tracking lives in [Notion](https://www.notion.so/3064bc06d35181238bcc1ec602763456c).

## Architecture Debt

- `orchestrator.py` inline executor switch statement → needs registry pattern
- `_load_secrets_to_env` mutates `os.environ` globally → not concurrent-safe
- `ChatServer` → `run_agent_loop` mutates message list in-place → fragile
- No dependency injection — everything created inside `ChatServer.__init__`
