# ollamail model evaluation

- Started: 2026-10-02T23:17:10+00:00, finished: 2026-10-02T23:41:57+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 logical CPUs, Linux x86_64
- Data set: 200 mails, 24 questions, languages de, en
- Stages: rag
- Embedding model (RAG): granite-embedding-multilingual:278m
- Judge model: -
- Deadline per model call: triage 120 s, todos 120 s, digest 120 s, rag_chat 120 s
- Note: Code ea3a54c (after #133), RAG only, sample of 24 of 60 questions, all 200 mails indexed.

## Summary

| Model | RAG Recall@3 | RAG answer (rules) | RAG no answer (rules) | Timeouts |
|---|---|---|---|---|
| `qwen2.5:3b` | 90.0 % | 90.0 % | 50.0 % | 0/46 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | rag_chat | 46 | 0 | 31.4 | 65.1 | 1.1 | 10.4 |

Wall-clock time per model: `qwen2.5:3b` 24.1 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### RAG

24 questions (20 answerable, 4 without answer) over 200 indexed mails (embeddings: `granite-embedding-multilingual:278m`).

- Source found: Recall@1 85.0 %, Recall@3 90.0 %, Recall@5 90.0 %, among all sources given to the model 90.0 %, MRR 0.875
- Expected mail cited: 65.0 %
- Answer correct (rules): 90.0 %; questions without answer handled correctly: 50.0 %
- Time to first token 56.37 s, whole answer 60.38 s on average; errors 0
