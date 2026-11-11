# Social Agent: your Instagram + TikTok manager

Send a photo or video from your phone with a few words of instruction. The agent looks at it, decides what to
post and where, edits it, writes captions and hashtags, schedules it into the right slot, posts it, and tells you
what happened. It also researches trends, keeps your feed balanced, and keeps your Highlights organised.

```
 phone ──► Telegram bot ─┐                                     ┌─► Instagram (photo/carousel/Reel/Story)
 (or iOS/Android         ├─► Claude plans ─► ffmpeg edits ─► scheduler ─┤
  Shortcut → HTTP) ──────┘   (sees your media, trends,        (undo window,   └─► TikTok (video/photo/draft)
                              brand voice, feed state)         approval gate)
```

Everything defaults to **DRY RUN**: until you switch it off, nothing is ever posted.

---

## 1. What it can and can't do (read this first)

| You asked for | Reality |
|---|---|
| Post photos, carousels, Reels, Stories to Instagram | ✅ Official Graph API. Needs a **Business or Creator** account. |
| Highlights | ❌ **Instagram has no API to create Highlights.** The agent tags every Story with a collection and sends you a checklist of exactly which Stories to tap into which Highlight (`/highlights`). |
| Post to TikTok | ⚠️ Official Content Posting API. **Until TikTok audits your app, every direct post is forced private.** Alternative: `TIKTOK_MODE=inbox` uploads a *draft* you publish with one tap. |
| Trending sounds | ❌ Can't be attached by any API. The agent names trends/formats and tells you when to add a sound in-app (or use TikTok inbox mode). |
| Use CapCut / Jianying | ❌ No public API, and I can't drive your phone apps. ffmpeg does the editing headlessly (9:16 blur-fill, trims, hook text, Chinese text OK). **Optional, experimental:** if the agent runs on the same computer as desktop CapCut/Jianying it can write a draft project to open and polish. |
| Research trends | ✅ Claude + web search once a day (incl. Douyin/Xiaohongshu signals that tend to reach TikTok/Reels first). |
| "Refer to my TikTok / Douyin" | ✅ Via things you give it: links (`/inspo <link>`), **screenshots** (it reads Chinese text and extracts the format/hook), or your TikTok "Download your data" export. It does **not** log into your accounts or scrape: that risks bans and breaks the platforms' terms. |
| Post while I'm out | ✅ The agent runs as an always-on service (see §3). A 2-minute **undo window** applies to "post now"; flagged content waits for your approval. |

### Verification status (what I checked vs. what you must check on first run)

| Item | Status |
|---|---|
| Instagram: container → status → publish flow; `media_type` `STORIES` / `REELS` / `CAROUSEL`; a `content_publishing_limit` endpoint exists | ✅ confirmed against Meta docs (via search) |
| Instagram media must be on a public URL | ⚠️ long-standing documented behaviour, not re-confirmed in this build |
| Instagram `status_code` values, quota field names, `/me` fields, token-refresh endpoint | ⚠️ written from memory; `socialagent selfcheck` exercises the read-only ones |
| "100 API posts / 24 h" | ⚠️ third-party figure only, so the agent reads your **real** quota from the API instead |
| TikTok: unaudited apps are private-only; inbox endpoint; photo endpoint + `post_mode`; the error codes `unaudited_client_can_only_post_to_private_accounts` and `url_ownership_unverified` | ✅ confirmed against TikTok docs (via search) |
| TikTok chunk-size rules, request field names, status values, token endpoint | ⚠️ from memory: test with a private post first |
| Whether TikTok inbox mode avoids the audit requirement | ⚠️ not confirmed |
| TikTok oEmbed works without login | ⚠️ docs show a plain GET; confirm with `/inspo <link>` |
| Telegram bots can download ≤ 20 MB; a local Bot API server lifts it | ✅ |
| Claude request shapes (structured output, effort, refusal fallbacks, web search + `pause_turn`) | ✅ matched to the SDK docs and the installed SDK's signatures · ❌ **never called live** (no API key existed in the build environment) |
| CapCut/Jianying draft calls | ✅ generated a real draft with `pyJianYingDraft` 0.3.1 · ⚠️ not opened in the actual app |
| Dockerfile / compose | ⚠️ written, not built |
| Windows behaviour (UTF-8 BOM / UTF-16 / CRLF config files, non-UTF-8 ffmpeg output, `FFMPEG_BIN`, Windows font paths, denied symlinks, no POSIX signals, tokens kept out of logs) | ✅ each simulated and covered by tests on Linux · ❌ **never run on real Windows** |
| `deploy/windows/install.ps1`, the Caddy and scheduled-task steps, AWS security-group advice | ⚠️ written without a Windows machine: run `selfcheck` and read the script before trusting it |
| All logic, safety rules, scheduling, publishers (against mock servers), channels, CLI | ✅ **230+ automated tests** (`python -m pytest -q`) |

**First-run discipline:** `selfcheck` → dry-run with a real photo → one *private* TikTok → one Story/post on a
throwaway Instagram account → only then set `DRY_RUN=false` on your real accounts.

---

## 2. Quick start (dry run on your own machine)

```bash
cd social-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                 # add ,capcut for the experimental draft export
sudo apt install ffmpeg fonts-noto-cjk  # macOS: brew install ffmpeg   (fonts-noto-cjk = Chinese text)
cp .env.example .env                    # then edit it: at minimum ANTHROPIC_API_KEY
python -m socialagent selfcheck --claude
python -m socialagent submit ~/Pictures/cafe.jpg -i "story only, food highlight"   # prints the plan, posts nothing
python -m pytest -q                     # optional: run the test-suite
```

Edit `config/brand.yaml` (voice, content pillars, hashtags, posting times, Highlight collections). It is
re-read on every submission, so changes apply immediately. You can also just tell Claude what to change.

---

## 3. Going live: setup guides

### 3.1 Telegram (how you talk to it)
1. In Telegram, message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Start the agent (`python -m socialagent run`), message your bot `/start`. It replies with **your user id**.
3. Put that in `OWNER_TELEGRAM_ID` and restart. From then on the bot obeys only you.

### 3.2 Hosting on an AWS Windows Server (step by step)
The scheduler, the bot and the public media URLs need an always-on process with a **public HTTPS** address
(`PUBLIC_BASE_URL`): Instagram downloads your media from it. A Windows Server on AWS does the job; Docker is not needed.
Telegram uses outbound polling, so the *only* thing that must be reachable from the internet is the HTTPS address.
(On Linux or elsewhere: a VPS with Caddy, Fly.io/Railway/Render, or `docker compose up -d` also work.)

**You need:** the running instance (Desktop Experience edition; 2 vCPU / 4 GB RAM is a sensible start, though I have not
benchmarked it: ffmpeg encoding is the only heavy step), an **Elastic IP**, and a **domain name** (any registrar). A domain
gives a stable HTTPS address (needed for Instagram) and is required to verify the media domain for TikTok photo posts.

1. **AWS console.** Attach an Elastic IP (otherwise the public IP changes when the instance stops). Security group
   inbound: TCP **80 and 443** from anywhere (certificate issuance needs 80), RDP **3389 only from your own IP**. Do
   **not** open 8080.
2. **DNS.** Create an `A` record `agent.yourdomain.com` → the Elastic IP.
3. **RDP in** and install (all official downloads; versions change, so take the current ones):
   - **Python 3.12** from python.org (tick *Add python.exe to PATH*, install for all users).
   - **ffmpeg**: `choco install ffmpeg` if you use Chocolatey, or unzip a Windows build to `C:\ffmpeg`. Then set
     `FFMPEG_BIN=C:\ffmpeg\bin\ffmpeg.exe` in `.env`: the service runs as SYSTEM, which may not see your PATH.
   - **Caddy** (single `caddy.exe` from caddyserver.com) into `C:\caddy\`.
   - **Git** (optional, for easy updates).
4. **Get the code onto the server:** `git clone -b <branch> <your-repo-url> C:\apps\repo` (for a private repo use a
   read-only GitHub token; never paste tokens into chats), or zip the `social-agent` folder and copy it over RDP.
5. **Configure.** In an **elevated PowerShell**: `cd C:\apps\repo\social-agent; .\deploy\windows\install.ps1`. First run creates
   the virtual environment and a locked-down `.env` (only Administrators/SYSTEM can read it) and stops. Edit it
   (`notepad .env`): set at least `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID`, `TIMEZONE`,
   `FFMPEG_BIN`, `HTTP_HOST=127.0.0.1`, `PUBLIC_BASE_URL=https://agent.yourdomain.com`, and a random `API_TOKEN`
   (generate: `$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b)`).
   Notepad is fine: the agent also accepts UTF-8 with BOM and PowerShell's UTF-16.
6. **Start it.** Run the script again: it runs `selfcheck`, then registers a start-at-boot task **SocialAgent** that restarts
   itself if it crashes. It stays in dry-run until you set `DRY_RUN=false`.
7. **HTTPS.** Copy `deploy\windows\Caddyfile` to `C:\caddy\`, put your hostname in it, allow ports 80/443 in Windows
   Firewall, then register Caddy to start at boot:
   ```powershell
   $a = New-ScheduledTaskAction -Execute "C:\caddy\caddy.exe" -Argument "run --config C:\caddy\Caddyfile" -WorkingDirectory "C:\caddy"
   $t = New-ScheduledTaskTrigger -AtStartup
   $s = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
   Register-ScheduledTask -TaskName Caddy -Action $a -Trigger $t -Settings $s -User SYSTEM -RunLevel Highest
   Start-ScheduledTask -TaskName Caddy
   ```
8. **Check from your phone's browser:** `https://agent.yourdomain.com/healthz` must say `ok`. Then message your bot.

**Day-to-day on the server**
- Logs: `Get-Content C:\apps\repo\social-agent\data\agent.log -Tail 50 -Wait` (tokens are never written to it).
- Restart: `Stop-ScheduledTask SocialAgent; Start-ScheduledTask SocialAgent` (do it when the queue is idle; a post caught
  mid-publish is flagged for your review after restart, never silently re-sent).
- Update: `git pull; .\.venv\Scripts\python.exe -m pip install -e .` then restart the task.
- Backups: the whole state is `social-agent\data\` (database + media) and `.env`; take EBS snapshots or copy `agent.db`.
- Security: keep RDP restricted to your IP (or use AWS Systems Manager Session Manager), keep Windows Update on, rotate
  tokens if the server is ever shared.
- Leave `JIANYING_DRAFTS_DIR` empty: CapCut drafts would land on the server, and you'd have to RDP in to open them.

### 3.3 Instagram
1. Switch the account to **Business or Creator** (Settings → Account type).
2. Create an app at developers.facebook.com and add the Instagram API **with Instagram Login** product; add your
   account to the app and generate an access token with the content-publishing permission (menu and permission
   names change: follow Meta's current guide).
3. Put the numeric account id and token in `IG_USER_ID` / `IG_ACCESS_TOKEN`. The agent refreshes the 60-day token weekly.
4. `python -m socialagent selfcheck` should print `connected as @you` and your publishing quota.

### 3.4 TikTok
1. Create an app at developers.tiktok.com, add **Content Posting API** and **Login Kit**, request `video.publish`
   (direct) and/or `video.upload` (inbox draft), add a redirect URI.
2. For **photo posts** TikTok pulls images from your URL, so verify the domain of `PUBLIC_BASE_URL` in the portal's URL properties (you need your own domain).
3. Put `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` in `.env`, then:
   `python -m socialagent auth tiktok-url --redirect https://your.site/cb` → approve → copy `code=` from the redirect →
   `python -m socialagent auth tiktok-exchange --code <code> --redirect https://your.site/cb`.
4. Leave `TIKTOK_PRIVACY=SELF_ONLY` until TikTok has audited the app (apply in the portal; timing varies: TikTok's app-review FAQ quotes several days to two weeks, third-party reports say longer). Until then use `TIKTOK_MODE=inbox` if you want to publish publicly yourself.

### 3.5 Switching on
Run through `selfcheck`, then set `DRY_RUN=false`. Consider `REQUIRE_APPROVAL=true` for the first week: every plan
then waits for your **✅ Approve** tap.

---

## 4. Using it

**Send media (caption = instruction):**
- (photo) `story only, travel highlight`
- (video) `post tonight at 8pm, funny tone, 抖音 style hook`
- (3 photos) `carousel, caption: Sunday reset`
- (video) `TikTok only, cut the first 3 seconds`
- no instruction at all → the agent decides platform, format, caption and timing

Albums and "photo, then a text a few seconds later" become one submission. For full quality send photos/videos as **File** (Telegram compresses normal photos). You get the plan back with
**Cancel / Approve** buttons and the post ids.

**Telegram commands:** `/queue` `/trends` (`/trends refresh`) `/review` `/highlights` `/highlights_done [name]`
`/cancel <id>` `/approve <id>` `/retry <id>` `/inspo <link> [note]` `/status`. Any other text is answered as chat.

**Inspiration:** send a screenshot with the caption `inspo` (works for Douyin / Xiaohongshu, Chinese text is
translated), `/inspo <TikTok link>`, or your TikTok data export file with the caption `inspo export`.

**Big videos (Telegram's 20 MB cap):** use the HTTP endpoint from an iOS Shortcut ("Get Contents of URL": POST,
Request Body = Form, fields `file` = File and `instruction` = Text, header `Authorization: Bearer <API_TOKEN>`),
an Android app such as HTTP Shortcuts, or curl:

```bash
curl -H "Authorization: Bearer $API_TOKEN" -F "file=@clip.mp4" -F "instruction=TikTok only" https://agent.example.com/api/submit
```

(or set `TELEGRAM_API_BASE` to a local Bot API server, see `docker-compose.yml`).

**Managing it with Claude Code:** `.claude/agents/social-media-manager.md` defines the manager agent, which drives
the same CLI (`submit`, `queue`, `trends`, `review`, `selfcheck`, …) and edits `brand.yaml` on your behalf.

---

## 5. How it organises your feed

- **Content pillars:** each piece is tagged with a pillar from `brand.yaml`; the planner is told which pillars are
  under-represented and varies formats so the grid doesn't repeat itself.
- **Slots:** posts go into your posting windows, respecting a minimum gap and a daily cap per platform. Stories go out
  soon after approval; "post now" waits the undo window.
- **Per-platform voice:** native captions, not copy-paste; bilingual if `languages` says so.
- **Highlights:** Stories are tagged to a collection; the weekly review (Sunday evening) and `/highlights` list what to add.
- **Trends:** refreshed daily; used only when they genuinely fit, and named in the plan so you can see why.

## 6. Safety design

DRY_RUN default · owner-only bot · undo window · optional approval + automatic approval for risk flags
(minors, other people, copyright, location, sensitive…) · atomic claim so a post can never publish twice ·
**no automatic retry after an ambiguous failure** (could double-post) · crash recovery marks interrupted posts for review ·
Claude only *proposes*, plain code validates what each API can publish · web/trend/inspiration text is treated as
untrusted data · secrets and data are git-ignored · official APIs only.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `Instagram rejected the access token` | Generate a new token, update `IG_ACCESS_TOKEN` |
| `could not process the media` | Check `PUBLIC_BASE_URL` is public HTTPS and the file opens in a browser |
| TikTok "audit" error | See §3.4: keep `SELF_ONLY` or use inbox mode until audited |
| TikTok `url_ownership_unverified` | Verify the domain of `PUBLIC_BASE_URL` in the TikTok portal (photo posts) |
| On-screen text missing | `selfcheck` shows font status; Chinese needs a CJK font (Linux: `fonts-noto-cjk`; Windows Server may lack Microsoft YaHei: install Noto Sans CJK and set `FONT_PATH`) |
| `ffmpeg ... is not installed or not reachable` | Set `FFMPEG_BIN` to the full path of `ffmpeg.exe` (the service account may not have your PATH) |
| "live web search wasn't available" | Trends fall back to evergreen formats; enable web search for your Anthropic org |
| `File is too big` in Telegram | §4 big-video options |
| HEIC photo rejected | Send as JPEG/PNG (iOS: Shortcut → Convert Image) |

## 8. Cost and privacy
Each submission sends your media (downsized) and context to the Claude API; rough guess 5-15 US cents on the default
model (`claude-opus-5-5`); set `CLAUDE_MODEL=claude-sonnet-5-5` to roughly halve it (an estimate; check your usage).
Trend research runs once a day. Nothing else leaves your machine except what you publish.

## 9. Layout
`src/socialagent/`: `brain.py` (planner + validation) · `llm.py` (Claude wrapper) · `trends.py` · `media.py`
(ffmpeg) · `feed.py` (slots, balance, Highlights) · `pipeline.py` (orchestration + safety) · `scheduler.py` ·
`publishers/` (instagram, tiktok, dry-run) · `channels/` (telegram, http) · `capcut.py` · `cli.py` · `tests/`.
