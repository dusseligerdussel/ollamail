# ollamail model evaluation

- Started: 2026-10-03T16:25:57+00:00, finished: 2026-10-03T16:41:54+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.10GHz, 4 logical CPUs, Linux x86_64
- Data set: 200 mails, 60 questions, languages de, en
- Stages: digest
- Embedding model (RAG): -
- Judge model: -
- Deadline per model call: triage 180 s, todos 180 s, digest 360 s, rag_chat 360 s

## Summary

| Model | Digest: important mails | Timeouts |
|---|---|---|
| `qwen2.5:3b` | 91.2 % | 0/32 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `qwen2.5:3b` | digest | 32 | 0 | 29.9 | 42.5 | 7.2 | 36.6 |

Wall-clock time per model: `qwen2.5:3b` 15.9 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `qwen2.5:3b`


### Digest

10 digests over 124 mails: important mails referenced 91.2 %, deadlines of important mails mentioned 82.9 %, 140.8 words and 95.7 s per digest. Errors: 0.
