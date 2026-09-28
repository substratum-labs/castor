# One-shot verifier manifest and result fields

`verification_command` is a nonempty JSON array of argument strings. Its first
element is the executable name or path. The verifier passes every element as an
argument; it never interpolates these strings into its fixed preparation script.
The command starts in `/workspace`, a private writable tmpfs containing a copy
of the staged candidate. Thus a relative project path such as `defect.txt`
refers to the candidate copy. The original candidate is also mounted at
`/candidate` read-only and is not modified by verification. For direct
`cargo test` commands, the verifier also requires a complete, untruncated
Cargo summary with at least one passing test; zero discovered tests fail.
Other verification runners must encode their own nonempty discovery check in
the command. Exit zero is only one process observation used by the host verdict.

`verification_timeout_seconds` is optional and accepts an integer from 1 to
86,400. It defaults to 300 seconds in production. The value covers Docker
setup, execution, inspection, and cleanup. `--allow-test-opcodes` uses a
five-second default and permits test-only launcher and timeout overrides.

`verifier_evidence.inspected_profile` is present only when the actual created
container passes the pre-start gate. It records the executed image ID, canonical
candidate source, Docker log driver, PID limit, memory limit, memory-plus-swap
limit, and CPU quota. The gate also checks non-root identity, dropped caps,
privilege restrictions, read-only root, network/PID/IPC isolation, the exact
mount set, scratch tmpfs set, working directory, and fixed preparation command.
The admitted scratch set is `/workspace` (512 MiB), private `/dev/shm`
(64 MiB), and a tiny inaccessible `/root` tmpfs (1 MiB, mode 0500) that masks
image-baked root home contents; no host path is mounted there.
The pinned image reference is
`python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`;
the result records and compares the local image ID actually used.

`terminal_running`, `terminal_oom_killed`, and `terminal_exit_code` are typed
Docker inspect observations. Null means that terminal evidence was unavailable,
never success. A successful result requires `Running=false`, `OOMKilled=false`,
exit code zero agreeing with `docker wait`, bounded saved output, and
`container_removed=true`. `captured_logs` reports saved stdout/stderr byte counts
and truncation at 1 MiB per stream. Docker's own log driver is `none`, so it
does not accumulate unbounded engine-managed logs; live attach supplies the
bounded host capture.
