"""Hardware profiles: default models and context size per class of hardware.

The model names are examples that work out of the box with Ollama, not requirements.
Operators override them with ``OLLAMAIL_LLM_DEFAULT_CHAT_MODEL``,
``OLLAMAIL_LLM_DEFAULT_EMBEDDING_MODEL``, ``OLLAMAIL_LLM_CONTEXT_TOKENS``,
``OLLAMAIL_LLM_CALL_TIMEOUT`` or per task.
"""

from dataclasses import dataclass

from app.core.config import LLMProfileName


@dataclass(frozen=True, slots=True)
class ModelProfile:
    chat_model: str
    embedding_model: str
    # Context window requested from the server; long mails are shortened to fit.
    context_tokens: int
    # Seconds one generation call may take in total (``OLLAMAIL_LLM_CALL_TIMEOUT``).
    call_timeout: float


PROFILES: dict[LLMProfileName, ModelProfile] = {
    # CPU only, ~16 GB RAM: small quantized model.
    "cpu": ModelProfile(
        chat_model="qwen2.5:3b", embedding_model="bge-m3", context_tokens=8192, call_timeout=180.0
    ),
    # 8-24 GB VRAM.
    "gpu-consumer": ModelProfile(
        chat_model="qwen2.5:14b", embedding_model="bge-m3", context_tokens=16384, call_timeout=60.0
    ),
    # Server GPUs (typically vLLM behind the OpenAI-compatible API).
    "gpu-server": ModelProfile(
        chat_model="qwen2.5:32b", embedding_model="bge-m3", context_tokens=32768, call_timeout=60.0
    ),
}
