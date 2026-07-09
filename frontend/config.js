// Runtime configuration.
//
// When the frontend is served BY the same FastAPI server (the normal
// case), leave API_BASE as "" — the app calls /api/... on its own origin.
//
// When you package this frontend into the Capacitor/APK wrapper, the
// app runs from a different origin (capacitor://localhost) and needs
// to know your server's real address. Set it here before building,
// e.g.:
//   API_BASE: "https://babytracker.yourdomain.com"
//   API_BASE: "http://203.0.113.10:8000"
window.CONFIG = {
  API_BASE: "",
};
