# ollamail model evaluation

- Started: 2026-10-04T09:27:27+00:00, finished: 2026-10-04T09:49:29+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.10GHz, 4 logical CPUs, Linux x86_64
- Data set: 100 mails, 39 questions, languages de, en
- Stages: todos
- Embedding model (RAG): -
- Judge model: -
- Deadline per model call: triage 180 s, todos 180 s, digest 360 s, rag_chat 360 s

## Summary

| Model | Todos precision | Todos recall | Due dates | Timeouts |
|---|---|---|---|---|
| `qwen2.5:3b` | 19.8 % | 80.8 % | 61.9 % | 0/53 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | todos | 53 | 0 | 24.9 | 86.8 | 9.1 | 64.1 |

Wall-clock time per model: `qwen2.5:3b` 22.0 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### Todos

53 mails sent to the model, 26 expected and 106 predicted todos: precision 19.8 %, recall 80.8 %, F1 31.8 %, due date correct for 61.9 % of 21 matched todos. Errors: 0. Skipped by category: 47 mails (0 expected todos).
