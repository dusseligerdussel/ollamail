# ollamail model evaluation

- Started: 2026-10-04T08:34:29+00:00, finished: 2026-10-04T09:27:21+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.10GHz, 4 logical CPUs, Linux x86_64
- Data set: 100 mails, 39 questions, languages de, en
- Stages: todos
- Embedding model (RAG): -
- Judge model: -
- Time limit per model call: 300 s

## Summary

| Model | Todos precision | Todos recall | Due dates | Timeouts |
|---|---|---|---|---|
| `qwen2.5:3b` | 20.2 % | 69.2 % | 61.1 % | 9/53 (17.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | todos | 53 | 9 | 59.8 | 300.1 | 8.6 | 90.6 |

Wall-clock time per model: `qwen2.5:3b` 52.9 min.

Timeouts: calls cancelled at the time limit; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### Todos

53 mails sent to the model, 26 expected and 89 predicted todos: precision 20.2 %, recall 69.2 %, F1 31.3 %, due date correct for 61.1 % of 18 matched todos. Errors: 9. Skipped by category: 47 mails (0 expected todos).
