"""Hybrid search index: chunks, full text and vectors (docs/ARCHITECTURE.md §4.5).

* ``chunking``: mail text and attachment text → overlapping chunks with header context.
* ``extract``: attachment text (PDF, DOCX, TXT, HTML) in a separate, resource-limited process.
* ``embedder``: batched, throttled embeddings through the LLM gateway.
* ``access``: the one place that decides which mailboxes a user may search.
* ``service``: write the index of a message, fill in embeddings, ``search()`` with
  Reciprocal Rank Fusion of full-text and vector results.
* ``tasks``: the ``index`` processing step and the embedding maintenance job.
* ``cli``: ``python -m app.cli search ...`` (status, resize after a dimension change).
"""
