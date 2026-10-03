# ollamail model evaluation

- Started: 2026-10-03T16:42:43+00:00, finished: 2026-10-03T17:27:45+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 logical CPUs, Linux x86_64
- Data set: 200 mails, 60 questions, languages de, en
- Stages: rag
- Embedding model (RAG): granite-embedding-multilingual:278m
- Judge model: -
- Deadline per model call: triage 120 s, todos 120 s, digest 120 s, rag_chat 120 s

## Summary

| Model | RAG Recall@3 | RAG answer (rules) | RAG no answer (rules) | Timeouts |
|---|---|---|---|---|
| `qwen2.5:3b` | 81.2 % | 79.2 % | 83.3 % | 0/111 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | rag_chat | 111 | 0 | 24.0 | 52.1 | 1.3 | 13.9 |

Wall-clock time per model: `qwen2.5:3b` 44.6 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### RAG

60 questions (48 answerable, 12 without answer) over 200 indexed mails (embeddings: `granite-embedding-multilingual:278m`).

- Source found: Recall@1 79.2 %, Recall@3 81.2 %, Recall@5 81.2 %, among all sources given to the model 81.2 %, MRR 0.799
- Expected mail cited: 70.8 %
- Answer correct (rules): 79.2 %; questions without answer handled correctly: 83.3 %
- Time to first token 41.98 s, whole answer 44.58 s on average; errors 0
