# ollamail model evaluation

- Started: 2026-10-03T18:18:11+00:00, finished: 2026-10-03T18:59:19+00:00
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
| `qwen2.5:3b` | 72.9 % | 72.9 % | 91.7 % | 0/106 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | rag_chat | 106 | 0 | 22.8 | 50.9 | 1.4 | 15.3 |

Wall-clock time per model: `qwen2.5:3b` 40.5 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### RAG

60 questions (48 answerable, 12 without answer) over 200 indexed mails (embeddings: `granite-embedding-multilingual:278m`).

- Source found: Recall@1 68.8 %, Recall@3 72.9 %, Recall@5 72.9 %, among all sources given to the model 72.9 %, MRR 0.705
- Expected mail cited: 58.3 %
- Answer correct (rules): 72.9 %; questions without answer handled correctly: 91.7 %
- Time to first token 38.14 s, whole answer 40.54 s on average; errors 0
