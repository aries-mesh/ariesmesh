## What this PR does

<!-- One paragraph describing the change and why it's needed.
     Link the issue it resolves: "Closes #<n>" or "Addresses #<n>". -->

## Layer(s) changed

- [ ] Identity
- [ ] Transport
- [ ] Scheduler
- [ ] Memory
- [ ] Adapters
- [ ] Protocol (continuation / receipt)
- [ ] Daemon (node.py)
- [ ] CLI
- [ ] Docs / tests only

## Checklist

- [ ] `pytest -v` passes locally (all 46 tests green, or a higher count with new tests included).
- [ ] New feature or bug fix has at least one test. Adversarial paths go in `tests/test_security.py`.
- [ ] `ruff check src/ tests/` is clean.
- [ ] Public functions have type hints. `from __future__ import annotations` at the top of any file that needs it.
- [ ] No new `# type: ignore` without an explanatory comment on the same line.
- [ ] No new GPL/AGPL/BSL dependencies (MIT/BSD/Apache-2.0/MPL only).
- [ ] If a wire-format field was added or removed: `CHANGELOG.md` updated and a `DeprecationWarning` exists in the prior form.
- [ ] `TECH_AND_TESTS.md` test count updated if new tests were added.

## Test evidence

```
# Paste the last few lines of pytest -v output here
```

## Notes for reviewers

<!-- Anything non-obvious about the implementation, known edge cases not covered, or follow-up items deferred. -->
