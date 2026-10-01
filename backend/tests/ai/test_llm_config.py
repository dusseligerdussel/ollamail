import pytest
from pydantic import ValidationError

from app.ai.llm.config import EnvConfigResolver
from app.ai.llm.profiles import PROFILES
from app.ai.llm.types import LLMTask
from app.core.config import LLMSettings


async def test_profile_defaults_apply_to_all_tasks() -> None:
    resolver = EnvConfigResolver(LLMSettings(profile="gpu-consumer"))
    profile = PROFILES["gpu-consumer"]

    triage = await resolver.resolve(LLMTask.TRIAGE)
    embeddings = await resolver.resolve(LLMTask.EMBEDDINGS)

    assert triage.model == profile.chat_model
    assert triage.context_tokens == profile.context_tokens
    assert triage.endpoint.name == "default"
    assert triage.endpoint.provider == "ollama"
    assert embeddings.model == profile.embedding_model


async def test_overrides_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_LLM_PROFILE", "cpu")
    monkeypatch.setenv("OLLAMAIL_LLM_DEFAULT_CHAT_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMAIL_LLM_CONTEXT_TOKENS", "4096")
    monkeypatch.setenv("OLLAMAIL_LLM_TASK_DIGEST_MODEL", "big-model")
    monkeypatch.setenv("OLLAMAIL_LLM_TASK_DIGEST_ENDPOINT", "vllm")
    monkeypatch.setenv(
        "OLLAMAIL_LLM_ENDPOINTS",
        '{"vllm": {"provider": "openai_compatible", "base_url": "http://vllm:8000/v1",'
        ' "api_key": "sk-test", "timeout": 30}}',
    )
    resolver = EnvConfigResolver(LLMSettings())

    triage = await resolver.resolve(LLMTask.TRIAGE)
    digest = await resolver.resolve(LLMTask.DIGEST)

    assert triage.model == "llama3.2:3b"
    assert triage.context_tokens == 4096
    assert digest.model == "big-model"
    assert digest.endpoint.name == "vllm"
    assert digest.endpoint.provider == "openai_compatible"
    assert digest.endpoint.api_key == "sk-test"
    assert digest.endpoint.timeout == 30


async def test_cloud_switch_and_retries() -> None:
    resolver = EnvConfigResolver(LLMSettings(cloud_enabled=True, structured_output_retries=4))

    assert await resolver.cloud_allowed() is True
    assert await resolver.structured_output_retries() == 4
    assert await EnvConfigResolver(LLMSettings()).cloud_allowed() is False


def test_unknown_task_endpoint_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LLMSettings(task_triage_endpoint="missing")


def test_default_endpoint_name_is_reserved() -> None:
    with pytest.raises(ValidationError):
        LLMSettings.model_validate({"endpoints": {"default": {"base_url": "http://x"}}})


async def test_api_key_is_not_rendered() -> None:
    settings = LLMSettings.model_validate({"api_key": "sk-hunter2"})
    endpoint = (await EnvConfigResolver(settings).resolve(LLMTask.TRIAGE)).endpoint

    assert "hunter2" not in repr(settings)
    assert "hunter2" not in repr(endpoint)
