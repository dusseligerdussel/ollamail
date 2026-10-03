# ollamail model evaluation

- Started: 2026-10-03T09:04:39+00:00, finished: 2026-10-03T09:26:02+00:00
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
| `qwen2.5:3b` | 58.1 % | 55.0 % | 25.0 % | 50.0 % | 88.9 % | 2/67 (3.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | triage | 40 | 0 | 7.0 | 8.6 | 6.0 | 101.5 |
| `qwen2.5:3b` | todos | 27 | 2 | 37.1 | 180.1 | 7.5 | 50.7 |

Wall-clock time per model: `qwen2.5:3b` 21.4 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`

### Triage

43 mails: accuracy 58.1 % (model only 55.0 %), priority 60.5 %, decided by rules 3/3 correct, errors 0.

Confusion matrix (rows: expected, columns: predicted):

| expected \ predicted | important | action | waiting_for | info | newsletter | notif. | spam |
|---|---|---|---|---|---|---|---|
| important | 4 | 1 | 0 | 0 | 0 | 1 | 0 |
| action | 1 | 4 | 3 | 1 | 0 | 0 | 0 |
| waiting_for | 0 | 1 | 0 | 3 | 0 | 0 | 0 |
| info | 0 | 0 | 1 | 4 | 0 | 3 | 0 |
| newsletter | 0 | 0 | 0 | 0 | 7 | 0 | 0 |
| notif. | 0 | 0 | 0 | 0 | 0 | 6 | 0 |
| spam | 1 | 1 | 0 | 0 | 0 | 1 | 0 |

### Todos

27 mails sent to the model, 18 expected and 36 predicted todos: precision 25.0 %, recall 50.0 %, F1 33.3 %, due date correct for 88.9 % of 9 matched todos. Errors: 2. Skipped by category: 16 mails (0 expected todos).
