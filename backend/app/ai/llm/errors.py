"""LLM errors.

Messages never contain prompts, model output or response bodies (docs/PRIVACY.md): only
model and endpoint names, status codes and counts.
"""


class LLMError(Exception):
    """Base class for all LLM errors."""


class LLMUnavailableError(LLMError):
    """Endpoint unreachable, timed out or failed with a server error (retry later)."""


class LLMRequestError(LLMError):
    """The endpoint rejected the request (4xx)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StructuredOutputUnsupportedError(LLMRequestError):
    """The endpoint rejected the JSON-schema parameter; the caller may fall back to prompting."""


class ModelNotAvailableError(LLMError):
    """The configured model is not installed/served by the endpoint."""

    def __init__(self, model: str) -> None:
        super().__init__(f"model {model!r} is not available")
        self.model = model


class LLMOutputError(LLMError):
    """The model did not produce output matching the schema, even after retries."""

    def __init__(self, schema_name: str, attempts: int) -> None:
        super().__init__(f"no valid {schema_name} after {attempts} attempt(s)")
        self.schema_name = schema_name
        self.attempts = attempts


class CloudLLMDisabledError(LLMError):
    """The task is assigned to a cloud endpoint while cloud LLMs are disabled."""

    def __init__(self, endpoint: str) -> None:
        super().__init__(f"endpoint {endpoint!r} is a cloud endpoint and cloud LLMs are disabled")
        self.endpoint = endpoint


class LLMNotReadyError(LLMError):
    """Readiness check failed: an endpoint is unreachable or a model is missing."""
