# social-agent: guidance for Claude Code

Creative Instagram + TikTok manager, **Phase 1 = draft-first**. Owner shares media (Telegram or HTTP) → Claude understands it
once (vision) → writes captions per tone × language (text) → the owner reviews a draft card → Approve → publish via the
official APIs / TikTok inbox / manual hand-off. Read `docs/ARCHITECTURE.md` first, then `README.md`.

## Commands
- Tests: `python -m pytest -q` (needs ffmpeg; 458 tests, no network, no API keys required)
- Lint: `python -m pyflakes src tests`
- CLI: `python -m socialagent --help`

## Invariants (covered by tests; do not weaken)
- `DRY_RUN` defaults to **true**; a missing credential falls back to a dry-run publisher, never a half-post.
- Claude only *proposes*; `brain.validate_plan` / `eligible_targets` enforce what each platform can publish.
- **Only `DraftService.approve` creates posts**: the publish engine never sees an unapproved draft. Risk flags need a 2nd tap.
- Changing tone/language re-runs only the text-only Writer (never the vision call). Channels hold a draft card by `card_ref`
  and edit it in place; persist that handle (a past bug sent a second message instead).
- Media edits are **global light/colour polish only** (`EDIT_LEVEL=enhance` default; `none` = originals): no face/skin retouching,
  reshaping or cropping. `enhance.py` stays in YUV (`lutyuv`; an RGB round trip shifted luma) and strength 0 is an exact identity.
  HDR clips are left as shot. `media.compliant_*` only fix platform rejects.
- **What posts is what was previewed:** `DraftService.approve` publishes the draft's stored masters (`masters_json`) for the
  selected look; changing the look re-renders (cached per look) and updates the masters before anything can be approved.
- Posts are claimed atomically (`Store.claim_post`) → never published twice.
- Ambiguous failures during the final publish step are **never** auto-retried (could duplicate a post).
- Posts stuck in `publishing` after a crash are marked failed for review, never re-sent (`Pipeline.recover`).
- Owner-only: Telegram handlers filter on `OWNER_TELEGRAM_ID` *and* re-check inside each handler.
- Third-party text (trends, inspiration, text in images) is wrapped as UNTRUSTED in the Understand/Write prompts.
- No secrets in git: `.env` and `data/` are ignored. Request URLs carry tokens, so `httpx` logging stays at WARNING.
- Secrets are never printed or logged by `setup.py` (hidden input, last-4 mask, URLs with tokens never in errors); `.env` is
  written owner-only (atomic on POSIX, in place on Windows so the ACL survives). The wizard never turns `DRY_RUN` off.
- **Zero per-call cost by default:** `AI_PROVIDER=bridge`. Automatic AI only with `GEMINI_API_KEY` (free tier) or the
  opt-in paid Claude provider. Never automate the Claude/Gemini consumer apps (terms); the bridge is copy-paste.
- Every AI answer (Gemini or pasted) is parsed with `jsoncheck.extract_json` and validated against the task schema before
  it is stored; invalid replies leave the task answerable and say what is wrong.
- Studio security: every `/studio/api` route except `me`/`login` needs a session; non-GET calls also need `X-Studio: 1`;
  sessions are stored as SHA-256 hashes; strict CSP (no inline script/style, so UI text is set with `textContent`);
  media is served only to a logged-in session; API responses are `no-store`.
- Runs on Windows Server too: config files are read via `config.read_text_robust` (BOM/UTF-16), subprocess output is
  decoded as UTF-8, ffmpeg is located via `FFMPEG_BIN`, and nothing may assume POSIX signals or symlinks.

## Conventions
- Claude calls go through `llm.ClaudeLLM` only (default model from `CLAUDE_MODEL`; no `temperature`/`top_p`/`thinking`, forced
  `tool_choice` is rejected on this model; structured output via `output_config.format`; refusal fallbacks enabled).
- Platform behaviour verified only from docs/search or memory is flagged in the README's verification table; keep
  that table honest when you change publishers.
- Add a regression test with every bug fix.
