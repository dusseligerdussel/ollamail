"""Hardware profiles: default models and context size per class of hardware.

The model names are examples that work out of the box with Ollama, not requirements.
Operators override them with ``OLLAMAIL_LLM_DEFAULT_CHAT_MODEL``,
``OLLAMAIL_LLM_DEFAULT_EMBEDDING_MODEL``, ``OLLAMAIL_LLM_CONTEXT_TOKENS`` or per task.
"""

from dataclasses import dataclass

from app.core.config import LLMProfileName


@dataclass(frozen=True, slots=True)
class ModelProfile:
    chat_model: str
    embedding_model: str
    # Context window requested from the server; long mails are shortened to fit.
    context_tokens: int


PROFILES: dict[LLMProfileName, ModelProfile] = {
    # CPU only, ~16 GB RAM: small quantized model.
    "cpu": ModelProfile(chat_model="qwen2.5:3b", embedding_model="bge-m3", context_tokens=8192),
    # 8-24 GB VRAM.
    "gpu-consumer": ModelProfile(
        chat_model="qwen2.5:14b", embedding_model="bge-m3", context_tokens=16384
    ),
    # Server GPUs (typically vLLM behind the OpenAI-compatible API).
    "gpu-server": ModelProfile(
        chat_model="qwen2.5:32b", embedding_model="bge-m3", context_tokens=32768
    ),
}
