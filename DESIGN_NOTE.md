# Design note

Atlas drafts an HR reply and writes it back as a case note. The happy path works. The failures that matter are the ones that would attach one employee's case to another, report a failed run as a success, or put employee history into a model prompt and an error log.

## Issues

**Critical**

- A missing idempotency key was treated as a real key. The lookup was `WHERE idempotency_key IS NULL`, so the second submission with no key returned the first person's case, including their draft. The UI submits no key unless someone types one. Two employees asking different questions collapsed into one row.
- There was no unique constraint, and the check-then-insert was not atomic. Concurrent requests with the same key each created a case and a case note. Under SQLite that also surfaced as `database is locked`, because the connection had no busy timeout and no WAL.
- A failed step left the case in `running`. The workflow-level retry then found that row and returned it as a normal result with empty outputs. The exception never reached the client. The same key could not be retried, because every later call short-circuited on `running`. Nothing in the process ever set `failed`, so the resume branch was dead code.
- The whole workflow was retried, including the HRIS write. A case note is an append. Retrying it creates a second official-looking note. Model calls are the part that is safe to retry.
- On failure the API printed a traceback and returned `str(exception)` to the caller. Pydantic's default 422 body also echoes the request, which is the transcript. Step errors were stored unsanitized. The model prompt was `json.dumps` of the entire HRIS record, so any future field (and prior case summaries, and the manager's name) would be sent to the provider and could be repeated in a draft.

**High**

- Replay always inserted a new case with `idempotency_key=None` and no foreign key back to the source. Combined with the NULL-key bug, a replay could also collide with an unrelated case. There was no way to answer "which drafts came from this case?"
- Policy retrieval cached the BM25 index by directory path and file count. Editing a policy in place did not change the count, so ranking kept using the old tokens while the snippet text was read fresh. Ties were broken with `hash()`, which changes across processes, so two replays could cite different documents.
- The engine preferred an OpenAI-shaped `raw.choices[0].message.content` over `CompletionResponse.text`. A misconfigured `PROVIDER_TYPE` (and a failed import of the local provider) silently substituted the stub. Artifacts always recorded `model_name: stub`, including when Ollama was the process that actually ran. A bad deploy would ship stub drafts that looked like model output.
- Model JSON was not checked against the summary schema before it was interpolated into the reply. Cited snippets were whatever the model returned, including lines that were not in the retrieved policy.
- An unknown employee id still ran the model and wrote a case note.
- Step attempts were hardcoded to 1. The trace list on the context was never written. Logging was not configured, so the step timing line was dropped. A failed case had no case-level error record.

**Before this would touch a real employee**

- There is no authentication or authorization. `GET /cases/{id}` returns the transcript, the HRIS snapshot, and the draft to anyone who can reach the port. That is a production blocker, not a follow-up.
- The draft note is written automatically. It is now labeled as not sent, but it is still a write to the HRIS with no human approval.
- Startup marks leftover `running` cases as failed. That is correct for one process. A second API process booting would fail a case the first process is still executing.

## Changes

Idempotency is a unique key plus an input hash. A null or blank key does not match other rows. The same key and the same body return the stored case; the same key and a different body return 409. Concurrent inserts lose on the unique index and then wait for the winner instead of starting a second workflow. Waiters that time out get 503 and the case id, not an empty 200.

Only the model call is retried (timeouts, connection failures, 429/5xx, and a reply that is not the required JSON), up to three times. The attempt count is stored on the step. If the step still fails, the case is `failed`, the HTTP body contains the case id, step name, and exception type, and the stored message is redacted against the transcript and HRIS strings. A later request with the same key resumes after the last completed step and does not insert a second note. The note write itself is deduped on `case:{id}:draft_response`.

Replay stores `replay_of_case_id` and the parent case lists its replays. It does not reuse the original idempotency key, so a replay is an explicit new execution rather than a hidden cache hit.

The HRIS tool allowlists what may enter the workflow at all (`department`, `level`, `location`, `tenure`, `employment status`, `manager`, and activity type/date/summary). The model sees a smaller slice: no manager, no prior cases, no field that was not named. An unknown employee gets a deterministic "record not found" draft and no case note. Retrieval keeps a policy only when its score is at least 30% of the best match, so a PTO question does not also receive the harassment policy. A live `llama3.2` run cited that policy before the cutoff. Citations that are not contained in a retrieved snippet are dropped. Summary and reply JSON are validated before they are stored. Prompt-file hashes, provider, model name, retrieved policy names, and a step trace are written onto the case. The engine reads `CompletionResponse.text` only. An unknown provider raises instead of falling back to the stub.

SQLite uses WAL and a 30s busy timeout. Case listing accepts `limit` and `offset` and returns `total`. CORS is limited to the local UI origin. The UI generates an idempotency key when the field is empty and labels the reply as a draft that a person has to review before it is sent. Validation errors no longer include the request body.

## Tradeoffs

- Callers with a duplicate key wait for the in-flight run, up to 90 seconds, instead of getting a 202 immediately. That preserves the synchronous response the UI already expects. The timeout returns 503 with the case id so the client can poll. A queue would be the better long-term shape; I did not want a second protocol in this pass.
- Resume-in-place avoids a second HRIS note. Replay is the way to force a new execution, and it is linked. I did not make "same key after failure" create a brand-new case, because that is what produced duplicate notes.
- The allowlist hides a new HRIS field until someone adds it on purpose. That will surprise a future change that expects "just put the record in the prompt." It is the right default for employee data.
- Dropping citations that are not verbatim substrings also drops reasonable paraphrases. The retrieved snippets are still on the step record for the reviewer. The body can still state a policy the excerpts do not support. I did not add a second model pass to police the body; that check needs an evaluation set, not another prompt.
- Ollama is asked for JSON at temperature 0. Wording will be flatter. Invalid JSON was the more expensive failure.
- Interrupted-case recovery runs at process start. It is the wrong lock once there is more than one worker. I left it in because a single crashed process otherwise leaves a case that no request will finish, and I called that constraint out above.
- Error logs contain the exception type, step, case id, and duration. They do not contain the transcript. Debugging a bad model reply means re-running from the stored inputs in a place that is allowed to see them. That is less convenient than `logger.exception`, and it is the point.

## What I would still do before production

- Authenticate the caller and authorize them to that employee. Record who viewed a case. Until that exists, this service cannot be on a shared network.
- Split "draft" from "send." A person confirms the text. The HRIS note for a sent message is a different write, with its own idempotency key.
- Replace the startup sweep with a lease and heartbeat before running more than one worker.
- Move off SQLite. Put the HRIS write in an outbox so a crash after the model call cannot lose the note or double-send it.
- Keep the prompt and policy bytes next to the hash, so a replay can explain why a draft changed.
- Build a labeled set for leave, accommodation, and harassment cases, and refuse to assert eligibility when the excerpts do not support it.
- Rate-limit, set a retention period, and define how a case is deleted when the employee record is. Add OpenTelemetry with an explicit rule that case text is not a log or span attribute.

## Scaling

The request should accept the case and return the id. A worker pulls it. The idempotency key is the dequeue key, which is the constraint added here. Cap how many model calls are in flight so a spike cannot fan out into the provider or the HRIS. The policy index is already keyed by a content hash and rebuilt when the bytes change; the next step is to publish that index instead of reading markdown from the API process. Pagination is `limit`/`offset` now and should become a cursor once the table is not SQLite. None of that is useful while every case is readable without an identity.
