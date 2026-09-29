**REVISE.** Scope A only. This is not whole-task acceptance. The MockModel Pi run (12 pass, zero live calls) and the protected baseline do not exercise this bridge.

Reviewed the pasted `scripts/trusted_slot/real_model.py` only. SHA-256 was not recomputed.

**Yes. A full-path `FileBridgeModel` physical preflight is required before any LIVE release.** The recorded Pi preflight used the old MockModel path. This bridge stops at unit coverage. The live path is UDS frame, `request-*.json`, `HostExchange.poll`, one loopback POST, `response-*.json` or `error-*.json`, then a length-prefixed UDS frame. That path is unmeasured. Use a 127.0.0.1 stub. Do not spend the Boltons Bits qwen budget on the preflight.

## Critical

**1. Durable reservation is wiped on every `HostExchange` construct, so restart issues a fresh 3 POSTs.**  
`__init__` always does `self.ledger = {..., "reservations": []}` and `write_json(..., budget.json)`. Nothing in this module reads `budget.json` back. The pre-HTTP `RESERVED` write is ignored by the next instance. A crash in the reserved-before-POST window, or a second `HostExchange` on the same evidence directory, starts at zero and will call `/api/chat` again for a request that was already reserved, failed, or completed. In-process `poll` replay of an identical file is the only limit that holds.

**2. The 300s cap is a pre-check plus a per-operation socket timeout. Wall clock can pass the task deadline, and the UDS wait has no deadline.**  
`poll` computes `remaining = self.deadline - time.monotonic()` once, then `transport(payload, min(remaining, 300))`. `opener.open(..., timeout=)` and `response.read` set a socket timeout. Each successful recv starts that timer again (status line, headers, then each body chunk). One call can therefore occupy the socket for several multiples of `remaining` while `self.deadline` is already in the past. Nothing closes that socket when the monotonic deadline hits, so the 60s cleanup can be stuck behind the same call.  
`FileBridgeModel.serve` sets `stream.settimeout(300)` and then waits in `time.sleep(0.05)` until a file appears or `stop` is set. That sleep does not touch the socket, so the 300s socket timeout does not end the wait. `close()` joins the thread for 1s and does not close the accepted socket or abort `transport`.

**3. The generation POST is pinned only by the movable tag `qwen3.5:9b`.**  
`validate_model_pin` checks name, `MODEL_DIGEST`, and `OLLAMA_VERSION`, but `HostExchange.poll` and `FileBridgeModel` never call it. `model_request` sends `"model": MODEL`. `native_response` accepts any `message` and never compares the response model to `MODEL_DIGEST`. A tag move between a manual pin and POST, or a skipped pin, still produces a normal observation. The digest constant is not on the call that spends the trial.

## Important

**4. Error files are not UDS frames.**  
On `error-*.json`, or when `stop` is set with no answer, `serve` drops out of the wait and closes the socket with no `>I` length prefix and no error body. The client observes EOF. A higher layer that opens a new `interaction_id` on EOF spends another of the 3 POSTs. Exceptions other than `OSError`, `ValueError`, and `EOFError` kill the daemon `serve` thread with no error file.

**5. Non-canonical content still spends a POST.**  
For `system`, `user`, and `assistant`, content that is neither `str` nor `list` becomes `""`. List blocks whose `type` is not `text` (and not assistant `toolCall`) are dropped. The first system message appends only string `sections` values and drops names, `None`, and removals; later system messages use the update/removal text. Those paths raise only for an unknown role. A stripped prompt is a normal reservation and a normal `/api/chat`.

**6. `done` is unchecked, and ingress to `poll` is any bridge file.**  
A 200 body with `done` absent or false is accepted when `message` and integer usage exist. `poll` also treats every `bridge/request-*.json` as work, and it skips the nonce when `response-*.json` or `error-*.json` already exists. `serve` will frame whatever bytes are already in the answer file. `request_digest` is a self-checksum of the JSON, not a writer check. That is safe only if the engine mount cannot create files in `evidence/bridge`. This module does not show that restriction. `serve` also accepts one connection at a time; a partial frame blocks later accepts until `read_frame` hits its socket timeout.

**7. Body bounds are one-sided, and the response file is consumed on first `exists()`.**  
`transport` rejects a chat body larger than 4 MiB after `read(4MiB+1)`. The outbound `json.dumps(payload)` has no maximum in this module. `validate_model_pin` reads `1MiB+1` and does not reject the over-cap length. `serve` does `answer.exists()` then `read_bytes().strip()` with no commit marker, size, or digest check in this file. A non-atomic `write_json` can frame a torn body. A torn `request-*.json` parse writes a sticky `error-*.json`, and the next `poll` will not read the finished request.

## Tests

Lock these before LIVE. Stub `transport` and the UDS peer. Zero calls to real Ollama.

1. **Restart budget.** Reserve one call, write `budget.json`, construct a second `HostExchange` on that directory, `poll` the same bytes. Assert `transport` count stays 0 and the new ledger still contains the old reservation. Repeat with status `RESERVED` (crash window) and `FAILED`. Four distinct ids still mean exactly three `transport` calls.
2. **Hard deadline.** Stub a body that returns one byte, then sleeps past `deadline_seconds`, then returns another byte. Assert the socket is closed from the monotonic deadline and elapsed time is within a small slack of 300s. Assert `FileBridgeModel` ends the UDS wait with an error frame if no answer file appears by the deadline, including when `close()` runs during the wait. Assert an in-flight POST cannot outlive that deadline into cleanup.
3. **Pin on the POST path.** Wrong `/api/tags` digest or wrong `/api/version`: zero `/api/chat`. Right pin, then a chat JSON whose model identity is not `MODEL` / `MODEL_DIGEST`: error frame, reservation `FAILED`, no success observation. Tag flip between pin and POST: no success observation.
4. **Single attempt.** `transport` raises `HTTPTransportError` once. `poll` again and reconnect the same interaction bytes: still one `transport` call. A 302 from the stub: one connection, no second URL. Output `513`, missing `prompt_eval_count`, bool counts, negative counts: error frame, no answer file, slot consumed.
5. **UDS error frame.** Force `native_response` to raise. The client reads one length-prefixed frame of the error object. `serve` stays alive on `TypeError` from a bad frame.
6. **Canonical golden.** First system message plus a later section update and a `None` removal; assistant `toolCall` blocks; `toolResult` with `toolCallId`; Pi-shaped tools and `{type: function, function: {...}}` tools. Compare Ollama `messages` and `tools` to a Pi-emitted golden transcript. A user content dict, and a non-text block, produce zero `transport` calls.
7. **Sticky files and bounds.** Pre-create `response-<nonce>.json`: the stub must see zero POSTs, and the controller must not treat that file as the model observation unless this process wrote it. Chat body of `4MiB+1`: error, no success frame. Request payload over the chosen maximum: error before POST. Publish the answer by rename, then assert the framed bytes parse as one JSON value.
8. **Physical preflight (stub, not live).** Real controller client, real `FileBridgeModel`, real `HostExchange.poll`, stub bound to `127.0.0.1:11434`. Assert reservation `RESERVED` is on disk before the stub accepts the POST, exactly one POST, proxies empty, and the client decodes the same observation the host wrote. This is a release gate. It is not a substitute for the later live trial, and it must not use the live trial’s 3 calls.

Same-process behavior that already holds in this text: identical interaction bytes replay the stored answer with no second POST; a changed or non-`COMPLETED` duplicate does not POST; the fourth reservation is refused before `transport`; missing and over-budget usage do not publish a success observation; `num_predict` is `min(requested, 512)`; offered-tool and object-argument checks fail closed; the chat URL is `127.0.0.1` with `ProxyHandler({})`. Those do not clear findings 1–3.
