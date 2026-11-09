// sync.js – realtidssynk via Firebase: rum (picks, bans, siktad comp, egna val, lineup) + spelarprofiler (var och en äger sin egen)
import { initializeApp } from "https://www.gstatic.com/firebasejs/13.2.0/firebase-app.js";
import { getAuth, signInAnonymously, onAuthStateChanged, GoogleAuthProvider, signInWithPopup, signOut } from "https://www.gstatic.com/firebasejs/13.2.0/firebase-auth.js";
import { getDatabase, ref, onValue, set, update, get, query, orderByChild, endAt, onDisconnect, serverTimestamp } from "https://www.gstatic.com/firebasejs/13.2.0/firebase-database.js";

const firebaseConfig = {
  apiKey: "AIzaSyBzF3WnqKAAjUckK7lNbj2tpGSOPJkGAmk",
  authDomain: "lolcompsimulator.firebaseapp.com",
  databaseURL: "https://lolcompsimulator-default-rtdb.europe-west1.firebasedatabase.app",
  projectId: "lolcompsimulator",
  storageBucket: "lolcompsimulator.firebasestorage.app",
  messagingSenderId: "212594544527",
  appId: "1:212594544527:web:b0d592a5f00be1333b05fa"
};
const app = initializeApp(firebaseConfig), auth = getAuth(app), db = getDatabase(app);

const ABC = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";           // inga 0/O, 1/I
const newCode = () => Array.from(crypto.getRandomValues(new Uint8Array(5)), b => ABC[b % ABC.length]).join("");
const okCode = c => /^[A-Z0-9]{4,8}$/.test(c || "");
const OLD = 3 * 864e5;                                     // rum utan aktivitet i 3 dygn rensas

let UID = null, ROOM = null, ONLINE = 1, CONNECTED = false, APPLY = false, UNSUB = [], STARTED = false;
const LAST = { d: Array(20).fill(null), sel: null, aim: Array(5).fill(null), lu: Array(5).fill(null), prof: null };
const err = e => { console.error(e); msg("Synkfel – se konsolen (F12)", true); };

// ---------- skicka lokala ändringar ----------
function pushDraft() {
  if (!ROOM) return; const u = {};
  bans.forEach((c, i) => { const v = c || null; if (LAST.d[i] !== v) { u["d/" + i] = v; LAST.d[i] = v; } });
  if (!Object.keys(u).length) return; u.ts = serverTimestamp();
  update(ref(db, "rooms/" + ROOM), u).catch(err);
}
function pushSel() {
  if (!ROOM) return; const v = SELCOMP || null; if (LAST.sel === v) return; LAST.sel = v;
  update(ref(db, "rooms/" + ROOM), { sel: v, ts: serverTimestamp() }).catch(err);
}
function pushAim() {
  if (!ROOM) return; const u = {};
  AIMX.forEach((c, i) => { const v = c || null; if (LAST.aim[i] !== v) { u["aim/" + i] = v; LAST.aim[i] = v; } });
  if (!Object.keys(u).length) return; u.ts = serverTimestamp();
  update(ref(db, "rooms/" + ROOM), u).catch(err);
}
function pushProf() {
  // egen profil (bara inloggad med Google, och bara sin egen – reglerna i Firebase stoppar allt annat)
  const me = MYUID && prof(MYUID);
  if (me) {
    const sig = JSON.stringify([me.name, me.pools]);
    if (sig !== LAST.prof) { LAST.prof = sig; set(ref(db, "profiles/" + MYUID), { name: me.name, pools: JSON.stringify(me.pools), ts: serverTimestamp() }).catch(err); }
  }
  // lineup följer med rummet
  if (ROOM) {
    const u = {}; PROF.lineup.forEach((id, i) => { if (LAST.lu[i] !== id) { u["lu/" + i] = id; LAST.lu[i] = id; } });
    if (Object.keys(u).length) { u.ts = serverTimestamp(); update(ref(db, "rooms/" + ROOM), u).catch(err); }
  }
}
// index.html anropar TCSYNC("draft" | "sel" | "aim" | "prof") när något ändras lokalt
window.TCSYNC = k => { if (APPLY || !UID) return; ({ draft: pushDraft, sel: pushSel, aim: pushAim, prof: pushProf })[k]?.(); };

// ---------- omritning: vänta medan någon skriver i ett fält, så inmatning inte försvinner ----------
const PEND = { players: false, render: false }; let PT = null;
const busy = () => { const a = document.activeElement; return !!(a && /^(INPUT|SELECT)$/.test(a.tagName) && a.closest("#tab-spelare,#aimcol,#list,.board")); };
function refresh(o) { if (o.players) PEND.players = true; if (o.render) PEND.render = true; clearTimeout(PT); PT = setTimeout(flush, 80); }
function flush() {
  if (!PEND.players && !PEND.render) return;
  if (busy()) { document.addEventListener("focusout", () => { clearTimeout(PT); PT = setTimeout(flush, 200); }, { once: true }); return; }
  const p = PEND.players, r = PEND.render; PEND.players = PEND.render = false;
  APPLY = true; try { if (p) renderPlayers(); if (r) render(); } finally { APPLY = false; }
}

// ---------- ta emot ändringar ----------
function applyRoom(v) {
  APPLY = true;
  try {
    let ch = false; const d = v.d || {}, a = v.aim || {};
    for (let i = 0; i < 20; i++) {
      const c = d[i] && CHAMPS.includes(d[i]) ? d[i] : null; LAST.d[i] = c;
      if (bans[i] === c) continue; ch = true;
      const sl = document.querySelector(`.slot[data-idx="${i}"]`); if (!sl) continue;
      if (c) sl._set(c);
      else { bans[i] = null; const inp = sl.querySelector("input"); inp.value = ""; inp.readOnly = false; sl.classList.remove("filled"); }
    }
    const sel = v.sel && COMPS.some(c => c.n === v.sel) ? v.sel : null; LAST.sel = sel;
    if (SELCOMP !== sel) { SELCOMP = sel; try { sel ? localStorage.setItem("tc-selcomp", sel) : localStorage.removeItem("tc-selcomp"); } catch (e) {} ch = true; }
    for (let i = 0; i < 5; i++) {
      const c = a[i] && CHAMPS.includes(a[i]) ? a[i] : null; LAST.aim[i] = c;
      if (AIMX[i] !== c) { AIMX[i] = c; ch = true; }
    }
    if (v.lu) {
      let lc = false;
      for (let i = 0; i < 5; i++) { const id = typeof v.lu[i] === "string" ? v.lu[i] : null; if (!id) continue; LAST.lu[i] = id; if (PROF.lineup[i] !== id) { PROF.lineup[i] = id; lc = true; } }
      if (lc) { saveProf(); applyProfiles(); refresh({ players: true }); ch = true; }
    }
    if (ch) { save(); saveAimx(); refresh({ render: true }); }
  } finally { APPLY = false; }
}
const profSig = list => JSON.stringify(list.filter(p => !p.builtin).map(p => [p.id, p.name, p.pools]).sort((a, b) => a[0] < b[0] ? -1 : 1));
function startProfiles() {
  onValue(ref(db, "profiles"), s => {
    const v = s.val() || {}, list = [];
    Object.entries(v).forEach(([id, x]) => {
      if (!x || typeof x !== "object" || typeof x.name !== "string" || typeof x.pools !== "string") return;   // hoppar över gammalt format
      let pools; try { pools = JSON.parse(x.pools); } catch (e) { return; }
      if (!Array.isArray(pools) || pools.length !== 5) return;
      list.push({ id, name: cleanName(x.name) || "Namnlös", pools: pools.map(r => Array.isArray(r) ? r.filter(c => CHAMPS.includes(c)) : []) });
    });
    const mine = list.find(p => p.id === MYUID);
    if (mine) LAST.prof = JSON.stringify([mine.name, mine.pools]);
    else if (MYUID && prof(MYUID)) list.push(prof(MYUID));     // nyss skapad lokalt, inte i databasen än
    if (profSig(list) === profSig(PROF.profiles)) return;      // inget nytt (t.ex. ekot av ens egen ändring)
    APPLY = true; try { setProfiles(list); } finally { APPLY = false; }
    refresh({ players: true, render: true });
  }, err);
}
window.TCDELPROF = () => { if (!MYUID) return; LAST.prof = null; set(ref(db, "profiles/" + MYUID), null).catch(err); };
window.TCLOGIN = () => signInWithPopup(auth, new GoogleAuthProvider()).catch(e => {
  if (e.code === "auth/popup-closed-by-user" || e.code === "auth/cancelled-popup-request") return;
  console.error(e); pMsg(e.code === "auth/popup-blocked" ? "Popupen blockerades – tillåt popups för sidan" : "Inloggningen misslyckades – se konsolen (F12)");
});
window.TCLOGOUT = () => signOut(auth).catch(err);

// ---------- rum ----------
function presence() {
  if (!ROOM || !UID) return; const me = ref(db, `rooms/${ROOM}/on/${UID}`);
  onDisconnect(me).remove().catch(err); set(me, true).catch(err);
}
function setUrl(code) {
  const u = new URL(location.href);
  if (code) u.searchParams.set("rum", code); else u.searchParams.delete("rum");
  if (/^#rum=/i.test(u.hash)) u.hash = "#draft";
  history.replaceState(null, "", u);
}
async function cleanup() {
  try {
    const s = await get(query(ref(db, "rooms"), orderByChild("ts"), endAt(Date.now() - OLD)));
    const u = {}; s.forEach(c => { if (c.key !== ROOM) u[c.key] = null; });
    if (Object.keys(u).length) await update(ref(db, "rooms"), u);
  } catch (e) { console.warn("Rensning av gamla rum misslyckades", e); }
}
async function join(code, fresh) {
  code = (code || "").trim().toUpperCase();
  if (!okCode(code)) return msg("Koden ska vara 4–8 bokstäver/siffror", true);
  const r = ref(db, "rooms/" + code);
  try {
    if (fresh) {
      const d = {}, aim = {};
      bans.forEach((c, i) => { if (c) d[i] = c; }); AIMX.forEach((c, i) => { if (c) aim[i] = c; });
      const lu = {}; PROF.lineup.forEach((id, i) => { lu[i] = id; });
      await set(r, { d, aim, lu, sel: SELCOMP || null, ts: serverTimestamp() });
    } else if (!(await get(r)).exists()) { setUrl(null); return msg(`Hittar inget rum med koden ${code}`, true); }
  } catch (e) { return err(e); }
  leave(true);
  ROOM = code; LAST.d.fill(null); LAST.aim.fill(null); LAST.lu.fill(null); LAST.sel = null;
  setUrl(code); presence();
  update(r, { ts: serverTimestamp() }).catch(err);
  UNSUB.push(onValue(r, s => {
    if (ROOM !== code) return; const v = s.val();
    if (!v) { msg("Rummet finns inte längre", true); return; }   // rensa inte lokalt om rummet försvunnit
    applyRoom(v); if (!v.lu) pushProf(); ONLINE = v.on ? Object.keys(v.on).length : 1; ui();
  }, err));
  ui(); if (fresh) { cleanup(); msg("Rum skapat – kopiera länken till laget"); }
}
function leave(silent) {
  UNSUB.forEach(f => f()); UNSUB = [];
  if (ROOM && UID) { const me = ref(db, `rooms/${ROOM}/on/${UID}`); onDisconnect(me).cancel().catch(() => {}); set(me, null).catch(() => {}); }
  ROOM = null; ONLINE = 1;
  if (!silent) { setUrl(null); ui(); msg("Du har lämnat rummet – draften ligger kvar lokalt"); }
}
const link = () => { const u = new URL(location.href); u.search = "?rum=" + ROOM; u.hash = "#draft"; return u.href; };

// ---------- UI (läggs i raden med "Rensa allt") ----------
document.head.insertAdjacentHTML("beforeend", `<style>
.room{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.room .rctl{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.room input{font:inherit;width:7.5em;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);text-transform:uppercase}
.room .rs{color:var(--muted);font-size:14px}
.room .rs b{color:var(--gold);letter-spacing:.08em}
.room .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#4caf50;margin-right:5px;vertical-align:1px}
.room .dot.off{background:#e57373}
.room .rmsg{font-size:13px;color:var(--muted)}
.room .rmsg.bad{color:#e57373}
</style>`);
const box = document.createElement("div"); box.className = "room";
box.innerHTML = `<div class="rctl"><span class="rs">Ansluter…</span></div><span class="rmsg" aria-live="polite"></span>`;
const tools = document.querySelector(".tools"); tools.insertBefore(box, tools.querySelector(".deco") || tools.lastElementChild);
const ctl = box.querySelector(".rctl"), mbox = box.querySelector(".rmsg");
let MT = null;
function msg(t, bad) { mbox.textContent = t; mbox.classList.toggle("bad", !!bad); clearTimeout(MT); MT = setTimeout(() => { mbox.textContent = ""; }, 4000); }
function ui() {
  if (!UID) return;
  ctl.innerHTML = ROOM
    ? `<span class="rs"><span class="dot${CONNECTED ? "" : " off"}"></span>Rum <b>${ROOM}</b> · ${CONNECTED ? ONLINE + " online" : "offline"}</span><button type="button" data-a="copy">Kopiera länk</button><button type="button" data-a="leave">Lämna</button>`
    : `<button type="button" data-a="new">Skapa rum</button><input maxlength="8" placeholder="Rumskod" aria-label="Rumskod"><button type="button" data-a="join">Gå med</button>`;
}
ctl.addEventListener("click", e => {
  const a = e.target.closest("button")?.dataset.a; if (!a) return;
  if (a === "new") join(newCode(), true);
  if (a === "join") join(ctl.querySelector("input").value, false);
  if (a === "leave") leave(false);
  if (a === "copy") (navigator.clipboard ? navigator.clipboard.writeText(link()) : Promise.reject())
    .then(() => msg("Länk kopierad")).catch(() => prompt("Kopiera länken:", link()));
});
ctl.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.matches("input")) join(e.target.value, false); });

// ---------- start ----------
const startCode = () => (new URLSearchParams(location.search).get("rum") || (location.hash.match(/^#rum=([A-Za-z0-9]+)/i) || [])[1] || "").toUpperCase();
const authFail = e => { console.error(e); ctl.innerHTML = `<span class="rs"><span class="dot off"></span>Ingen anslutning</span>`; msg("Inloggning misslyckades – är Anonymous påslaget?", true); };
onAuthStateChanged(auth, u => {
  if (!u) { signInAnonymously(auth).catch(authFail); return; }        // utloggad → tillbaka till anonym
  if (UID && UID !== u.uid && ROOM) set(ref(db, `rooms/${ROOM}/on/${UID}`), null).catch(() => {});
  UID = u.uid; MYUID = u.isAnonymous ? null : u.uid; MYNAME = u.isAnonymous ? "" : (u.displayName || u.email || "");
  LAST.prof = null;
  if (MYUID && prof(MYUID)) PSEL = MYUID;
  APPLY = true; try { renderPlayers(); } finally { APPLY = false; }
  if (!STARTED) {
    STARTED = true; startProfiles();
    onValue(ref(db, ".info/connected"), s => { CONNECTED = s.val() === true; if (CONNECTED) presence(); ui(); });
    const c = startCode(); ui(); if (c) join(c, false);
  } else { presence(); ui(); }
});
