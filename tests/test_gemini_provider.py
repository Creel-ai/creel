"""Tests for the Google Gemini provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from creel.providers.base import LLMMessage, LLMProviderError
from creel.providers.gemini import (
    GeminiProvider,
    _convert_messages_to_gemini,
    _convert_tools_to_gemini,
    _wrap_gemini_error,
)

# -- Tool format conversion --


class TestConvertToolsToGemini:
    def test_basic_tool(self):
        tools = [
            {
                "name": "weather",
                "description": "Get weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            }
        ]

        result = _convert_tools_to_gemini(tools)

        assert len(result) == 1
        assert result[0]["name"] == "weather"
        assert result[0]["description"] == "Get weather"
        assert "properties" in result[0]["parameters"]

    def test_multiple_tools(self):
        tools = [
            {"name": "a", "description": "A", "input_schema": {}},
            {"name": "b", "description": "B", "input_schema": {}},
        ]

        result = _convert_tools_to_gemini(tools)
        assert len(result) == 2
        assert result[0]["name"] == "a"
        assert result[1]["name"] == "b"

    def test_empty_tools(self):
        assert _convert_tools_to_gemini([]) == []

    def test_tool_no_schema(self):
        tools = [{"name": "ping", "description": "Ping"}]
        result = _convert_tools_to_gemini(tools)
        assert result[0]["parameters"] is None


# -- Message format conversion --


class TestConvertMessagesToGemini:
    def test_simple_text_message(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = _convert_messages_to_gemini(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["parts"] == [{"text": "Hello"}]

    def test_assistant_maps_to_model(self):
        messages = [{"role": "assistant", "content": "Hi there"}]
        result = _convert_messages_to_gemini(messages)
        assert result[0]["role"] == "model"
        assert result[0]["parts"] == [{"text": "Hi there"}]

    def test_text_blocks_in_list(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ],
            }
        ]
        result = _convert_messages_to_gemini(messages)
        assert result[0]["role"] == "model"
        assert len(result[0]["parts"]) == 2
        assert result[0]["parts"][0] == {"text": "Hello"}
        assert result[0]["parts"][1] == {"text": "World"}

    def test_tool_use_blocks(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_123",
                        "name": "weather",
                        "input": {"location": "Denver"},
                    }
                ],
            }
        ]
        result = _convert_messages_to_gemini(messages)
        assert result[0]["role"] == "model"
        fc = result[0]["parts"][0]["function_call"]
        assert fc["name"] == "weather"
        assert fc["args"] == {"location": "Denver"}

    def test_tool_result_blocks(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_123",
                        "name": "weather",
                        "input": {"location": "Denver"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_123",
                        "content": "Sunny, 72F",
                    }
                ],
            },
        ]
        result = _convert_messages_to_gemini(messages)
        # tool_result message is the second entry
        fr = result[1]["parts"][0]["function_response"]
        assert fr["name"] == "weather"
        assert fr["response"]["result"] == "Sunny, 72F"

    def test_tool_result_without_prior_tool_use_falls_back(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "orphan_id",
                        "content": "some result",
                    }
                ],
            },
        ]
        result = _convert_messages_to_gemini(messages)
        fr = result[0]["parts"][0]["function_response"]
        assert fr["name"] == "unknown"


# -- Error wrapping --


class TestWrapGeminiError:
    def test_rate_limit_by_message(self):
        exc = Exception("RESOURCE_EXHAUSTED: quota exceeded")
        wrapped = _wrap_gemini_error(exc)
        assert wrapped.status_code == 429

    def test_auth_error_by_message(self):
        exc = Exception("PERMISSION_DENIED: invalid API key")
        wrapped = _wrap_gemini_error(exc)
        assert wrapped.status_code == 403

    def test_transient_by_message(self):
        exc = Exception("UNAVAILABLE: service temporarily down")
        wrapped = _wrap_gemini_error(exc)
        assert wrapped.status_code == 500

    def test_generic_error(self):
        exc = Exception("something else went wrong")
        wrapped = _wrap_gemini_error(exc)
        assert isinstance(wrapped, LLMProviderError)


# -- Provider --


class TestGeminiProvider:
    @patch("creel.providers.gemini._get_gemini_client")
    @patch("creel.providers.gemini._get_genai_module")
    def test_create_text_response(self, mock_genai_module, mock_get_client):
        # Setup mock genai module
        mock_genai = MagicMock()
        mock_genai_module.return_value = mock_genai

        # Setup mock model and response
        mock_model_instance = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model_instance

        # Build mock response
        mock_part = MagicMock()
        mock_part.text = "Hello from Gemini"
        mock_part.function_call = None
        type(mock_part).function_call = property(lambda s: None)
        mock_part.text = "Hello from Gemini"

        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]
        mock_candidate.finish_reason = 1  # STOP

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata.prompt_token_count = 10
        mock_response.usage_metadata.candidates_token_count = 5

        mock_model_instance.generate_content.return_value = mock_response

        provider = GeminiProvider()
        result = provider.create(
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-2.0-flash",
            max_tokens=100,
        )

        assert isinstance(result, LLMMessage)
        assert len(result.content) >= 1
        mock_model_instance.generate_content.assert_called_once()

    @patch("creel.providers.gemini._get_gemini_client")
    @patch("creel.providers.gemini._get_genai_module")
    def test_create_with_system(self, mock_genai_module, mock_get_client):
        mock_genai = MagicMock()
        mock_genai_module.return_value = mock_genai

        mock_model_instance = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model_instance

        mock_part = MagicMock()
        mock_part.text = "I am helpful"
        mock_part.function_call = None
        type(mock_part).function_call = property(lambda s: None)

        mock_candidate = MagicMock()
        mock_candidate.content.parts = [mock_part]
        mock_candidate.finish_reason = 1

        mock_response = MagicMock()
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata = None

        mock_model_instance.generate_content.return_value = mock_response

        provider = GeminiProvider()
        provider.create(
            messages=[{"role": "user", "content": "Hi"}],
            model="gemini-2.0-flash",
            max_tokens=100,
            system="You are helpful.",
        )

        # Verify system_instruction was passed
        call_kwargs = mock_genai.GenerativeModel.call_args[1]
        assert call_kwargs["system_instruction"] == "You are helpful."

    def test_extract_env_vars(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        provider = GeminiProvider()
        env = provider.extract_env_vars()
        assert env["GOOGLE_API_KEY"] == "test-key"

    def test_extract_env_vars_gemini_key(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
        provider = GeminiProvider()
        env = provider.extract_env_vars()
        assert env["GEMINI_API_KEY"] == "gemini-key"

    def test_extract_env_vars_missing(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        provider = GeminiProvider()
        env = provider.extract_env_vars()
        assert "GOOGLE_API_KEY" not in env


# -- Factory registration --


class TestGeminiFactory:
    def test_build_provider_gemini(self):
        from creel.providers.base import build_provider

        provider = build_provider("gemini")
        assert isinstance(provider, GeminiProvider)

    def test_build_provider_gemini_case_insensitive(self):
        from creel.providers.base import build_provider

        provider = build_provider("Gemini")
        assert isinstance(provider, GeminiProvider)

    def test_parse_model_string(self):
        from creel.providers.base import LLMProvider

        provider, model = LLMProvider.parse_model_string("gemini/gemini-2.0-flash")
        assert provider == "gemini"
        assert model == "gemini-2.0-flash"
