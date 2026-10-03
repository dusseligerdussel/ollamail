# ollamail model evaluation

- Started: 2026-10-03T09:55:51+00:00, finished: 2026-10-03T10:04:39+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 logical CPUs, Linux x86_64
- Data set: 43 mails, 21 questions, languages de, en
- Stages: triage, todos
- Embedding model (RAG): -
- Judge model: -
- Deadline per model call: triage 180 s, todos 180 s, digest 360 s, rag_chat 360 s

## Summary

| Model | Triage accuracy | Triage (model only) | Todos precision | Todos recall | Due dates | Timeouts |
|---|---|---|---|---|---|---|
| `qwen2.5:3b` | 60.5 % | 57.5 % | 92.9 % | 72.2 % | 69.2 % | 0/67 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | triage | 40 | 0 | 8.0 | 10.2 | 5.4 | 137.0 |
| `qwen2.5:3b` | todos | 27 | 0 | 7.8 | 19.0 | 5.8 | 178.5 |

Wall-clock time per model: `qwen2.5:3b` 8.8 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`

### Triage

43 mails: accuracy 60.5 % (model only 57.5 %), priority 51.2 %, decided by rules 3/3 correct, errors 0.

Confusion matrix (rows: expected, columns: predicted):

| expected \ predicted | important | action | waiting_for | info | newsletter | notif. | spam |
|---|---|---|---|---|---|---|---|
| important | 4 | 0 | 2 | 0 | 0 | 0 | 0 |
| action | 1 | 4 | 4 | 0 | 0 | 0 | 0 |
| waiting_for | 0 | 1 | 1 | 1 | 0 | 1 | 0 |
| info | 1 | 0 | 2 | 3 | 0 | 2 | 0 |
| newsletter | 0 | 0 | 0 | 0 | 7 | 0 | 0 |
| notif. | 0 | 0 | 0 | 0 | 0 | 6 | 0 |
| spam | 0 | 1 | 0 | 0 | 0 | 1 | 1 |

### Todos

27 mails sent to the model, 18 expected and 14 predicted todos: precision 92.9 %, recall 72.2 %, F1 81.2 %, due date correct for 69.2 % of 13 matched todos. Errors: 0. Skipped by category: 16 mails (0 expected todos).
