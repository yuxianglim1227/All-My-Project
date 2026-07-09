# Baby Tracker

A baby tracker — feeding, naps, diapers, meals, growth, daily routine, weekly
meal plan, prep checklist — shared live between caregivers.

**There is no server.** This is a static HTML/CSS/JS app (a PWA) that reads
and writes its data directly to either **GitHub** or **Google Drive**, using
your own account as the database. Open the same file from two phones and
you have "family sharing" — no backend to run, patch, or pay for.

```
babytracker/
├── frontend/                 The whole app — this is what you host/open
│   ├── index.html / styles.css / app.js
│   ├── storage.js            Picks a backend, generic read-modify-write + retry
│   ├── storage-github.js     Talks to the GitHub Contents API
│   ├── storage-drive.js      Talks to the Google Drive API (GIS auth)
│   ├── config.js             Your Google OAuth Client ID goes here (optional)
│   ├── manifest.json / service-worker.js / icons/
├── mobile-wrapper/           Capacitor project — packages frontend/ as a real .apk
│   └── android/              (see the GitHub-only note below)
├── backend/                  OPTIONAL — a self-hosted FastAPI+SQLite alternative,
│                             kept for anyone who'd rather run their own server
│                             instead of GitHub/Drive. Not needed for the setup
│                             below.
└── README.md
```

---

## Which data store should you pick?

|                          | GitHub                                   | Google Drive |
|---|---|---|
| Setup effort             | 5 minutes                                | ~15 minutes (Cloud Console project) |
| Sharing model            | One access token, used by both phones    | Each caregiver signs in with their own Google account |
| Works inside the packaged APK | ✅ Yes                               | ⚠️ No — Google's sign-in needs a real https:// origin; use the PWA (below) instead |
| What it looks like under the hood | A JSON file, one git commit per change | A JSON file in Drive |

If you want the APK, use GitHub. If you're fine using the installed-PWA
route (no APK), either works — Drive means each caregiver keeps their own
Google login instead of sharing a token.

---

## Option A — GitHub

### 1. Create a repo for the data
A **private** repo, separate from your app code is the cleanest option —
e.g. `baby-tracker-data`. Just needs to exist; the app creates the JSON file
inside it automatically on first connect.

### 2. Create a Personal Access Token
GitHub → Settings → Developer settings → **Personal access tokens →
Fine-grained tokens** → Generate new token.
- **Repository access:** "Only select repositories" → pick the one repo above.
- **Permissions:** Repository permissions → **Contents: Read and write**. Nothing else.
- Set an expiration you're comfortable with (you'll generate a new one and
  re-enter it on each device when it expires).

Copy the token now — GitHub only shows it once.

### 3. Connect the app
Open the app → **GitHub** → enter:
- Repo owner (your GitHub username)
- Repo name (`baby-tracker-data`)
- File path (default `baby-tracker-data.json` is fine)
- The token you just created

Hit Connect. Do this on the second phone too, with the same three fields —
either the same token, or a second fine-grained token scoped to the same repo.

**What happens to the data:** every logged entry is a real git commit. You
can look at the full history in the GitHub UI at any time. Expect the
commit count to climb quickly (every feed/nap/diaper is one commit) — that's
harmless, GitHub repos handle this fine, but don't be surprised by it.

---

## Option B — Google Drive

This needs a one-time Google Cloud Console setup, done by whichever
caregiver is setting this up first.

### 1. Create a Google Cloud project
[console.cloud.google.com](https://console.cloud.google.com) → create a new
project (any name).

### 2. Configure the OAuth consent screen
APIs & Services → OAuth consent screen.
- User type: **External**.
- Fill in the required app name / support email fields.
- **Publishing status: leave it on "Testing"** — this is what makes this
  free and instant instead of needing Google's app-review process.
- Under **Test users**, add both caregivers' Google account emails. Only
  these accounts will be able to sign in.

### 3. Enable the Drive API
APIs & Services → Library → search "Google Drive API" → Enable.

### 4. Create an OAuth Client ID
APIs & Services → Credentials → Create Credentials → **OAuth client ID**.
- Application type: **Web application**.
- **Authorized JavaScript origins:** the exact URL you'll open this app
  from (e.g. `https://you.github.io/baby-tracker` if using GitHub Pages
  below, or `http://localhost:8080` while testing locally).

Copy the **Client ID** (not the secret — none is needed).

### 5. Add it to the app
Open `frontend/config.js` and set:
```js
window.CONFIG = { GOOGLE_CLIENT_ID: "xxxxx.apps.googleusercontent.com" };
```
This value is public by design (it's restricted by the origins you set
above, not by secrecy) — fine to commit.

### 6. Connect
Open the app → **Google Drive** → Sign in. First caregiver: choose
**"Create it."** This creates `Baby Tracker Data.json` in their Drive.

### 7. Invite the second caregiver
In the app's **Family** tab, enter their email under "Share with another
caregiver" and tap Invite (this uses Drive's sharing API directly — no need
to leave the app). They then open the app, choose Google Drive, sign in
with that email, and the shared file is found automatically.

**Note on permissions:** this uses the broad `drive` scope (not the
narrower `drive.file`) specifically so a second Google account can access a
file it didn't create, without needing Google's file-picker widget. Since
the app stays in "Testing" mode, Google shows the sign-in only to the test
users you listed — nobody else can use it, verified or not.

---

## Hosting the app

It's static files — anything that serves plain HTML works. Since the code
already lives in GitHub, **GitHub Pages** is the natural choice and needs
no extra hosting account:

1. Push this repo to GitHub (see below if you haven't yet).
2. Repo → Settings → Pages → Source: **Deploy from a branch** → branch
   `main`, folder `/frontend`.
3. Your app is live at `https://<you>.github.io/<repo>/`.

If you're using Google Drive, make sure that exact URL is in "Authorized
JavaScript origins" (step 4 above) — GitHub Pages URLs are stable once set.

### Pushing this repo to GitHub
```bash
git remote add origin https://github.com/<you>/baby-tracker.git
git branch -M main
git push -u origin main
```
(Create the empty repo first at github.com/new.)

---

## Installing it on a phone (PWA — works today, no app store)

1. Open the hosted URL in Chrome (Android) or Safari (iPhone).
2. Android: menu → **"Add to Home screen."** iPhone: Share sheet →
   **"Add to Home Screen."**
3. It now behaves like a real app — its own icon, full-screen.

Do this on both caregivers' phones. Each device stores its own connection
info (token, or Drive sign-in) in that browser's local storage — nothing is
shared between devices except through GitHub/Drive itself.

---

## Building a real APK (GitHub-backed only — see table above)

`mobile-wrapper/` already has a Capacitor/Android project pointed at this
frontend. Building the actual `.apk` needs the Android SDK, which has to
run on your own machine (Google's SDK servers aren't reachable from where
this was built):

```bash
cd mobile-wrapper
npm install
npx cap sync android
npx cap open android      # opens Android Studio
```
Build → Generate Signed App Bundle / APK. The result lands in
`android/app/release/` (or `/debug/`). Install it on a phone and enter your
GitHub token on first launch, same as the PWA.

If you change `frontend/`, copy it into `mobile-wrapper/www/` again and
re-run `npx cap sync android` before rebuilding.

---

## Security notes

- **GitHub token:** scope it to exactly one repo, Contents read/write only.
  It's stored in each device's local storage, never committed anywhere. If
  a device is lost, revoke that token on GitHub and issue a new one.
- **Google Drive:** access tokens are short-lived (~1 hour) and live only in
  memory, not storage — you'll occasionally need to tap "Reconnect" if the
  app's been idle. Revoke access anytime at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
- Both backends mean your baby's data lives in an account you already
  control — no third-party server in between.

## Conflict handling

- **GitHub:** real optimistic concurrency — each save checks the file's
  current version and retries automatically if another device saved a
  half-second earlier.
- **Google Drive:** best-effort — the app always reads the latest version
  immediately before writing, which makes true collisions rare, but there's
  no hard version check the way GitHub has one. If both caregivers save in
  the exact same instant, the later write wins.

## Extending it

Everything is plain JS — `app.js` for UI/rendering, `storage-github.js` /
`storage-drive.js` for the two backends. Add a field to `storage.js`'s
`DEFAULT_STATE`, then a render function + a couple of `withMutation(...)`
calls in `app.js`. Natural next additions: medicine/temperature logging,
pumping sessions, splitting night sleep from naps, a third backend (e.g.
Dropbox) by writing one more file matching the `readRaw/writeRaw` shape.
