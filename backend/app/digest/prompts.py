"""Prompts of the daily digest: map, condense and reduce.

* ``digest_map``: one sentence per mail plus a deadline or appointment, for a small batch
  of mails (structured output).
* ``digest_condense``: merges notes when there are too many for one context window.
* ``digest_reduce``: writes the spoken summary from the notes (plain text with ``[n]``
  references). An example in the prompt shows the references; an answer without any is
  asked for again once with ``REDUCE_MISSING_REFERENCES`` (#171).
"""

from app.ai.prompts.base import PromptTemplate, registry

DIGEST_MAP = registry.register(
    PromptTemplate(
        name="digest_map",
        version=1,
        system={
            "en": (
                "You prepare notes for a spoken morning briefing about the new e-mails of "
                "the user ($user). For each numbered mail write one short English "
                "sentence (at most 25 words): who wants what, and what the user has to "
                "do, if anything. Write about the mail, do not quote it.\n"
                "- deadline: an appointment or deadline from the mail as written there "
                '(e.g. "Friday 10:00", "by 15 October"), or null.\n'
                '- Return {"items": [{"ref": <number>, "summary": "...", "deadline": '
                "null}]} with exactly one item per mail."
            ),
            "de": (
                "Du bereitest Notizen für eine gesprochene Morgen-Zusammenfassung der "
                "neuen E-Mails des Nutzers ($user) vor. Schreibe zu jeder nummerierten Mail "
                "einen kurzen deutschen Satz (höchstens 25 Wörter): wer will was, und was "
                "der Nutzer gegebenenfalls tun muss. Beschreibe die Mail, zitiere sie nicht.\n"
                "- deadline: ein Termin oder eine Frist aus der Mail, so wie sie dort steht "
                '(z. B. "Freitag 10 Uhr", "bis 15. Oktober"), oder null.\n'
                '- Gib {"items": [{"ref": <Nummer>, "summary": "...", "deadline": null}]} '
                "zurück, mit genau einem Eintrag pro Mail."
            ),
        },
        user={
            "en": "$mails",
            "de": "$mails",
        },
    )
)

DIGEST_CONDENSE = registry.register(
    PromptTemplate(
        name="digest_condense",
        version=1,
        system={
            "en": (
                "Merge these notes about e-mails into at most $count shorter English "
                "notes. Combine notes on the same topic, keep the most important first, "
                "keep deadlines. End every note with the numbers in square brackets of "
                "all notes it covers, e.g. [2, 7]. One note per line, nothing else."
            ),
            "de": (
                "Fasse diese Notizen zu E-Mails zu höchstens $count kürzeren deutschen "
                "Notizen zusammen. Notizen zum selben Thema zusammenlegen, das Wichtigste "
                "zuerst, Fristen behalten. Beende jede Notiz mit den Nummern aller Notizen, "
                "die sie abdeckt, in eckigen Klammern, z. B. [2, 7]. Eine Notiz pro Zeile, "
                "sonst nichts."
            ),
        },
        user={"en": "$notes", "de": "$notes"},
    )
)

DIGEST_REDUCE = registry.register(
    PromptTemplate(
        name="digest_reduce",
        version=2,
        system={
            "en": (
                "Write the main part of a spoken morning briefing about the user's new "
                "e-mails, in English, about $words words, from the numbered notes below. "
                "The notes are ordered by importance.\n"
                "Rules:\n"
                "- Most important first; mention what the user has to do and all "
                "deadlines.\n"
                "- Combine related notes; leave out unimportant details.\n"
                "- Plain, natural sentences for listening: no headings, no lists, no "
                "Markdown, no greeting and no closing (they are added separately).\n"
                "- Required: end every sentence with the numbers of the notes it is based "
                "on in square brackets, like the notes do. A sentence without numbers is "
                "an error.\n"
                "- Only use facts from the notes.\n"
                "\n"
                "Example\n"
                "Notes:\n"
                "Anna Example asks for the budget figures (deadline: Friday). [1]\n"
                "The dentist confirms the appointment on Tuesday 9:00. [2]\n"
                "Ben Sample asks whether the budget meeting can move. [3]\n"
                "Answer:\n"
                "Anna Example needs the budget figures from you by Friday, and Ben Sample "
                "wants to move the budget meeting. [1, 3] Your dentist appointment on "
                "Tuesday at 9:00 is confirmed. [2]"
            ),
            "de": (
                "Schreibe den Hauptteil einer gesprochenen Morgen-Zusammenfassung der neuen "
                "E-Mails des Nutzers, auf Deutsch, etwa $words Wörter, aus den nummerierten "
                "Notizen unten. Die Notizen sind nach Wichtigkeit sortiert.\n"
                "Regeln:\n"
                "- Das Wichtigste zuerst; nenne, was der Nutzer tun muss, und alle Fristen.\n"
                "- Verwandte Notizen zusammenfassen, Unwichtiges weglassen.\n"
                "- Einfache, natürliche Sätze zum Zuhören: keine Überschriften, keine "
                "Listen, kein Markdown, keine Begrüßung und kein Schluss (die kommen "
                "separat).\n"
                "- Pflicht: Beende jeden Satz mit den Nummern der Notizen, auf denen er "
                "beruht, in eckigen Klammern, so wie in den Notizen. Ein Satz ohne Nummern "
                "ist ein Fehler.\n"
                "- Verwende nur Fakten aus den Notizen.\n"
                "\n"
                "Beispiel\n"
                "Notizen:\n"
                "Anna Beispiel bittet um die Budgetzahlen (Frist: Freitag). [1]\n"
                "Die Zahnarztpraxis bestätigt den Termin am Dienstag um 9 Uhr. [2]\n"
                "Ben Muster fragt, ob das Budget-Meeting verschoben werden kann. [3]\n"
                "Antwort:\n"
                "Anna Beispiel braucht bis Freitag die Budgetzahlen von dir, und Ben Muster "
                "möchte das Budget-Meeting verschieben. [1, 3] Dein Zahnarzttermin am "
                "Dienstag um 9 Uhr ist bestätigt. [2]"
            ),
        },
        user={"en": "$notes", "de": "$notes"},
    )
)

# Follow-up message when the reduce answer has no valid reference at all (asked once).
# Part of ``digest_reduce``: bump its version when changing the wording.
REDUCE_MISSING_REFERENCES = {
    "en": (
        "Your answer has no note numbers. Write it again and end every sentence with the "
        "numbers of the notes it is based on in square brackets, e.g. [2] or [3, 5]."
    ),
    "de": (
        "Deiner Antwort fehlen die Nummern der Notizen. Schreibe sie noch einmal und beende "
        "jeden Satz mit den Nummern der Notizen, auf denen er beruht, in eckigen Klammern, "
        "z. B. [2] oder [3, 5]."
    ),
}
