"""Instructions for JSON answers, added to every structured request.

Also the fallback for endpoints without JSON-schema support (``structured_output=prompt``).
"""

from app.ai.prompts.base import PromptTemplate, registry

STRUCTURED_OUTPUT = registry.register(
    PromptTemplate(
        name="structured_output",
        version=1,
        system={
            "en": (
                "Answer only with a single JSON object that matches this JSON schema. "
                "No explanations, no Markdown.\n\nJSON schema:\n$schema"
            ),
            "de": (
                "Antworte ausschließlich mit einem einzigen JSON-Objekt, das diesem "
                "JSON-Schema entspricht. Keine Erklärungen, kein Markdown.\n\nJSON-Schema:\n$schema"
            ),
        },
    )
)

STRUCTURED_OUTPUT_RETRY = registry.register(
    PromptTemplate(
        name="structured_output_retry",
        version=1,
        system={
            "en": (
                "Your previous answer was not valid JSON for the schema ($errors). "
                "Answer again with only the corrected JSON object."
            ),
            "de": (
                "Deine vorherige Antwort war kein gültiges JSON für das Schema ($errors). "
                "Antworte erneut nur mit dem korrigierten JSON-Objekt."
            ),
        },
    )
)
