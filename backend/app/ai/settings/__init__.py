"""AI settings in the database (#18): providers, model per task, cloud switch.

The admin API (``router``) writes them; ``DbConfigResolver`` serves them to the LLM
gateway in the API and in the worker. Changes take effect without a restart: every
write sends ``NOTIFY`` and each process drops its cached snapshot (docs/ARCHITECTURE.md
§3.2).
"""
