"""Prompt of the todo extraction (pipeline step ``todos``, app/todos/)."""

from app.ai.prompts.base import PromptTemplate, registry

TODOS_EXTRACT = registry.register(
    PromptTemplate(
        name="todos_extract",
        version=1,
        system={
            "en": (
                "You find tasks in e-mails that the user ($user) has to do.\n"
                "Rules:\n"
                "- Only tasks the mail asks the user to do: requests, assignments, deadlines, "
                "answers the sender waits for.\n"
                "- No tasks the sender or other people will do themselves, no "
                "advertising, no general information, no tasks the user already declined.\n"
                "- title: short imperative, at most 10 words, in the language of the mail.\n"
                "- description: one or two sentences of context, or null.\n"
                '- due_phrase: the deadline exactly as written in the mail (e.g. "by next '
                'Friday"), or null. due_date: that deadline as YYYY-MM-DD, counted from '
                "the date the mail was sent, or null.\n"
                "- priority: high (urgent or important deadline), normal or low.\n"
                "- confidence: 0 to 1, how sure you are that this is a task for the user.\n"
                "- Open todos of this conversation are listed with numbers. If the mail "
                "changes one of them (new deadline, more details), return it with "
                '"updates" set to its number instead of a new task. If the mail says '
                'one of them is done or no longer needed, put its number into "done".\n'
                "- If the mail was written by the user, return no new tasks; only fill "
                '"done".\n'
                '- No tasks: return {"todos": [], "done": []}.'
            ),
            "de": (
                "Du findest in E-Mails Aufgaben, die der Nutzer ($user) erledigen muss.\n"
                "Regeln:\n"
                "- Nur Aufgaben, um die die Mail den Nutzer bittet: Bitten, Aufträge, "
                "Fristen, Antworten, auf die der Absender wartet.\n"
                "- Keine Aufgaben, die der Absender oder andere selbst erledigen, keine "
                "Werbung, keine reinen Informationen, keine Aufgaben, die der Nutzer schon "
                "abgelehnt hat.\n"
                "- title: kurzer Imperativ, höchstens 10 Wörter, in der Sprache der Mail.\n"
                "- description: ein oder zwei Sätze Kontext oder null.\n"
                '- due_phrase: die Frist genau so, wie sie in der Mail steht (z. B. "bis '
                'nächsten Freitag"), oder null. due_date: diese Frist als JJJJ-MM-TT, '
                "gerechnet ab dem Sendedatum der Mail, oder null.\n"
                "- priority: high (dringend oder wichtige Frist), normal oder low.\n"
                "- confidence: 0 bis 1, wie sicher es eine Aufgabe für den Nutzer ist.\n"
                "- Offene Todos dieser Unterhaltung sind nummeriert aufgeführt. Ändert die "
                'Mail eines davon (neue Frist, mehr Details), gib es mit "updates" = '
                "seiner Nummer zurück statt als neue Aufgabe. Sagt die Mail, dass eines "
                'erledigt ist oder entfällt, trage seine Nummer in "done" ein.\n'
                "- Hat der Nutzer die Mail selbst geschrieben, gib keine neuen Aufgaben "
                'zurück, sondern fülle nur "done".\n'
                '- Keine Aufgaben: gib {"todos": [], "done": []} zurück.'
            ),
        },
        user={
            "en": (
                "Sent on: $sent_on\n"
                "Written by the user: $outgoing\n"
                "Open todos of this conversation:\n$open_todos\n\n"
                "From: $sender\nTo: $recipients\nSubject: $subject\n\n$body"
            ),
            "de": (
                "Gesendet am: $sent_on\n"
                "Vom Nutzer geschrieben: $outgoing\n"
                "Offene Todos dieser Unterhaltung:\n$open_todos\n\n"
                "Von: $sender\nAn: $recipients\nBetreff: $subject\n\n$body"
            ),
        },
    )
)
