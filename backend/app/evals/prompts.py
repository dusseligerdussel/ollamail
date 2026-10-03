"""Prompt of the optional LLM-as-judge for RAG answers (evaluation only, never used by
features). The judge sees the question, the expected facts and the answer, not mails."""

from app.ai.prompts import PromptTemplate, registry

EVAL_JUDGE = registry.register(
    PromptTemplate(
        name="eval_judge",
        version=1,
        system={
            "en": (
                "You grade answers of an e-mail assistant. You get a question, the facts a "
                "correct answer must contain (or the note that the e-mails contain no "
                "answer) and the assistant's answer.\n"
                "- With facts: the answer is correct if it states all facts, in any "
                "wording or language, and does not contradict them.\n"
                "- Without an answer in the e-mails: the answer is correct if it says that "
                "nothing was found and does not make up an answer.\n"
                "Citation markers like [1] do not matter. Return correct (true or false) "
                "and a short reason."
            ),
        },
        user={
            "en": "Question: $question\n\nExpected: $expected\n\nAnswer:\n<<<\n$answer\n>>>",
        },
    )
)
