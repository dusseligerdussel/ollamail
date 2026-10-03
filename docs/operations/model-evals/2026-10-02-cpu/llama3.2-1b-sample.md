# ollamail model evaluation

- Started: 2026-10-02T23:41:59+00:00, finished: 2026-10-02T23:56:05+00:00
- Endpoint: ollama `http://localhost:11434`
- Host: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 logical CPUs, Linux x86_64
- Data set: 30 mails, 12 questions, languages de, en
- Stages: triage, todos, digest, rag
- Embedding model (RAG): granite-embedding-multilingual:278m
- Judge model: -
- Deadline per model call: triage 120 s, todos 120 s, digest 120 s, rag_chat 120 s
- Note: Code ea3a54c (after #133), sample of 30 of 200 mails and 12 questions (index of the 30 mails only).

## Summary

| Model | Triage accuracy | Triage (model only) | Todos precision | Todos recall | Due dates | Digest: important mails | RAG Recall@3 | RAG answer (rules) | RAG no answer (rules) | Timeouts |
|---|---|---|---|---|---|---|---|---|---|---|
| `llama3.2:1b` | 13.3 % | 3.7 % | 29.4 % | 76.9 % | 50.0 % | 58.3 % | 75.0 % | 50.0 % | 87.5 % | 0/79 (0.0 %) |

## Speed

| Model | Task | Calls | Timeouts | s/call | p95 s | generated tok/s | processed tok/s |
|---|---|---|---|---|---|---|---|
| `llama3.2:1b` | triage | 27 | 0 | 3.9 | 6.0 | 10.6 | 185.9 |
| `llama3.2:1b` | todos | 13 | 0 | 10.1 | 23.2 | 14.5 | 104.0 |
| `llama3.2:1b` | digest | 18 | 0 | 17.4 | 57.1 | 19.6 | 51.7 |
| `llama3.2:1b` | rag_chat | 21 | 0 | 13.6 | 34.5 | 7.6 | 31.6 |

Wall-clock time per model: `llama3.2:1b` 14.0 min.

Timeouts: calls cancelled at their deadline; they count as failed answers (error) in the stage that made them. Seconds per call include retries of structured output. Generated tok/s: answer tokens per second of call time; processed tok/s: prompt and answer tokens per second of call time. `rag_chat` streams its answers; their token count is estimated from the length (≈ 3 characters per token) and prompt tokens are unknown, so its numbers are rough.

## `llama3.2:1b`

### Triage

30 mails: accuracy 13.3 % (model only 3.7 %), priority 23.3 %, decided by rules 3/3 correct, errors 0.

Confusion matrix (rows: expected, columns: predicted):

| expected \ predicted | important | action | waiting_for | info | newsletter | notif. | spam |
|---|---|---|---|---|---|---|---|
| important | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| action | 0 | 0 | 1 | 7 | 0 | 0 | 0 |
| info | 0 | 0 | 0 | 1 | 0 | 0 | 0 |
| newsletter | 0 | 0 | 0 | 4 | 0 | 0 | 0 |
| notif. | 0 | 0 | 0 | 5 | 0 | 3 | 0 |
| spam | 1 | 0 | 2 | 2 | 0 | 0 | 0 |

### Todos

13 mails sent to the model, 13 expected and 34 predicted todos: precision 29.4 %, recall 76.9 %, F1 42.5 %, due date correct for 50.0 % of 10 matched todos. Errors: 0. Skipped by category: 17 mails (0 expected todos).

### Digest

8 digests over 13 mails: important mails referenced 58.3 %, deadlines of important mails mentioned 12.5 %, 285.2 words and 39.2 s per digest. Errors: 0.

### RAG

12 questions (4 answerable, 8 without answer) over 30 indexed mails (embeddings: `granite-embedding-multilingual:278m`).

- Source found: Recall@1 75.0 %, Recall@3 75.0 %, Recall@5 75.0 %, among all sources given to the model 75.0 %, MRR 0.750
- Expected mail cited: 0.0 %
- Answer correct (rules): 50.0 %; questions without answer handled correctly: 87.5 %
- Time to first token 14.82 s, whole answer 24.31 s on average; errors 0
