<!--
Thanks for the contribution! A short, focused PR is much easier to review
than a long one. If this change is broad, consider splitting it.
-->

## Summary

<!-- One or two sentences. Lead with *why*; the diff already shows *what*. -->

## Backend impact

<!-- Tick all that apply. Drives which CI jobs reviewers should focus on. -->

- [ ] SQLite (default backend)
- [ ] Neo4j (opt-in extra)
- [ ] Both backends (contract / engine / skills / MCP)
- [ ] None — docs, CI, repo hygiene, or tests only

## Test plan

<!--
What did you actually run locally? Pre-checked the rows you executed.
Add more bullets for anything specific to this change (manual MCP call,
CLI invocation, sample script, screenshot, etc.).
-->

- [ ] `uv run ruff format --check . && uv run ruff check .`
- [ ] `uv run pytest -q tests/backends tests/contracts tests/test_protocols.py tests/test_composable.py tests/test_openai_compat_embedder.py tests/test_cli.py`
- [ ] Full suite with Neo4j running (only if this PR touches the Neo4j path): `uv run pytest -v`
- [ ] Manual verification (describe):

## Notes for reviewer

<!--
Open questions, follow-ups deliberately deferred, gotchas, links to
related issues / DDRs / PRs. Leave empty if nothing applies.
-->
