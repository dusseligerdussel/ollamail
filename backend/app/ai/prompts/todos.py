"""Prompt of the todo extraction (pipeline step ``todos``, app/todos/).

Version 2 (#158): what a task is and is not, "no task" as the expected common answer,
the yes/no field ``asks_user`` before the list, and two synthetic examples.
"""

from app.ai.prompts.base import PromptTemplate, registry

TODOS_EXTRACT = registry.register(
    PromptTemplate(
        name="todos_extract",
        version=2,
        system={
            "en": (
                "You find tasks in one e-mail that the user ($user) has to do.\n"
                "A task is an action the sender explicitly asks the user to do: answer a "
                "question, send, confirm, decide, approve, pay, sign, fill in, book. It is "
                'usually a direct request or question ("please …", "could you …", '
                '"by Friday").\n'
                "Not a task:\n"
                "- information, news, minutes, announcements, status updates\n"
                "- confirmations of orders, bookings, registrations or appointments\n"
                "- things the sender or other people will do themselves\n"
                "- things the mail says need no action from the user\n"
                "- advertising, offers, newsletters, tips, optional invitations\n"
                "- signatures, disclaimers and quoted earlier e-mails\n"
                "Most e-mails contain no task; then the correct answer is "
                '{"asks_user": false, "todos": [], "done": []}.\n'
                "Fields:\n"
                "- asks_user: true only if the e-mail explicitly asks the user to do "
                "something.\n"
                "- todos: one entry per requested action, usually one, rarely more than "
                "two. Do not split one request into steps. Empty if asks_user is false.\n"
                "- title: short imperative, at most 10 words, in the language of the mail.\n"
                "- description: one sentence of context, or null.\n"
                '- due_phrase: the deadline exactly as written in the mail (e.g. "by next '
                'Friday"), or null. due_date: that deadline as YYYY-MM-DD, counted from '
                "the date the mail was sent, or null.\n"
                "- priority: high (urgent or important deadline), normal or low.\n"
                "- confidence: 0 to 1, how sure you are that the sender asks the user to "
                "do this.\n"
                "- Open todos of this conversation are listed with numbers. If the mail "
                "changes one of them (new deadline, more details), return it with "
                '"updates" set to its number instead of a new task. If the mail says '
                'one of them is done or no longer needed, put its number into "done".\n'
                "- If the mail was written by the user, return no new tasks; only fill "
                '"done".\n'
                "Examples:\n"
                '"Your table for four on Friday at 7 pm is confirmed. See you then!" → '
                '{"asks_user": false, "todos": [], "done": []}\n'
                'Sent on Monday, 2026-03-02: "Could you send me the signed offer by Wednesday? '
                'Thanks, Kim" → '
                '{"asks_user": true, "todos": [{"title": "Send signed offer to Kim", '
                '"description": null, "due_phrase": "by Wednesday", "due_date": '
                '"2026-03-04", "priority": "normal", "confidence": 0.9}], "done": []}'
            ),
            "de": (
                "Du findest in einer E-Mail Aufgaben, die der Nutzer ($user) erledigen "
                "muss.\n"
                "Eine Aufgabe ist eine Handlung, um die der Absender den Nutzer "
                "ausdrücklich bittet: eine Frage beantworten, etwas schicken, bestätigen, "
                "entscheiden, freigeben, zahlen, unterschreiben, ausfüllen, buchen. Meist "
                "eine direkte Bitte oder Frage („bitte …“, „kannst du …“, „bis Freitag“).\n"
                "Keine Aufgabe:\n"
                "- Informationen, Neuigkeiten, Protokolle, Ankündigungen, Statusmeldungen\n"
                "- Bestätigungen von Bestellungen, Buchungen, Anmeldungen oder Terminen\n"
                "- was der Absender oder andere selbst erledigen\n"
                "- was laut Mail vom Nutzer nichts verlangt\n"
                "- Werbung, Angebote, Newsletter, Tipps, freiwillige Einladungen\n"
                "- Signaturen, Disclaimer und zitierte frühere E-Mails\n"
                "Die meisten E-Mails enthalten keine Aufgabe; dann ist die richtige "
                'Antwort {"asks_user": false, "todos": [], "done": []}.\n'
                "Felder:\n"
                "- asks_user: nur true, wenn die E-Mail den Nutzer ausdrücklich um etwas "
                "bittet.\n"
                "- todos: ein Eintrag je erbetener Handlung, meist einer, selten mehr als "
                "zwei. Zerlege eine Bitte nicht in Einzelschritte. Leer, wenn asks_user "
                "false ist.\n"
                "- title: kurzer Imperativ, höchstens 10 Wörter, in der Sprache der Mail.\n"
                "- description: ein Satz Kontext oder null.\n"
                '- due_phrase: die Frist genau so, wie sie in der Mail steht (z. B. "bis '
                'nächsten Freitag"), oder null. due_date: diese Frist als JJJJ-MM-TT, '
                "gerechnet ab dem Sendedatum der Mail, oder null.\n"
                "- priority: high (dringend oder wichtige Frist), normal oder low.\n"
                "- confidence: 0 bis 1, wie sicher der Absender den Nutzer darum bittet.\n"
                "- Offene Todos dieser Unterhaltung sind nummeriert aufgeführt. Ändert die "
                'Mail eines davon (neue Frist, mehr Details), gib es mit "updates" = '
                "seiner Nummer zurück statt als neue Aufgabe. Sagt die Mail, dass eines "
                'erledigt ist oder entfällt, trage seine Nummer in "done" ein.\n'
                "- Hat der Nutzer die Mail selbst geschrieben, gib keine neuen Aufgaben "
                'zurück, sondern fülle nur "done".\n'
                "Beispiele:\n"
                "„Ihr Tisch für vier Personen am Freitag um 19 Uhr ist bestätigt. Bis dann!“ "
                '→ {"asks_user": false, "todos": [], "done": []}\n'
                "Gesendet am Montag, 2026-03-02: „Kannst du mir bis Mittwoch das "
                'unterschriebene Angebot schicken? Danke, Kim“ → {"asks_user": true, '
                '"todos": [{"title": "Unterschriebenes Angebot '
                'an Kim schicken", "description": null, "due_phrase": "bis Mittwoch", '
                '"due_date": "2026-03-04", "priority": "normal", "confidence": 0.9}], '
                '"done": []}'
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
