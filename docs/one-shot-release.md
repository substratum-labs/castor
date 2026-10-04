# One-shot Castor release operator runbook

The [one-shot release workflow](../.github/workflows/one-shot-release.yml) is
deliberately separate from the PR candidate workflow. It runs only for a
`one-shot-v<version>` tag whose commit is reachable from `main`. It builds four
host binaries and two native Linux variants each of the controller and Pi
carrier. The protected publication job pushes architecture-specific images to
GHCR, records their immutable digests in four new host archives, attests all
eight subjects, verifies the source ref/commit/workflow identity, and opens a
**draft** GitHub Release. Native Linux amd64 and arm64 jobs then download the
actual draft assets, anonymously prepare the GHCR images, and run the installed
fake-provider read/edit/verifier fixture. The workflow never makes the GitHub
Release public and never calls a real model.

## Before a tag is pushed

1. Review and merge the exact tested Castor source and coordination PRs. Check
   the merged `main` CI and record its full commit ID. A PR candidate artifact
   built from GitHub's synthetic merge revision is not a final release asset.
2. In Castor repository settings, create the `castor-one-shot-release`
   environment with Yong as a required reviewer, administrator bypass disabled,
   and a deployment tag policy allowing only `one-shot-v*`. Confirm the actual
   GitHub account for Yong rather than assuming it from another environment.
   This is an external release control and requires Yong's explicit approval.
3. Set the environment-only secret `CASTOR_RELEASE_ARMED` to exactly
   `one-shot-v<version>@<40-character-main-commit>`. A missing or mismatched
   secret fails before any registry push. Do not put the value in a repository
   secret, workflow file, log or PR. Remove or rotate it after the release.
4. After Yong separately authorizes publication, create and push the release
   tag at that exact `main` commit. Wait for the protected environment review.

## Review the draft before making it public

The first public GHCR push is not atomic with GitHub Release publication. If
any later step fails, stop at the draft; do not infer that a partially pushed
image or archive is accepted. Record the exact four GHCR digest references,
four archive SHA-256s, eight verified attestations, workflow run URL, and both
native installed smoke results. Confirm the GHCR packages are publicly
readable: the smoke jobs use a fresh empty Docker config and cannot rely on the
workflow token. An anonymous pull failure means the package visibility needs
operator repair and another reviewed validation, not a silent authenticated
fallback.

On a clean macOS Docker Desktop host, download the **draft** arm64 (and, when
available, amd64) GitHub Release archive, verify its attestation against the
exact source commit/ref/workflow, unpack outside a Castor checkout, run
`castor runtime prepare` anonymously against its embedded GHCR digests, then
run one zero-real-model fixture with the registry unavailable after prepare.
Record result, verifier, exact CID cleanup and pre/post Docker inventory. The
earlier local-registry macOS candidate trial does not prove these published
bytes. A separately approved model-call budget is required before a real
Ollama trial; no inference budget is implied by tag or release approval.

Yong reviews the complete packet before a draft Release is made public. The
public transition is a separate Level-3 action; update the T-389 ledger only
after the actual published artifacts, attestations, and installed runs have
been verified. Preserve failures and partial publication honestly.
