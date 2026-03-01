"""Conftest for mock LLM server tests.

Adds the test-harness directory to sys.path so we can import mock_llm_server.
"""

import sys
from pathlib import Path

# Add the test-harness directory to the path so mock_llm_server is importable
_harness_dir = Path(__file__).resolve().parent.parent
if str(_harness_dir) not in sys.path:
    sys.path.insert(0, str(_harness_dir))
