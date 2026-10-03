# ollamail model evaluation

- Started: 2026-10-03T18:59:20+00:00, finished: 2026-10-03T19:41:05+00:00
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
| `qwen2.5:3b` | 83.3 % | 81.2 % | 75.0 % | 0/107 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | rag_chat | 107 | 0 | 23.0 | 50.7 | 1.4 | 15.0 |

Wall-clock time per model: `qwen2.5:3b` 41.3 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### RAG

60 questions (48 answerable, 12 without answer) over 200 indexed mails (embeddings: `granite-embedding-multilingual:278m`).

- Source found: Recall@1 79.2 %, Recall@3 83.3 %, Recall@5 83.3 %, among all sources given to the model 83.3 %, MRR 0.809
- Expected mail cited: 62.5 %
- Answer correct (rules): 81.2 %; questions without answer handled correctly: 75.0 %
- Time to first token 38.91 s, whole answer 41.3 s on average; errors 0
