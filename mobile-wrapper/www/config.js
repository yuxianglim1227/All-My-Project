// Runtime configuration for the PACKAGED (Capacitor/APK) build.
//
// This app runs from capacitor://localhost inside the APK, which is a
// different origin from your server — so API_BASE must be a full URL.
// Set this to wherever you deployed the backend, then rebuild the app.
window.CONFIG = {
  API_BASE: "https://REPLACE-WITH-YOUR-SERVER-ADDRESS",
};
