"""`socialagent setup`: guided, validated first-time setup.

Automated here: both keys are checked LIVE before they are saved, your Telegram user id is found by waiting for you to
message the bot (nothing to look up or copy), the account questions are written into ``config/brand.yaml`` (comments kept)
and everything else into ``.env``.

Not automatable (and never will be by anyone but you): creating the bot in @BotFather with your own Telegram account, and
the Instagram / TikTok developer apps. Secrets are typed into YOUR terminal (hidden), validated, written to ``.env`` and
never echoed or logged.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx
import yaml

from .config import BrandProfile, load_dotenv, read_text_robust
from .enhance import LOOKS, ORIGINAL
from .styles import StyleCatalog
from .timeutil import tz

TELEGRAM_API = "https://api.telegram.org"
_TOKEN_SHAPE = re.compile(r"^\d+:[\w-]+$")  # 123456789:ABC-def_123 (as BotFather prints it)

OK, REJECTED, UNVERIFIED = "ok", "rejected", "unverified"

# Deliberately uncommon: 3000 / 5000 / 8000 / 8080 are what other apps on the same server usually take.
LOCAL_PORT_DEFAULT = 8787
TEMPLATE_PORT = "8080"  # the value in .env.example; a saved 8080 means "never chosen", not "chosen"


class SetupError(RuntimeError):
    """A step failed in a way the owner can act on. The message never contains a secret."""


class TokenRejected(SetupError):
    """Telegram does not accept the bot token."""


@dataclass
class IO:
    ask: Callable[[str, str], str]  # (prompt, default) -> answer; an empty answer returns the default
    secret: Callable[[str], str]  # hidden input
    say: Callable[[str], None]


def default_io() -> IO:
    import getpass

    def ask(prompt: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        return input(f"{prompt}{shown}: ").strip() or default

    return IO(ask=ask, secret=lambda prompt: getpass.getpass(f"{prompt} (hidden, press Enter after pasting): ").strip(), say=print)


def mask(value: str) -> str:
    return f"…{value[-4:]}" if len(value) >= 12 else "(saved)"


def _yes(answer: str) -> bool:
    return answer.strip().lower().startswith("y")


# ---------------------------------------------------------------------------
# Live checks
# ---------------------------------------------------------------------------
def validate_claude_key(key: str) -> tuple[str, str]:
    """(OK | REJECTED | UNVERIFIED, message). Lists models, which consumes no tokens."""
    import anthropic
    try:
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=20).models.list()
    except anthropic.AuthenticationError:
        return REJECTED, "Claude rejected that key (wrong, revoked, or from a different account)."
    except anthropic.PermissionDeniedError:
        return OK, "Claude key accepted (it is restricted, which is fine)."
    except anthropic.APIConnectionError:
        return UNVERIFIED, "Couldn't reach api.anthropic.com to check it (network or firewall?)."
    except Exception as exc:  # unknown failure: say so rather than guess
        return UNVERIFIED, f"Couldn't verify the key ({type(exc).__name__})."
    return OK, "Claude key works."


def _tg(http: httpx.Client, token: str, method: str, **params) -> dict:
    """One Telegram Bot API call. Errors never include the URL (it contains the token)."""
    try:
        resp = http.get(f"{TELEGRAM_API}/bot{token}/{method}", params=params, timeout=float(params.get("timeout", 0)) + 20)
    except httpx.HTTPError:
        raise SetupError("Couldn't reach Telegram (network or firewall?).") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code in (401, 404):  # Telegram answers an unknown token with 401 or 404
        raise TokenRejected("Telegram says that token is wrong. Copy it again from @BotFather (it looks like 123456789:ABC…).")
    if resp.status_code == 409:
        raise SetupError("Another program is already reading this bot's messages (is the agent running? stop it first), "
                         "or the bot has a webhook set.")
    if resp.status_code >= 400 or not body.get("ok"):
        raise SetupError(f"Telegram error: {body.get('description', resp.status_code)}")
    return body


def telegram_get_me(http: httpx.Client, token: str) -> dict:
    return _tg(http, token, "getMe")["result"]


def telegram_wait_for_owner(http: httpx.Client, token: str, io: IO, *, wait_seconds: int = 180,
                            sleep: Callable[[float], None] = time.sleep,
                            now: Callable[[], float] = time.monotonic) -> dict | None:
    """Wait for a private message to the bot; return its sender once they confirm it is them ({id, first_name, username})."""
    deadline, offset = now() + wait_seconds, None
    while now() < deadline:
        params: dict = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        updates = _tg(http, token, "getUpdates", **params).get("result", [])
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            sender = msg.get("from") or {}
            if (msg.get("chat") or {}).get("type") != "private" or sender.get("is_bot") or "id" not in sender:
                continue  # groups, channels and other bots can never become the owner
            who = str(sender.get("first_name") or "someone") + (f" (@{sender['username']})" if sender.get("username") else "")
            if _yes(io.ask(f"A message arrived from {who} (id {sender['id']}). Is that you? (y/n)", "y")):
                _tg(http, token, "getUpdates", offset=offset, timeout=0)  # acknowledge, so the agent never sees it
                return {"id": sender["id"], "first_name": sender.get("first_name", ""), "username": sender.get("username", "")}
        if not updates:
            sleep(1)
    return None


# ---------------------------------------------------------------------------
# Local port for the agent's web server (other apps may already be running on this machine)
# ---------------------------------------------------------------------------
def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something already listens on this port (or the OS will not let us have it).

    Two probes, because on Windows a plain bind() can succeed next to a listener on another address: first try to
    connect (a listener answers), then try to bind (catches reserved or half-closed ports).
    """
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def pick_free_port(preferred: int = LOCAL_PORT_DEFAULT, *, avoid: Iterable[int] = (), attempts: int = 100,
                   in_use: Callable[[int], bool] = port_in_use) -> int:
    """First port at or above `preferred` that is not in `avoid` and not in use."""
    skip = set(avoid)
    for port in range(preferred, min(preferred + attempts, 65536)):
        if port not in skip and not in_use(port):
            return port
    raise SetupError(f"Couldn't find a free port from {preferred} upwards; free one up or set HTTP_PORT in .env yourself.")


# ---------------------------------------------------------------------------
# File writers (pure functions; unit-tested)
# ---------------------------------------------------------------------------
def _env_value(value: str) -> str:
    return f'"{value}"' if re.search(r"[\s#]", value) else value


def upsert_env(text: str, updates: dict[str, str]) -> str:
    """Set KEY=value lines, keeping every comment and unrelated line; append keys that are missing."""
    out = text.replace("\r\n", "\n")
    if out and not out.endswith("\n"):
        out += "\n"
    for key, value in updates.items():
        pattern = re.compile(rf"^({re.escape(key)}=)[^#\n]*?(\s+#.*)?$", re.M)
        if pattern.search(out):
            out = pattern.sub(lambda m: f"{m.group(1)}{_env_value(value)}{m.group(2) or ''}", out, count=1)
        else:
            out += f"{key}={_env_value(value)}\n"
    return out


def _yaml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def apply_brand(text: str, answers: dict[str, str]) -> str:
    """Fill the account answers into brand.yaml without losing its comments. Returns ``text`` unchanged if the result would not parse."""
    out = text.replace("\r\n", "\n")
    for key in ("account_name", "niche", "audience", "region"):
        if answers.get(key):
            out = re.sub(rf'^({key}:\s*)"[^"\n]*"', lambda m: m.group(1) + _yaml_str(answers[key]), out, count=1, flags=re.M)
    if answers.get("voice"):
        out = re.sub(r"^voice: >\n(?:[ \t]+.*\n)+", lambda m: "voice: >\n  " + " ".join(answers["voice"].split()) + "\n", out, count=1, flags=re.M)
    for key in ("tone", "language", "look"):
        if answers.get(key):
            out = re.sub(rf"^(  {key}:[ \t]*)\S+", lambda m: m.group(1) + answers[key], out, count=1, flags=re.M)
    try:
        data = yaml.safe_load(out)
        if not isinstance(data, dict):
            return text
        BrandProfile.from_dict(data)
    except Exception:
        return text
    return out


def _write_private(path: Path, text: str, *, in_place: bool | None = None) -> None:
    """Write the secrets file: owner-only on POSIX, and never half-written.

    POSIX: a temp file created 0600, then an atomic rename. Windows: a rename would replace the file with a new one that
    inherits the folder's (wider) permissions and drop the ACL install.ps1 set, so an existing file is rewritten in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if in_place is None:
        in_place = os.name == "nt" and path.exists()
    if in_place:
        path.write_text(text, encoding="utf-8", newline="\n")
        return
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------
def _pick(io: IO, label: str, options: list[str], default: str) -> str:
    default = default if default in options else options[0]
    io.say(f"  {label}: " + ", ".join(f"{i + 1}={o}" for i, o in enumerate(options)))
    while True:
        answer = io.ask(f"{label} (name or number)", default)
        if answer in options:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        io.say("  Please pick one of the listed options.")


def _collect_secret(io: IO, *, saved: str, keep_prompt: str, ask_prompt: str, check: Callable[[str], tuple[str, str]],
                    shape: Callable[[str], str | None] = lambda s: None, attempts: int = 3) -> str | None:
    """Reuse the saved secret if the owner wants, else ask (hidden) until `check` accepts it. None after `attempts` failures."""
    secret = saved if saved and _yes(io.ask(keep_prompt, "y")) else ""
    for _ in range(attempts):
        if not secret:
            secret = io.secret(ask_prompt).strip()
        problem = shape(secret) if secret else "Nothing was entered."
        verdict, message = (REJECTED, problem) if problem else check(secret)
        if verdict == OK:
            io.say(f"  ✅ {message}")
            return secret
        io.say(f"  ❌ {message}")
        if verdict == UNVERIFIED and _yes(io.ask("  Save it anyway? (y/n)", "n")):
            return secret
        secret = ""
    return None


def run_setup(io: IO, *, env_path: Path, brand_path: Path, example_path: Path | None = None, http: httpx.Client | None = None,
              check_claude: Callable[[str], tuple[str, str]] = validate_claude_key,
              sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
              port_busy: Callable[[int], bool] = port_in_use) -> int:
    http = http or httpx.Client(timeout=40)
    env_path, brand_path = Path(env_path), Path(brand_path)
    say = io.say
    existing: dict[str, str] = {}
    if env_path.exists():
        try:
            load_dotenv(env_path, existing)
            base = read_text_robust(env_path)
        except ValueError as exc:
            say(f"❌ {exc}")
            return 2
    elif example_path and Path(example_path).exists():
        base = read_text_robust(example_path)
    else:
        base = ""
    try:
        brand = BrandProfile.load(brand_path)
    except ValueError as exc:
        say(f"❌ {exc}")
        return 2
    updates: dict[str, str] = {}
    answers: dict[str, str] = {}

    say("\n=== Social Agent setup ===\nEvery key is checked before it is saved. Nothing gets posted: the safety switch DRY_RUN stays as it is.\n")
    try:
        # 1. Claude -----------------------------------------------------------------------------------
        say("1/5  Claude (the AI brain). Create a key at https://console.anthropic.com -> API keys.")
        claude = _collect_secret(io, saved=existing.get("ANTHROPIC_API_KEY", ""),
                                 keep_prompt=f"Keep your saved Claude key ({mask(existing.get('ANTHROPIC_API_KEY', ''))})? (y/n)",
                                 ask_prompt="Paste your Claude API key", check=check_claude)
        if not claude:
            raise SetupError("Couldn't get a working Claude key after 3 tries. Check it at console.anthropic.com and run setup again.")
        updates["ANTHROPIC_API_KEY"] = claude

        # 2. Telegram bot -------------------------------------------------------------------------------
        say("\n2/5  Your Telegram bot (only you can create it; 30 seconds):\n"
            "     Telegram -> search @BotFather -> send /newbot -> pick a name, then a username ending in 'bot'.\n"
            "     BotFather replies with a token like 123456789:ABC… - paste it below.\n"
            "     Tip: send BotFather /setjoingroups and choose Disable, so nobody can add the bot to a group.")
        bot: dict = {}

        def check_token(value: str) -> tuple[str, str]:
            try:
                bot.update(telegram_get_me(http, value))
            except TokenRejected as exc:
                return REJECTED, str(exc)
            return OK, f"Connected to your bot @{bot.get('username', '?')}."

        token = _collect_secret(io, saved=existing.get("TELEGRAM_BOT_TOKEN", ""),
                                keep_prompt=f"Keep your saved bot token ({mask(existing.get('TELEGRAM_BOT_TOKEN', ''))})? (y/n)",
                                ask_prompt="Paste the bot token from BotFather", check=check_token,
                                shape=lambda s: None if _TOKEN_SHAPE.match(s) else "That doesn't look like a bot token (expected 123456789:ABC…, no spaces).")
        if not token:
            raise SetupError("Couldn't connect to the bot after 3 tries.")
        updates["TELEGRAM_BOT_TOKEN"] = token

        # 3. Owner id: found automatically ---------------------------------------------------------------------
        say("\n3/5  Linking the bot to YOU (it obeys nobody else).")
        owner = existing.get("OWNER_TELEGRAM_ID", "")
        if owner.isdigit() and token == existing.get("TELEGRAM_BOT_TOKEN") and _yes(io.ask(f"Keep the saved owner id {owner}? (y/n)", "y")):
            updates["OWNER_TELEGRAM_ID"] = owner
        else:
            say(f"  On your phone open https://t.me/{bot.get('username', '')} , press Start (or send any message). I'm listening for up to 3 minutes…")
            found = telegram_wait_for_owner(http, token, io, sleep=sleep, now=now)
            if found:
                updates["OWNER_TELEGRAM_ID"] = str(found["id"])
                say(f"  ✅ Linked to {found['first_name'] or 'you'}.")
            else:
                say("  I didn't see a message. You can type your numeric Telegram id instead (message @userinfobot on Telegram to learn it).")
                while True:
                    manual = io.ask("Your Telegram user id (digits only)", "")
                    if manual.isdigit():
                        updates["OWNER_TELEGRAM_ID"] = manual
                        break
                    say("  Digits only, please.")

        # 4. About you ---------------------------------------------------------------------------------------
        say("\n4/5  A little about the account (edit config/brand.yaml any time; leave a line blank to keep what is there).")
        while True:
            zone = io.ask("Your time zone (e.g. Asia/Kuala_Lumpur, Europe/London, America/New_York)", existing.get("TIMEZONE", "UTC"))
            try:
                tz(zone)
                break
            except ValueError as exc:
                say(f"  {exc}")
        updates["TIMEZONE"] = zone
        answers.update(
            account_name=io.ask("Account / brand name", ""), niche=io.ask("What the account is about (one line)", ""),
            audience=io.ask("Who it is for (optional)", ""), region=io.ask("Where you / your audience are (optional)", ""),
            voice=io.ask("Your voice in a sentence (optional, e.g. 'warm, witty, never salesy')", ""))
        catalog = StyleCatalog.from_brand(brand.custom_tones)
        answers["tone"] = _pick(io, "Default caption tone", [t.key for t in catalog.tones], brand.default_tone)
        answers["language"] = _pick(io, "Default language", [x.key for x in catalog.languages], brand.default_language)
        answers["look"] = _pick(io, "Default photo/video look", [*LOOKS, ORIGINAL], brand.default_look)

        # 5. Server bits -------------------------------------------------------------------------------------------
        say("\n5/5  Web address and port. Telegram works without any of this; Instagram API posting and big-video uploads need it.\n"
            "     The agent's web server listens on this machine only. Your Cloudflare Tunnel (or Caddy/IIS/nginx) is what\n"
            "     exposes it, so the port just has to be free and different from your other apps (README section 3.2).")
        saved_port = existing.get("HTTP_PORT", "")
        if saved_port.isdigit() and saved_port != TEMPLATE_PORT and not port_busy(int(saved_port)):
            suggested = int(saved_port)
        else:
            suggested = pick_free_port(LOCAL_PORT_DEFAULT, in_use=port_busy)
        while True:
            port = io.ask("Local port for the agent's web server", str(suggested))
            if not (port.isdigit() and 1024 <= int(port) <= 65535):
                say("  Use a number between 1024 and 65535.")
            elif port_busy(int(port)):
                say(f"  Port {port} is already in use on this machine (is the agent itself running? stop it first). Pick another.")
            else:
                break
        updates["HTTP_PORT"] = str(int(port))
        host = existing.get("HTTP_HOST", "")
        updates["HTTP_HOST"] = host if host not in ("", "0.0.0.0") else "127.0.0.1"   # local only: the tunnel/proxy is the way in
        current_url = existing.get("PUBLIC_BASE_URL", "")
        url = io.ask("Public HTTPS address (e.g. https://social.yourdomain.com; blank = later)",
                     "" if (not current_url or "example.com" in current_url) else current_url)
        if url:
            updates["PUBLIC_BASE_URL"] = url.rstrip("/")
        updates["API_TOKEN"] = existing.get("API_TOKEN") or secrets.token_urlsafe(32)
        updates["DRY_RUN"] = existing.get("DRY_RUN") or "true"
    except SetupError as exc:
        say(f"\n❌ {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        say("\nSetup cancelled. Nothing was saved.")
        return 130

    # ---- write ---------------------------------------------------------------------------------------------------------
    _write_private(env_path, upsert_env(base, updates))
    if brand_path.exists() and any(answers.values()):
        brand_path.write_text(apply_brand(read_text_robust(brand_path), answers), encoding="utf-8", newline="\n")
    delivered = False
    try:
        resp = http.get(f"{TELEGRAM_API}/bot{updates['TELEGRAM_BOT_TOKEN']}/sendMessage", params={
            "chat_id": updates["OWNER_TELEGRAM_ID"],
            "text": "✅ Setup complete. Once the agent is running, send me a photo or video and I'll draft the post for your approval."})
        delivered = resp.status_code == 200
    except httpx.HTTPError:
        pass  # a nicety only
    dry = updates["DRY_RUN"].strip().lower() in {"1", "true", "yes", "on", "y"}
    say(f"\n✅ Saved {env_path} (private) and updated {brand_path.name}."
        + (" A confirmation was sent to your Telegram." if delivered else " (No Telegram confirmation was sent; press Start on the bot first.)")
        + "\n\nNext:\n"
          "  1. python -m socialagent selfcheck --live   (checks Claude, Telegram, ffmpeg, fonts)\n"
          "  2. python -m socialagent run                (start the agent; on Windows the installer makes it a background task)\n"
          "  3. On your phone, send the bot a photo.\n"
        + ("     DRY_RUN is ON: drafts are made and the publish step is rehearsed, nothing is posted until you set DRY_RUN=false.\n"
           if dry else "     ⚠️ DRY_RUN is OFF in your .env: approved posts will really publish.\n")
        + f"\nThe agent's web server listens on {updates['HTTP_HOST']}:{updates['HTTP_PORT']} (this machine only). "
          f"Point your tunnel / reverse proxy at http://localhost:{updates['HTTP_PORT']}.")
    return 0


def main_setup(env_file: str) -> int:
    env_path = Path(env_file)
    values: dict[str, str] = {}
    try:
        load_dotenv(env_path, values)
    except ValueError:
        pass  # run_setup reports the unreadable file
    brand = Path(values.get("BRAND_FILE") or os.environ.get("BRAND_FILE") or "config/brand.yaml")
    return run_setup(default_io(), env_path=env_path, brand_path=brand, example_path=env_path.with_name(".env.example"))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_setup(".env"))
