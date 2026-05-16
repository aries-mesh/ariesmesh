# Contributing to Aries Mesh

Thanks for the interest. This project is small enough that direct contact works well — please open an issue before sending a non-trivial PR.

## Getting started

```bash
git clone <repo-url>
cd aries-mesh
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
pytest                           # expect 46 passing
```

If `pytest` is anything other than 46 green, do not start a feature PR — open a bug first.

## Before you contribute

- **Open an issue first.** Describe what you want to do; check whether it overlaps with anything in `CHANGELOG.md` or the deferred items in `docs/ARCHITECTURE.md`.
- **For bugs** include: OS + version, Python version, the Aries Mesh commit SHA, the exact command, and the full traceback or unexpected output.
- **For features** describe the use case — who benefits, what changes for them, and which layer (identity / transport / scheduler / memory / adapters / protocol / CLI) is affected. If you're not sure, propose and we'll figure it out together.

## Code style

- `ruff` with `line-length = 100` (config is in `pyproject.toml`). Run `ruff check src/ tests/` before each commit.
- Type hints required on public functions. Use `from __future__ import annotations` so forward refs work cleanly.
- No `# type: ignore` without a one-line comment explaining why.
- Prefer dataclasses over dicts for structured records. Mirror the existing patterns in `identity/household.py` and `scheduler/router.py`.
- Don't add comments that just restate what the code does. Comments should explain *why* a non-obvious choice was made (e.g. the "use 0x03 not 0x02 as GF(2^8) generator" note in `keys.py`).

## Testing

- Every new feature needs at least one test. Adversarial paths (tampering, replay, expiry, wrong-key) belong in `tests/test_security.py`.
- Every bug fix needs a regression test that fails before the fix and passes after.
- Run `pytest -v` before opening the PR. All tests must pass. If a test you didn't touch breaks, investigate the root cause — don't paper over it.
- Async tests live alongside sync ones. `pytest-asyncio` is configured in auto mode in `pyproject.toml`.

## What we'd especially welcome help with

- **Hardware diversity testing.** Confirm the daemon behaves well on Apple Silicon, various Linux distros, Raspberry Pi, and Android via Termux. Open issues with `aries status` output and any failures.
- **Adapter development** for specialized providers (local OpenAI-compatible servers, vLLM, llama.cpp HTTP, MLX, niche cloud APIs). Keep `LiteLLMAdapter` as the catch-all; add focused adapters only when litellm can't cover the case.
- **Mobile apps** — Android (Kotlin/Compose) and iOS (Swift) ports of the daemon. The wire format is platform-neutral CBOR; a Kotlin/Swift implementation just needs to match the message-type strings in `transport/peer.py`.
- **Distributed inference research.** Tensor parallelism, pipeline parallelism, and KV-cache sharing across a household LAN. See `docs/research/distributed-inference-survey.md` for the open questions.
- **Security audit.** External review of the Shamir 2-of-3 implementation, the UCAN chain-validation logic, and the Argon2id+SecretBox key-at-rest scheme.

## What we won't merge

- Changes that break existing tests without a written discussion in the linked issue.
- Vendor-specific features that compromise the vendor-agnostic architecture (e.g. tightly coupling the scheduler to one provider's quirks).
- New dependencies under non-permissive licenses (GPL/AGPL/BSL). MIT/BSD/Apache-2.0/MPL only.
- Removing the `DeprecationWarning` legacy-grammar path before v0.2 — it's there to give users one release of migration runway.

## Releasing

Tags follow SemVer. Patch releases only carry bug fixes and security hardening. Minor releases can break the wire protocol if accompanied by a migration note in `CHANGELOG.md` and a `DeprecationWarning` in the prior release.
