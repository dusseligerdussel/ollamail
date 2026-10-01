"""Mail processing pipeline (docs/ARCHITECTURE.md §4.1).

* ``steps``: ``ProcessingStep`` and the ``registry`` feature modules register with.
* ``tasks``: the jobs and ``enqueue_processing``, the entry point for the mail sync.
* ``service``: step state in ``message_processing``, per-mailbox opt-out.
* ``cli``: ``python -m app.cli processing ...`` (reprocess, enable/disable a mailbox).
"""
