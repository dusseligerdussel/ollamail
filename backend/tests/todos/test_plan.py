"""``plan_extraction`` and the answer schema without a database: the ``asks_user`` gate
and the per-mail cap."""

from datetime import date

from app.todos.extraction import (
    MAX_TODOS_PER_MAIL,
    ExtractedTodo,
    TodoExtraction,
    plan_extraction,
)

REFERENCE = date(2026, 10, 7)


def _plan(result: TodoExtraction, max_todos: int = 3) -> list[str]:
    plan = plan_extraction(
        result,
        open_titles=["Send report"],
        reference=REFERENCE,
        min_confidence=0.5,
        outgoing=False,
        max_todos=max_todos,
    )
    return [planned.item.title for planned in plan.todos]


def test_asks_user_defaults_to_true_for_older_answers() -> None:
    result = TodoExtraction.model_validate({"todos": [{"title": "Call Kim"}]})

    assert result.asks_user is True
    assert _plan(result) == ["Call Kim"]


def test_no_request_drops_new_todos_but_keeps_done() -> None:
    result = TodoExtraction(
        asks_user=False, todos=[ExtractedTodo(title="Read the minutes", confidence=0.9)], done=[1]
    )

    plan = plan_extraction(
        result,
        open_titles=["Send report"],
        reference=REFERENCE,
        min_confidence=0.5,
        outgoing=False,
    )

    assert (plan.todos, plan.done) == ([], [0])


def test_only_the_most_confident_todos_are_kept() -> None:
    result = TodoExtraction(
        todos=[
            ExtractedTodo(title="Open the attachment", confidence=0.6),
            ExtractedTodo(title="Sign the contract", confidence=0.95),
            ExtractedTodo(title="Read the terms", confidence=0.6),
            ExtractedTodo(title="Send it back", confidence=0.9),
        ]
    )

    # Ties keep the model's order.
    assert _plan(result, max_todos=3) == [
        "Sign the contract",
        "Send it back",
        "Open the attachment",
    ]
    assert _plan(result, max_todos=1) == ["Sign the contract"]


def test_schema_ties_the_todo_list_to_asks_user() -> None:
    schema = TodoExtraction.model_json_schema()

    assert schema["type"] == "object"
    no, yes = schema["anyOf"]
    assert no["properties"]["asks_user"] == {"const": False}
    assert no["properties"]["todos"]["maxItems"] == 0
    assert yes["properties"]["asks_user"] == {"const": True}
    assert (yes["properties"]["todos"]["minItems"], yes["properties"]["todos"]["maxItems"]) == (
        1,
        MAX_TODOS_PER_MAIL,
    )
    assert no["required"] == yes["required"] == ["asks_user", "todos", "done"]


def test_validation_stays_lenient() -> None:
    # Prompt-mode endpoints are not bound by the schema; the plan applies the rules.
    many = [{"title": f"Task {n}", "confidence": 0.9} for n in range(MAX_TODOS_PER_MAIL + 2)]

    result = TodoExtraction.model_validate({"asks_user": False, "todos": many})

    assert len(result.todos) == MAX_TODOS_PER_MAIL + 2
    assert _plan(result) == []
