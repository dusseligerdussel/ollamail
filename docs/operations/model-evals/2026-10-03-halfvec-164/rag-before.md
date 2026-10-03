# ollamail model evaluation

- Started: 2026-10-03T15:58:19+00:00, finished: 2026-10-03T16:42:34+00:00
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
| `qwen2.5:3b` | 85.4 % | 81.2 % | 91.7 % | 0/109 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | rag_chat | 109 | 0 | 23.9 | 51.4 | 1.3 | 14.2 |

Wall-clock time per model: `qwen2.5:3b` 43.7 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### RAG

60 questions (48 answerable, 12 without answer) over 200 indexed mails (embeddings: `granite-embedding-multilingual:278m`).

- Source found: Recall@1 81.2 %, Recall@3 85.4 %, Recall@5 85.4 %, among all sources given to the model 85.4 %, MRR 0.830
- Expected mail cited: 72.9 %
- Answer correct (rules): 81.2 %; questions without answer handled correctly: 91.7 %
- Time to first token 41.21 s, whole answer 43.69 s on average; errors 0
