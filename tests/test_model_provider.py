"""Model provider settings: OpenAI-compatible endpoint, .env loading, key never reaches the sandbox.

No network: the model client is only constructed, never called.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coding_harness.config import ModelSettings, SandboxSettings, load_dotenv, load_settings
from coding_harness.llm import LangChainModel, make_model
from coding_harness.sandbox import LocalSandbox

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "cosmic-feature" / "harness.yaml"
_VARS = ("HARNESS_PROVIDER", "HARNESS_MODEL", "OPENAI_BASE_URL", "OLLAMA_HOST", "OPENAI_API_KEY", "HARNESS_TEST_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


def test_env_switches_to_openai_compatible_provider(monkeypatch):
    monkeypatch.setenv("HARNESS_PROVIDER", "openai")
    monkeypatch.setenv("HARNESS_MODEL", "Qwen/Qwen3.6-35B-A3B")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.test/v1")
    m = load_settings(CONFIG).model
    assert (m.provider, m.name, m.base_url) == ("openai", "Qwen/Qwen3.6-35B-A3B", "https://llm.example.test/v1")


def test_ollama_default_unchanged():
    m = load_settings(CONFIG).model
    assert m.provider == "ollama" and m.base_url == "http://localhost:11434"


def test_openai_provider_requires_key():
    s = ModelSettings(provider="openai", name="m", base_url="https://llm.example.test/v1")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        make_model(s, [])


def test_openai_provider_builds_client(monkeypatch):
    monkeypatch.setenv("HARNESS_TEST_KEY", "sk-test-not-real")
    s = ModelSettings(provider="openai", name="m", base_url="https://llm.example.test/v1", api_key_env="HARNESS_TEST_KEY")
    assert isinstance(make_model(s, []), LangChainModel)


def test_dotenv_loads_without_overriding(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MODEL", "already-set")
    env = tmp_path / ".env"
    env.write_text('# comment\nOPENAI_API_KEY="sk-from-file"\nHARNESS_MODEL=from-file\n\nnot a line\n', encoding="utf-8")
    loaded = load_dotenv(env)
    assert loaded == ["OPENAI_API_KEY"]
    assert os.environ["OPENAI_API_KEY"] == "sk-from-file"
    assert os.environ["HARNESS_MODEL"] == "already-set"
    assert load_dotenv(tmp_path / "missing.env") == []


def test_api_key_is_not_passed_to_local_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    sb = LocalSandbox(SandboxSettings(), tmp_path, tmp_path)
    env = sb._env(str(tmp_path))
    assert "OPENAI_API_KEY" not in env
    assert "sk-secret-value" not in env.values()


def test_model_check_needs_a_tool_call():
    from coding_harness.llm import ScriptedModel, ai, check_model, tool_call

    assert check_model(ScriptedModel([ai(tool_call("add", {"a": 2, "b": 3}))]))[0] is True
    ok, detail = check_model(ScriptedModel([ai(content="5")]))
    assert not ok and "without the expected tool call" in detail
    ok, detail = check_model(ScriptedModel([ConnectionError("unreachable")]))
    assert not ok and "ConnectionError" in detail
