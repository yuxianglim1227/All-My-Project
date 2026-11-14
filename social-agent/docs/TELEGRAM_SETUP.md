# Telegram setup: create your bot and link it to the agent

About 10 minutes. Telegram is how you talk to the agent: you share a photo or video to your own private bot, and it
answers with the final media plus a draft card. The bot is the front door of the agent program on your server, not a
separate service (see [ARCHITECTURE.md §4.1](ARCHITECTURE.md)).

**What only you can do:** create the bot (it must belong to your own Telegram account) and create your Claude API key.
**What the agent does for you:** checks both keys live, finds your Telegram id from a message you send, and writes the
configuration. Nobody, including Claude, ever needs to see your tokens: you type them into the setup program on your own
server and they are never printed or logged.

## Before you start

| You need | Notes |
|---|---|
| Telegram on your phone | The same account you will use to send media |
| A Claude API key | Create one at <https://console.anthropic.com> → API keys. Billing is separate from a Claude chat subscription |
| The agent installed on your server | [README §3.2](../README.md), steps 1–4: Python, ffmpeg and the code in place. You do **not** need a domain, Cloudflare or Caddy for Telegram: the bot only makes outbound connections |
| A way to copy text from Telegram to the server | Easiest: do step 1 in **Telegram Desktop or <https://web.telegram.org>** on the PC you use for Remote Desktop, then paste into the RDP window. Avoid emailing or messaging yourself the token |

## Step 1. Create the bot in BotFather

1. In Telegram, search for **@BotFather** (exact spelling; beware lookalikes) and open the chat. Press **Start**.
2. Send `/newbot`.
3. BotFather asks for a **name** (shown in the chat header; anything, e.g. `My Social Manager`).
4. BotFather asks for a **username**. It must be unique and **end in `bot`**, for example `lily_social_manager_bot`.
   If it is taken or invalid, BotFather says so: just try another.
5. BotFather replies with a message containing your **token**: a line like `123456789:AAH…` (digits, a colon, then
   letters/digits/`_`/`-`). Copy it. **Treat it like a password:** whoever has it controls your bot.
6. Optional but recommended: send `/setjoingroups`, pick your bot, choose **Disable**, so nobody can add the bot to a group.
7. Optional: `/setuserpic` and `/setdescription` to make it look nice. You do **not** need `/setcommands`: the agent
   sets the command menu itself every time it starts.

If you ever lose the token: in BotFather send `/mybots` → pick the bot → **API Token** (shows it again) or
**Revoke current token** (issues a new one; the old one stops working).

> Use a **dedicated bot for this agent.** Only one program can read a bot's messages at a time; if you reuse a token that
> another app (for example your baby tracker or travel planner) already uses, the two fight and setup fails with
> "Another program is already reading this bot's messages".

## Step 2. Run the guided setup on the server

Open an **elevated PowerShell** on the server (over Remote Desktop):

```powershell
cd C:\apps\repo\social-agent            # wherever you put the project
.\deploy\windows\install.ps1            # installs, then launches the guided setup
```

If the project is already installed and you only want the questions, run just the wizard:

```powershell
.\.venv\Scripts\python.exe -m socialagent setup
```

What it asks, in order:

| # | It asks | You do |
|---|---|---|
| 1/5 | **Claude API key** (hidden) | Paste it, press Enter. Nothing appears while you paste: that is normal. It is checked live (a wrong key is rejected and you try again, 3 tries) |
| 2/5 | **Bot token** (hidden) | Paste the token from Step 1. It is checked with Telegram; you should see `Connected to your bot @your_bot_name` |
| 3/5 | *"On your phone open https://t.me/your_bot… I'm listening for up to 3 minutes"* | On your phone open that link (or search for your bot), press **Start** or send any message. The wizard prints `A message arrived from <your name> (id …). Is that you? (y/n)`: press Enter for yes. Only you can become the owner: groups, channels and other bots are ignored |
| 4/5 | Time zone, account name, what it is about, voice, default **tone / language / look** | Answer, or press Enter to keep a default. Everything can be changed later in `config\brand.yaml` or the bot's `/settings` |
| 5/5 | Local web port, public web address | Press Enter for the suggested free port (default 8787). Leave the public address blank until you set up Cloudflare/Caddy ([README §3.2 step 7](../README.md)); Telegram does not need it |

When it finishes it writes `.env` (readable only by you/Administrators) and `config\brand.yaml`, and the bot sends you
a message on Telegram: *"✅ Setup complete…"*. `DRY_RUN` stays **on**, so nothing is ever posted yet.

If you do not send a message within 3 minutes, the wizard lets you type your numeric Telegram id instead (message
**@userinfobot** on Telegram to learn it).

## Step 3. Check the link, then start the agent

```powershell
.\.venv\Scripts\python.exe -m socialagent selfcheck --live
```

You want to see `✅ Telegram bot + owner id configured`, `✅ Telegram bot reachable: @your_bot_name` and
`✅ Claude replied: 'OK'` (warnings about Instagram/TikTok are expected until you connect them).

Start it:

- **Quick test (foreground):** `.\.venv\Scripts\python.exe -m socialagent run`, and leave the window open.
- **Permanently:** run `.\deploy\windows\install.ps1` again: it registers a start-at-boot task called `SocialAgent`.

Within a few seconds your phone should show: **"🟢 Social manager online (🧪 DRY RUN). Send me a photo or video and I'll
draft it."**

## Step 4. Your first draft

1. On your phone, open the bot chat and send a photo (or in your gallery: **Share → Telegram → your bot**). For full
   quality send it **as a file**: Telegram compresses normal photos.
2. The bot answers `📥 Got it - one moment…`, then shows the **final, lightly polished photo** and the **draft card**
   under it (caption, hashtags, where it would go, suggested time).
3. Tap around: tone (16 of them, paged), language, look (Natural / Vivid / Bright / Moody / None), 🔍 Compare for a
   before | after image, 🔄 Rewrite, ✏️ My caption.
4. Tap **✅ Approve**. Because no Instagram/TikTok API is connected yet, the bot will send you a **ready-to-post package**
   at the scheduled time (tap **⚡ Now** to get it after the 2-minute undo window) and you post it in the app.
5. Useful commands: `/status` (mode and connections), `/settings`, `/drafts`, `/queue`, `/help`.

## Everyday use

- **Albums:** send several photos/videos at once (or one, then a text a few seconds later): they become one submission.
- **Type hints in the caption** to skip taps: `funny 中文`, `moody`, `story only`, `post tonight at 8pm`, `TikTok only`.
- **Reply to a draft** with a change ("shorter, mention Sunday"), or start the reply with `=` to use your own caption.
- **Videos over 20 MB:** Telegram's cloud bot API cannot download them. Send a compressed video, use the HTTP upload
  shortcut (needs your public address), or run a local Bot API server ([README §4](../README.md)).
- If the server is off, Telegram keeps your messages for up to 24 hours and delivers them when it is back.

## Security

- The agent obeys **only your Telegram id**. Anyone else who finds the bot gets no answer (the attempt is logged).
- The bot's username is public and cannot be hidden; that is fine because of the owner lock.
- If the token leaks: `/mybots` → your bot → **API Token** → **Revoke current token**, then run
  `socialagent setup` again and paste the new one.
- `.env` holds your tokens. It is git-ignored, locked to Administrators/SYSTEM by the installer, and must never be
  pasted into a chat, email or repository.

## Doing it by hand (without the wizard)

1. Put `TELEGRAM_BOT_TOKEN=<token>` in `.env` and start the agent.
2. Message the bot `/start`. While no owner is set it replies with your numeric id.
3. Put it in `OWNER_TELEGRAM_ID=<id>` and restart the agent. From then on only you are obeyed.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Wizard: *"Telegram says that token is wrong"* | Re-copy the token from BotFather (`/mybots` → bot → API Token). No spaces, format `digits:letters` |
| Wizard: *"Another program is already reading this bot's messages"* | The agent is running (stop it: `Stop-ScheduledTask SocialAgent`), or another app uses the same token, or the bot has a webhook. Use a dedicated bot |
| Wizard: *"Couldn't reach Telegram"* | The server needs outbound HTTPS to `api.telegram.org`. Check the firewall / proxy |
| Wizard does not see your message | Open **your** bot (the link it prints) and press **Start**; or type your numeric id when it asks |
| Bot says *"Setup: your Telegram user id is …"* | `OWNER_TELEGRAM_ID` is not set. Put that id in `.env` (or rerun `setup`) and restart |
| Bot never answers | Agent not running (`Get-ScheduledTask SocialAgent`, or check `data\agent.log`); or you are not the owner id in `.env` (the log says "Ignoring message from unauthorized user") |
| No "🟢 online" message | The owner id is missing, or you have never pressed Start on the bot (Telegram does not let a bot message you first) |
| *"File is too big"* | See "Videos over 20 MB" above |
| Messages arrive late | The server was off; Telegram delivers queued messages (up to 24 h) when it returns |

## What was verified (honest status)

| Item | Status |
|---|---|
| Setup wizard: key checks, owner discovery (ignores groups/bots), file writing, no secret printed | ✅ automated tests against a **mock** Telegram server |
| Bot handlers (owner lock, albums, cards, previews) | ✅ tested against fake updates and real `Update` objects |
| Behaviour against the **real** Telegram service and the real Claude API | ❌ never run live: expect small fixes on the first real use |
| BotFather menu wording and the username rules above | ⚠️ written from general knowledge and third-party guides; Telegram's own documentation was not reachable from the build environment. BotFather tells you if something differs |
| `install.ps1` on a real Windows Server | ⚠️ written without a Windows machine |
