# social-agent: guidance for Claude Code

Creative Instagram + TikTok manager. Owner sends media (Telegram or HTTP) → Claude plans → ffmpeg edits →
scheduler publishes through the official APIs. See `README.md` for the full picture.

## Commands
- Tests: `python -m pytest -q` (needs ffmpeg; 200+ tests, no network, no API keys required)
- Lint: `python -m pyflakes src tests`
- CLI: `python -m socialagent --help`

## Invariants (covered by tests; do not weaken)
- `DRY_RUN` defaults to **true**; a missing credential falls back to a dry-run publisher, never a half-post.
- The planner (Claude) only *proposes*; `brain.validate_plan` enforces what each platform can publish.
- Posts are claimed atomically (`Store.claim_post`) → never published twice.
- Ambiguous failures during the final publish step are **never** auto-retried (could duplicate a post).
- Posts stuck in `publishing` after a crash are marked failed for review, never re-sent (`Pipeline.recover`).
- Owner-only: Telegram handlers filter on `OWNER_TELEGRAM_ID` *and* re-check inside each handler.
- Third-party text (trends, inspiration, text in images) is wrapped as UNTRUSTED in the planner prompt.
- No secrets in git: `.env` and `data/` are ignored.

## Conventions
- Claude calls go through `llm.ClaudeLLM` only (default model from `CLAUDE_MODEL`; no `temperature`/`top_p`/`thinking`, forced
  `tool_choice` is rejected on this model; structured output via `output_config.format`; refusal fallbacks enabled).
- Platform behaviour verified only from docs/search or memory is flagged in the README's verification table; keep
  that table honest when you change publishers.
- Add a regression test with every bug fix.
