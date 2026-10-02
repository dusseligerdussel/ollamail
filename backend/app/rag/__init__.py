"""Ask your inbox: answers with citations from the hybrid search (docs/ARCHITECTURE.md §4.5).

* ``query``: search query and filters from the question, combined with the UI filters.
* ``rerank``: optional reranking of retrieved chunks (off for the ``cpu`` profile).
* ``citations``: data blocks for the prompt and the filter that keeps only valid citations.
* ``service``: the answer pipeline (``RagService.ask``) and stored conversations.
* ``router``: ``POST /rag/ask`` (SSE) and ``/rag/conversations``.
* ``tasks``: daily purge of conversations past their retention.
"""
