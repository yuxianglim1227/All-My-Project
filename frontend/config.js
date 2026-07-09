// Runtime configuration.
//
// GOOGLE_CLIENT_ID is only needed if you plan to offer Google Drive as a
// storage option. It is a PUBLIC identifier (not a secret) — safe to commit
// to a public repo. Get one from Google Cloud Console -> APIs & Services ->
// Credentials -> "OAuth client ID" -> Application type: "Web application".
// Add your app's exact URL (e.g. https://you.github.io/baby-tracker/) under
// "Authorized JavaScript origins". See README.md for full step-by-step setup.
//
// If you only want GitHub as a storage option, leave this blank -- the
// Google Drive setup button will simply explain it's not configured.
window.CONFIG = {
  GOOGLE_CLIENT_ID: "",
};
