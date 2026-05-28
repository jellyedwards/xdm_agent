// Firebase Web SDK, ESM via Google's CDN — no build step.
// API key + project ID are public Firebase identifiers; rules secure resources.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.13.0/firebase-app.js";
import {
  getAuth, signInWithPopup, signOut, GoogleAuthProvider,
  onAuthStateChanged, onIdTokenChanged,
} from "https://www.gstatic.com/firebasejs/10.13.0/firebase-auth.js";

const firebaseConfig = {
  apiKey: "AIzaSyDggOfJzx7KtpoO1nFcRaty58yriL0HBFM",
  authDomain: "design-xdm.firebaseapp.com",
  projectId: "design-xdm",
  storageBucket: "design-xdm.firebasestorage.app",
  messagingSenderId: "426915934003",
  appId: "1:426915934003:web:e6a6b47a26c4b2366a9846",
};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);

let currentUser = null;
let embedToken = null;       // populated when embedded (Flow 2) via postMessage
let embedExpiresAt = 0;
const listeners = new Set();
let firstResolve;
const firstAuthDecision = new Promise(r => { firstResolve = r; });

function fanout(u) { for (const fn of listeners) { try { fn(u); } catch (e) { console.error(e); } } }

onAuthStateChanged(auth, (user) => {
  currentUser = user;
  if (firstResolve) { firstResolve(user); firstResolve = null; }
  fanout(user);
});
onIdTokenChanged(auth, () => fanout(currentUser));

// Embedded mode: design xdm's parent page can pipe in auth state via postMessage.
// We accept messages only from a known origin.
const TRUSTED_PARENT_ORIGINS = new Set([
  "https://designxdm.com",
  "https://www.designxdm.com",
  "http://localhost:3009",
]);
const isEmbedded = window.self !== window.top;

window.addEventListener("message", (ev) => {
  if (!TRUSTED_PARENT_ORIGINS.has(ev.origin)) return;
  const msg = ev.data;
  if (!msg || typeof msg !== "object") return;
  if (msg.kind === "auth" && msg.id_token) {
    embedToken = msg.id_token;
    embedExpiresAt = Date.now() + ((msg.expires_in_seconds || 3300) * 1000);
    const u = msg.user ? { uid: msg.user.uid, email: msg.user.email, displayName: msg.user.display_name } : { uid: "embed", email: "embedded" };
    currentUser = u;
    if (firstResolve) { firstResolve(u); firstResolve = null; }
    fanout(u);
  } else if (msg.kind === "install_complete" && msg.installation_id) {
    window.dispatchEvent(new CustomEvent("agent-install-complete", { detail: msg }));
  }
});

async function requestEmbedRefresh() {
  if (!isEmbedded) return null;
  return new Promise((resolve) => {
    const handler = (ev) => {
      if (!TRUSTED_PARENT_ORIGINS.has(ev.origin)) return;
      const m = ev.data;
      if (m && m.kind === "auth" && m.id_token) {
        embedToken = m.id_token;
        embedExpiresAt = Date.now() + ((m.expires_in_seconds || 3300) * 1000);
        window.removeEventListener("message", handler);
        resolve(embedToken);
      }
    };
    window.addEventListener("message", handler);
    window.parent.postMessage({ kind: "refresh_token" }, "*");
    setTimeout(() => { window.removeEventListener("message", handler); resolve(null); }, 5000);
  });
}

async function getIdToken() {
  if (isEmbedded) {
    if (embedToken && Date.now() < embedExpiresAt - 60_000) return embedToken;
    return await requestEmbedRefresh();
  }
  return currentUser ? await currentUser.getIdToken() : null;
}

window.agentAuth = {
  isEmbedded,
  firstAuthDecision: () => firstAuthDecision,
  current: () => currentUser,
  signIn: () => signInWithPopup(auth, new GoogleAuthProvider()),
  signOut: () => signOut(auth),
  getIdToken,
  onChange: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
  // For #3 — used from app.js when the user clicks "Connect to design xdm".
  requestInstall: (agentId, scopes) => {
    if (isEmbedded) {
      window.parent.postMessage({ kind: "request_install", agent: agentId, scopes }, "*");
      return new Promise((resolve) => {
        const h = (ev) => { window.removeEventListener("agent-install-complete", h); resolve(ev.detail); };
        window.addEventListener("agent-install-complete", h);
      });
    }
    // popup fallback handled in app.js
    return null;
  },
};

// Signal readiness so app.js can wait for the SDK to come online.
window.dispatchEvent(new CustomEvent("agent-auth-ready"));
