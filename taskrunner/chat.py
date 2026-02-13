"""Chat server - wires channels, sessions, and the agent loop together."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from taskrunner.agent import run_agent_loop
from taskrunner.models import AgentDefinition
from taskrunner.session import SessionManager

logger = logging.getLogger(__name__)

# Special commands handled before the agent loop
_CLEAR_COMMANDS = {"clear", "reset", "/clear", "/reset"}


class ChatServer:
    """Connects a channel to the agent loop via session management."""

    def __init__(self, agent_def: AgentDefinition):
        self._agent_def = agent_def
        self._session_mgr = SessionManager(
            sessions_dir=agent_def.session.sessions_dir,
            max_history=agent_def.session.max_history,
        )

        # Initialize guardian if configured and enabled
        self._guardian = None
        if agent_def.guardian and agent_def.guardian.enabled:
            from guardian import Guardian

            self._guardian = Guardian(agent_def.guardian)
            logger.info("Guardian enabled")

    def handle_message(self, sender_id: str, text: str) -> str:
        """Process an incoming message and return a response.

        This is the callback passed to channels.
        """
        # Handle clear command
        if text.strip().lower() in _CLEAR_COMMANDS:
            self._session_mgr.clear(sender_id)
            return "Session cleared."

        # Screen input through guardian (before adding to session)
        if self._guardian:
            screen_result = self._guardian.screen_input(text)
            if screen_result.blocked:
                logger.warning("Guardian blocked input from %s", sender_id)
                return screen_result.rejection_message

        # Add user message and get session with history
        session = self._session_mgr.add_user_message(sender_id, text)

        # Build system prompt with current date
        now = datetime.now(timezone.utc)
        system_prompt = self._agent_def.system_prompt.format(
            date=now.strftime("%A, %B %d, %Y"),
        )

        # Load LLM secrets if configured
        if self._agent_def.llm.secrets:
            from taskrunner.orchestrator import _load_secrets_to_env
            _load_secrets_to_env(self._agent_def.llm.secrets)

        # Run the agent loop
        result = run_agent_loop(
            messages=session.messages,
            llm_config=self._agent_def.llm,
            tools_config=self._agent_def.tools,
            agent_config=self._agent_def.agent,
            system_prompt=system_prompt,
            guardian=self._guardian,
        )

        logger.info(
            "Agent response for %s: %d chars, %d turns, %d tool calls (%s)",
            sender_id,
            len(result.text),
            result.turns_used,
            result.tool_calls_made,
            result.stop_reason,
        )

        # Save the updated messages (agent loop mutates the list)
        self._session_mgr._save(session)

        return result.text
