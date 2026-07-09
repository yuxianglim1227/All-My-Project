# Baby Tracker

A self-hosted baby tracker: feeding, naps, diapers, meals, growth, daily
routine, weekly meal plan, and a prep checklist — shared live across every
caregiver's phone.

No Claude, no artifact runtime, no build step required to run it. It's a
plain FastAPI + SQLite backend (same pattern as your MT5 dashboard) serving
a vanilla HTML/CSS/JS frontend. One process, one port.

```
babytracker/
├── backend/            FastAPI + SQLite API, serves the frontend too
│   ├── app.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/            Static PWA (installable on any phone, no app store)
│   ├── index.html / styles.css / app.js / config.js
│   ├── manifest.json / service-worker.js
│   └── icons/
├── mobile-wrapper/      Capacitor project — wraps the frontend into a real .apk
│   ├── package.json / capacitor.config.json
│   ├── www/             (copy of frontend, pre-pointed at your server)
│   └── android/         native Android project, ready to open in Android Studio
└── README.md            (this file)
```

---

## 0. Get it into your own GitHub

This project is already a git repo (with a `.gitignore` that keeps secrets,
`venv/`, `node_modules/`, and the SQLite data file out of version control).
Push it to a new repo under your account:

```bash
# from inside the babytracker/ folder
git remote add origin https://github.com/<you>/baby-tracker.git
git branch -M main
git push -u origin main
```

(Create the empty repo first at github.com/new, or with `gh repo create
baby-tracker --private --source=. --push` if you have the GitHub CLI.)

The initial commit was made under a placeholder identity — fix that once,
if you care about commit authorship:
```bash
git commit --amend --reset-author -m "Initial commit"
```

**Deploying from GitHub to your AWS Windows Server** — pull instead of
copying files by hand:
```powershell
cd C:\
git clone https://github.com/<you>/baby-tracker.git babytracker
cd babytracker\backend
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env
```
From then on, updates are just:
```powershell
cd C:\babytracker
git pull
venv\Scripts\pip install -r backend\requirements.txt   # if deps changed
# restart the uvicorn/NSSM service
```

**Want it to redeploy automatically on every push?** That's a GitHub
Actions job with a self-hosted runner installed on the Windows server (so
Actions can reach into your private AWS instance) — happy to set that
workflow file up if you want it; it needs a couple of specifics from your
setup (service name, runner install path) so I didn't want to guess at it.

---

## 1. Run the backend

Works on your AWS Windows Server exactly like your MT5 dashboard does.

```powershell
cd backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env
notepad .env          # set ACCESS_CODE and DB_PATH

venv\Scripts\uvicorn.exe app:app --host 0.0.0.0 --port 8000
```

Visit `http://<server-ip>:8000` — you should see the app immediately. That
same URL is now the "installable app" you'll open from any phone.

**Keep it running as a Windows service** (so it survives reboots), the same
way you'd run any other always-on Python service:
- Easiest: [NSSM](https://nssm.cc/) — `nssm install BabyTracker "C:\path\to\venv\Scripts\uvicorn.exe" "app:app --host 0.0.0.0 --port 8000"` with the working directory set to `backend\`.
- Alternative: Task Scheduler, running at startup, "run whether user is logged on or not."

**Networking:**
- Open port 8000 (or whichever you choose) in the Windows Firewall and your AWS security group.
- Put it behind HTTPS if it's reachable from the open internet — either an IIS reverse proxy with a cert, or a small reverse proxy like Caddy in front of uvicorn. Browsers and Android both get stricter about installing PWAs and calling APIs over plain HTTP once it's not `localhost`.
- If this only ever needs to be reachable from home Wi-Fi, a plain `http://<lan-ip>:8000` is fine and simpler — skip HTTPS.

**Access code:** Set `ACCESS_CODE` in `.env` to anything — it's a shared
passcode every device enters once (stored on-device after that). Leave it
blank only if the server is not reachable from the internet at all.

The SQLite file (`babytracker.db`) is the entire database. Back it up by
copying that one file.

---

## 2. Use it as an installed app (PWA) — no APK needed

This is the fastest path and works today on both Android and iPhone:

1. Open `http://<server-ip>:8000` in Chrome (Android) or Safari (iPhone).
2. Enter the access code once if you set one.
3. Android: menu → **"Add to Home screen" / "Install app."** iPhone: Share
   sheet → **"Add to Home Screen."**
4. It now behaves like a real app — its own icon, full-screen, works
   offline for the interface (data still needs the network).

Do this on both parents' phones, pointing at the same server address — that
*is* the second-caregiver mode. There's no separate toggle: same server,
same data, live.

---

## 3. Build a real APK (optional)

For a proper installable `.apk` instead of a PWA, this repo already includes
a ready-to-open Capacitor/Android project in `mobile-wrapper/`. The web code
is done — what's left is a native Android build, which needs the Android
SDK and Gradle. That toolchain is large (a few GB) and needs Google's
servers, so it has to be built on your own machine — it can't be produced
inside this chat.

**One-time setup on your machine:**
1. Install [Android Studio](https://developer.android.com/studio) (includes the Android SDK).
2. Install [Node.js](https://nodejs.org).

**Point the app at your server:**
1. Open `mobile-wrapper/www/config.js`.
2. Replace the placeholder with your real address, e.g.:
   ```js
   window.CONFIG = { API_BASE: "https://babytracker.yourdomain.com" };
   ```
   (Use `https://`, not `http://`, if the server has a cert — Android
   blocks plain HTTP to non-local addresses by default. If you must use
   HTTP, allow cleartext traffic in `android/app/src/main/AndroidManifest.xml`.)

**Build:**
```bash
cd mobile-wrapper
npm install
npx cap sync android
npx cap open android      # opens Android Studio
```
In Android Studio: **Build → Generate Signed App Bundle / APK** → choose
APK → create or reuse a signing key → build. The finished `.apk` lands in
`android/app/release/` (or `/debug/` for an unsigned test build). Copy that
file to a phone and install it (enable "install unknown apps" for whichever
app you send it through — Drive, email, etc.).

Rebuilding after a frontend change: edit `frontend/`, copy it over
`mobile-wrapper/www/` again (keeping `mobile-wrapper/www/config.js`'s
server address), then repeat `npx cap sync android` and rebuild in Android
Studio.

*If you'd rather skip Android Studio entirely, the command-line route is
`cd android && ./gradlew assembleDebug` (`gradlew.bat` on Windows) once the
Android SDK is installed and `ANDROID_HOME` is set — Android Studio just
does this for you with a UI.*

---

## How the data model works

- Every phone (browser tab, PWA, or APK) talks to the **same backend URL**,
  so there is one shared SQLite database — that's the whole sharing model.
  No local/shared toggle, no conflict resolution to think about.
- Each device remembers its own **caregiver name** (Family tab) and stamps
  new entries with it ("by Mum", "by Dad") — this is stored locally per
  device, not in the shared database.
- The app polls the server every ~8 seconds, so an entry logged on one
  phone shows up on the other within a few seconds.
- Deleting an entry is immediate and permanent — there's no undo, so the
  app doesn't prompt twice; make sure that's the behavior you want, or ask
  if you'd like a confirmation step added.

## Extending it

Everything is plain Python (`backend/app.py`) and vanilla JS
(`frontend/app.js`) — no build step, no framework version to keep in sync.
Add a table + a couple of endpoints in `app.py`, then a render function +
a couple of `fetch` calls in `app.js`. Natural next additions: medicine/
temperature logging, pumping sessions, splitting night sleep from day naps,
push notifications instead of polling, multi-baby support.
