# ollamail model evaluation

- Started: 2026-10-03T10:30:59+00:00, finished: 2026-10-03T11:12:42+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 logical CPUs, Linux x86_64
- Data set: 200 mails, 60 questions, languages de, en
- Stages: triage, todos
- Embedding model (RAG): -
- Judge model: -
- Deadline per model call: triage 180 s, todos 180 s, digest 360 s, rag_chat 360 s

## Summary

| Model | Triage accuracy | Triage (model only) | Todos precision | Todos recall | Due dates | Timeouts |
|---|---|---|---|---|---|---|
| `qwen2.5:3b` | 69.0 % | 65.4 % | 68.1 % | 78.3 % | 74.5 % | 0/303 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | triage | 179 | 0 | 8.2 | 11.0 | 5.8 | 175.1 |
| `qwen2.5:3b` | todos | 124 | 0 | 8.3 | 19.9 | 6.1 | 167.6 |

Wall-clock time per model: `qwen2.5:3b` 41.7 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`

### Triage

200 mails: accuracy 69.0 % (model only 65.4 %), priority 41.0 %, decided by rules 21/21 correct, errors 0.

Confusion matrix (rows: expected, columns: predicted):

| expected \ predicted | important | action | waiting_for | info | newsletter | notif. | spam |
|---|---|---|---|---|---|---|---|
| important | 12 | 7 | 1 | 8 | 0 | 0 | 0 |
| action | 0 | 37 | 2 | 1 | 0 | 0 | 0 |
| waiting_for | 3 | 8 | 7 | 6 | 0 | 0 | 0 |
| info | 1 | 1 | 2 | 27 | 0 | 1 | 0 |
| newsletter | 0 | 0 | 0 | 1 | 27 | 0 | 0 |
| notif. | 1 | 1 | 0 | 3 | 0 | 23 | 0 |
| spam | 1 | 13 | 0 | 0 | 1 | 0 | 5 |

### Todos

124 mails sent to the model, 60 expected and 69 predicted todos: precision 68.1 %, recall 78.3 %, F1 72.9 %, due date correct for 74.5 % of 47 matched todos. Errors: 0. Skipped by category: 76 mails (0 expected todos).
