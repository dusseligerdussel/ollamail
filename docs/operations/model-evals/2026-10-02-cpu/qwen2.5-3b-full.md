# ollamail model evaluation

- Started: 2026-10-02T20:50:57+00:00, finished: 2026-10-02T23:17:02+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 logical CPUs, Linux x86_64
- Data set: 200 mails, 60 questions, languages de, en
- Stages: triage, todos, digest
- Embedding model (RAG): granite-embedding-multilingual:278m
- Judge model: -
- Deadline per model call: triage 120 s, todos 120 s, digest 120 s, rag_chat 120 s
- Note: Code 720d6f2 (before #133), time limit 120 s per call. RAG stage excluded: the endpoint was stopped on purpose to end the run, see qwen2.5-3b-rag-sample.

## Summary

| Model | Triage accuracy | Triage (model only) | Todos precision | Todos recall | Due dates | Digest: important mails | Timeouts |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | 61.0 % | 56.4 % | 24.4 % | 63.3 % | 52.6 % | 27.9 % | 38/335 (11.3 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | triage | 179 | 0 | 9.5 | 12.3 | 4.6 | 75.3 |
| `qwen2.5:3b` | todos | 124 | 38 | 46.3 | 120.1 | 5.7 | 70.0 |
| `qwen2.5:3b` | digest | 32 | 0 | 40.2 | 56.6 | 5.3 | 26.2 |

Wall-clock time per model: `qwen2.5:3b` 145.4 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`

### Triage

200 mails: accuracy 61.0 % (model only 56.4 %), priority 58.0 %, decided by rules 21/21 correct, errors 0.

Confusion matrix (rows: expected, columns: predicted):

| expected \ predicted | important | action | waiting_for | info | newsletter | notif. | spam |
|---|---|---|---|---|---|---|---|
| important | 13 | 4 | 0 | 7 | 0 | 4 | 0 |
| action | 2 | 30 | 6 | 2 | 0 | 0 | 0 |
| waiting_for | 2 | 4 | 1 | 13 | 0 | 4 | 0 |
| info | 1 | 1 | 0 | 24 | 0 | 6 | 0 |
| newsletter | 0 | 0 | 0 | 3 | 24 | 1 | 0 |
| notif. | 0 | 0 | 0 | 1 | 0 | 27 | 0 |
| spam | 7 | 3 | 0 | 1 | 1 | 5 | 3 |

### Todos

124 mails sent to the model, 60 expected and 156 predicted todos: precision 24.4 %, recall 63.3 %, F1 35.2 %, due date correct for 52.6 % of 38 matched todos. Errors: 38. Skipped by category: 76 mails (0 expected todos).

### Digest

10 digests over 124 mails: important mails referenced 27.9 %, deadlines of important mails mentioned 90.2 %, 143.4 words and 128.8 s per digest. Errors: 0.
