const $app = document.getElementById("app");
const $userSlot = document.getElementById("user-slot");

// Stable per-browser session id for public-demo quotas.
const SESSION_ID = (() => {
  let s = localStorage.getItem("xdm_session_id");
  if (!s) {
    s = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
    localStorage.setItem("xdm_session_id", s);
  }
  return s;
})();
// Capture ?code=… from the magic link and remember it.
(() => {
  const c = new URLSearchParams(location.search).get("code");
  if (c) localStorage.setItem("xdm_access_code", c.trim());
})();
function accessCode() { return localStorage.getItem("xdm_access_code") || ""; }

async function authHeaders() {
  const headers = { "content-type": "application/json", "X-Session-Id": SESSION_ID };
  const code = accessCode();
  if (code) headers["X-Access-Code"] = code;
  try {
    const token = await window.agentAuth?.getIdToken?.();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  } catch (e) { console.warn("getIdToken failed:", e); }
  return headers;
}

async function _err(p, r) {
  let detail = "";
  try { detail = (await r.json()).detail || ""; } catch { try { detail = (await r.text()).slice(0, 200); } catch {} }
  const e = new Error(detail || `${p}: ${r.status}`);
  e.status = r.status;
  return e;
}

const api = {
  async get(p) {
    const r = await fetch(p, { headers: await authHeaders(), cache: "no-store" });
    if (!r.ok) throw await _err(p, r);
    return r.json();
  },
  async post(p, body) {
    const r = await fetch(p, { method: "POST", headers: await authHeaders(), body: JSON.stringify(body || {}) });
    if (!r.ok) throw await _err(p, r);
    return r.json();
  },
  async del(p) {
    const r = await fetch(p, { method: "DELETE", headers: await authHeaders() });
    if (!r.ok) throw await _err(p, r);
    return r.json();
  },
};

// Quota / access-aware error surfacing. On 403 with no code yet, let the user
// paste the access code they were given.
function notifyError(e) {
  if (e && e.status === 403 && !accessCode()) {
    const c = prompt("This is a limited public demo.\nEnter the access code you were given:");
    if (c && c.trim()) { localStorage.setItem("xdm_access_code", c.trim()); alert("Access code saved — please try again."); return; }
  }
  alert(e && e.message ? e.message : String(e));
}

async function updateQuotaNote() {
  const note = document.getElementById("quota-note");
  if (!note) return;
  try {
    const q = await api.get("/quota");
    note.textContent = (!q.public_mode || q.trusted) ? "" : `· ${q.hunts_remaining}/${q.daily_hunts} runs left today`;
  } catch { note.textContent = ""; }
}

const RIGHTS_LABEL = {
  clear:   { text: "free to use",     cls: "good", title: "Licensed for any use; attribution preserved." },
  caveat:  { text: "credit required", cls: "warn", title: "Editorial use and/or attribution required — full string saved with the image." },
  unknown: { text: "rights unknown",  cls: "",     title: "License could not be confirmed — surfaced only if explicitly enabled." },
};

let SOURCES = {};
const AGENT_ID = "xdm_agent_v1";

function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const k in props) {
    if (k === "class") e.className = props[k];
    else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), props[k]);
    else if (k === "html") e.innerHTML = props[k];
    else e.setAttribute(k, props[k]);
  }
  for (const c of kids) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function sourceName(id) { return (SOURCES[id] && SOURCES[id].display_name) || id; }

async function refreshHealth() {
  try {
    await api.get("/health");
    SOURCES = await api.get("/sources");
  } catch {}
}

function route() {
  const h = location.hash || "#/";
  if (h.startsWith("#/mindset/")) {
    const id = h.split("/")[2];
    if (h.endsWith("/trace")) return renderTrace(id);
    return renderMindset(id);
  }
  return renderHome();
}

async function renderHome() {
  $app.replaceChildren();
  const card = el("section", {},
    el("h2", {}, "create a mindset"),
    el("div", {class:"card"},
      el("div", {class:"row"},
        el("div", {class:"field"},
          el("label", {for:"name"}, "name"),
          el("input", {type:"text", id:"name", placeholder:"e.g. 'microscopy feed'"}),
        ),
        el("div", {class:"field"},
          el("label", {for:"theme"}, "prompt / theme"),
          el("input", {type:"text", id:"theme", placeholder:"e.g. 'microscopy'"}),
        ),
        el("button", {class:"primary", id:"create-btn", onClick: createMindset, style:"align-self:flex-end"}, "create"),
      ),
      el("div", {class:"theme", style:"margin-top:8px"}, "creating a mindset triggers a deep dive on the theme — competitions, creators, subgenres, queries — before the first hunt. takes ~10s."),
    )
  );
  $app.appendChild(card);

  const listSec = el("section", {}, el("h2", {}, "your mindsets"));
  const list = el("div", {class:"mindset-list", id:"mindset-list"});
  listSec.appendChild(list);
  $app.appendChild(listSec);

  try {
    const ms = await api.get("/mindset");
    if (!ms.length) list.appendChild(el("div", {class:"empty"}, "no mindsets yet"));
    for (const m of ms) {
      list.appendChild(el("a", {href: `#/mindset/${m.id}`},
        el("div", {class:"name"}, m.name),
        el("div", {class:"theme"}, `${m.theme} · v${m.version}${m.dossier ? " · researched" : ""}`),
      ));
    }
  } catch (e) { list.appendChild(el("div", {class:"empty"}, `error: ${e.message}`)); }
}

async function createMindset() {
  const btn = document.getElementById("create-btn");
  const name = document.getElementById("name").value.trim();
  const theme = document.getElementById("theme").value.trim();
  if (!name || !theme) { alert("name and theme required"); return; }
  btn.disabled = true; btn.replaceChildren(el("span", {class:"spinner"}), document.createTextNode("researching " + theme + "…"));
  try {
    const m = await api.post("/mindset", {name, theme});
    location.hash = `#/mindset/${m.id}`;
  } catch (e) { notifyError(e); btn.disabled = false; btn.textContent = "create"; }
}

async function renderMindset(id) {
  $app.replaceChildren();
  $app.appendChild(el("div", {}, el("a", {href:"#/"}, "← back")));
  $app.appendChild(el("section", {id:"mhead"}));
  $app.appendChild(el("section", {id:"dossier-sec"}));
  $app.appendChild(el("section", {},
    el("h2", {}, "nudge"),
    el("div", {class:"card"},
      el("textarea", {id:"direction", placeholder:"e.g. 'lean into bioluminescence' or 'less clinical, more art' — applied on next hunt", rows:"2"}),
      el("div", {class:"row", style:"margin-top:8px"},
        el("button", {onClick: () => saveDirection(id)}, "save direction"),
        el("button", {class:"primary", id:"hunt-btn", onClick: () => hunt(id)}, "hunt now"),
        el("span", {id:"quota-note", class:"theme"}, ""),
      ),
    )
  ));
  $app.appendChild(el("section", {id:"publish-sec"}));
  $app.appendChild(el("section", {id:"rubric"}));
  $app.appendChild(el("section", {}, el("h2", {}, "feed"), el("div", {id:"feed", class:"grid"})));
  $app.appendChild(el("section", {}, el("h2", {}, "recent hunts"), el("div", {id:"hunts"})));
  await reloadMindset(id);
}

async function reloadMindset(id) {
  try {
    let m;
    try { m = await api.get(`/mindset/${id}`); }
    catch (e) {
      if (String(e.message).includes("404")) {
        $app.replaceChildren(
          el("div", {class:"card", style:"text-align:center; padding:40px"},
            el("div", {style:"font-size:18px;margin-bottom:12px"}, "that mindset is gone"),
            el("div", {class:"theme", style:"margin-bottom:16px"}, "it may have been wiped before persistence was on"),
            el("a", {href:"#/"}, "← go home")
          )
        );
        return;
      }
      throw e;
    }
    const head = document.getElementById("mhead");
    head.replaceChildren(
      el("h2", {}, "mindset"),
      el("div", {class:"card"},
        el("div", {style:"font-size:18px;font-weight:600"}, m.name),
        el("div", {class:"theme"}, `${m.theme}`),
        el("div", {style:"margin-top:10px"},
          el("span", {class:"pill"}, `v${m.version}`),
          el("span", {class:"pill"}, `serendipity ${m.serendipity?.toFixed(2)}`),
          ...Object.entries(m.tactic_prefs || {}).map(([t, v]) => el("span", {class: "pill" + (v > 1.05 ? " good" : v < 0.95 ? " warn" : "")}, `${t} ${v.toFixed(2)}`)),
        ),
      )
    );

    renderDossier(id, m);

    document.getElementById("rubric").replaceChildren(
      el("details", {},
        el("summary", {}, "show rubric (the agent's internal taste model)"),
        el("div", {class:"card"}, el("pre", {class:"rubric"}, m.rubric_text || "(not seeded)")),
      )
    );

    const cs = await api.get(`/collection/${id}`);
    renderPublish(id, m, cs);
    updateQuotaNote();
    const feed = document.getElementById("feed");
    feed.replaceChildren();
    if (!cs.length) feed.appendChild(el("div", {class:"empty"}, "no images yet — try 'hunt now'"));
    for (const c of cs) feed.appendChild(renderTile(c));

    const hs = await api.get(`/hunts/${id}`);
    const hunts = document.getElementById("hunts");
    hunts.replaceChildren();
    if (!hs.length) hunts.appendChild(el("div", {class:"empty"}, "no hunts yet"));
    for (const h of hs) {
      hunts.appendChild(el("div", {class:"card"},
        el("div", {}, `${h.status} · ${h.started_at?.slice(0,19)} · ${h.duration_ms || "?"}ms`),
        el("div", {class:"theme"}, `plan ${h.plan?.length || 0} · raw ${h.n_candidates} · unique ${h.n_unique} · cleared ${h.n_rights_cleared} · kept ${h.n_kept}`),
        el("div", {style:"margin-top:8px"}, el("a", {href:`#/mindset/${id}/trace`, "data-hunt-id": h.id}, "view trace →")),
      ));
    }
  } catch (e) { alert(`reload failed: ${e.message}`); }
}

function dossierSection(label, preview, body) {
  const det = el("details", {class:"dossier-collapsible"});
  const sum = el("summary", {},
    el("span", {class:"dossier-label"}, label),
    preview ? el("span", {class:"dossier-preview"}, preview) : null,
  );
  det.appendChild(sum);
  const bod = el("div", {class:"dossier-body"});
  body.forEach(c => bod.appendChild(c));
  det.appendChild(bod);
  return det;
}

function firstSentence(s, limit = 90) {
  const m = (s || "").match(/^[^.!?]+[.!?]?/);
  let text = (m ? m[0] : (s || "")).trim();
  if (text.length > limit) text = text.slice(0, limit - 1).trimEnd() + "…";
  return text;
}

function summarizeItems(items, max = 3) {
  const arr = items || [];
  if (!arr.length) return "";
  return arr.slice(0, max).join(", ") + (arr.length > max ? ", …" : "");
}

function truncate(s, limit) {
  if (!s) return "";
  s = String(s).trim();
  return s.length > limit ? s.slice(0, limit).trimEnd() + "…" : s;
}

function renderDossier(id, m) {
  const sec = document.getElementById("dossier-sec");
  sec.replaceChildren();
  sec.appendChild(el("h2", {}, "dossier — what the agent knows about this theme"));
  const d = m.dossier;
  if (!d) {
    sec.appendChild(el("div", {class:"card", style:"text-align:center"},
      el("div", {class:"theme", style:"margin-bottom:10px"}, "no dossier yet for this mindset"),
      el("button", {id:"build-dossier-btn", onClick: () => buildDossier(id)}, "build dossier"),
    ));
    return;
  }
  const groups = [];
  if (d.summary) groups.push(el("div", {class:"summary"}, d.summary));

  if (d.visual_markers && d.visual_markers.length) {
    groups.push(dossierSection(
      "visual hallmarks",
      summarizeItems(d.visual_markers),
      [el("div", {class:"pill-row"}, ...d.visual_markers.map(v => el("span", {class:"pill"}, v)))],
    ));
  }
  if (d.subgenres && d.subgenres.length) {
    groups.push(dossierSection(
      "subgenres",
      summarizeItems(d.subgenres.map(s => s.name)),
      d.subgenres.map(s => el("div", {class:"item"}, el("b", {}, s.name), s.description ? " — " + s.description : "")),
    ));
  }
  if (d.competitions && d.competitions.length) {
    groups.push(dossierSection(
      "competitions & showcases",
      summarizeItems(d.competitions.map(c => c.name)),
      d.competitions.map(c => el("div", {class:"item"},
        c.url ? el("a", {href:c.url, target:"_blank", rel:"noopener"}, c.name) : el("b", {}, c.name),
        c.description ? el("span", {class:"why"}, " — " + c.description) : "",
      )),
    ));
  }
  if (d.galleries_and_archives && d.galleries_and_archives.length) {
    groups.push(dossierSection(
      "galleries & archives",
      summarizeItems(d.galleries_and_archives.map(g => g.name)),
      d.galleries_and_archives.map(g => el("div", {class:"item"},
        g.url ? el("a", {href:g.url, target:"_blank", rel:"noopener"}, g.name) : el("b", {}, g.name),
      )),
    ));
  }
  if (d.canonical_creators && d.canonical_creators.length) {
    groups.push(dossierSection(
      "canonical creators",
      summarizeItems(d.canonical_creators.map(c => c.name)),
      d.canonical_creators.map(c => el("div", {class:"item"}, el("b", {}, c.name), c.why_notable ? el("span", {class:"why"}, " — " + c.why_notable) : "")),
    ));
  }
  if (d.adjacent_themes && d.adjacent_themes.length) {
    groups.push(dossierSection(
      "adjacent themes",
      summarizeItems(d.adjacent_themes),
      [el("div", {class:"pill-row"}, ...d.adjacent_themes.map(t => el("span", {class:"pill"}, t)))],
    ));
  }
  if (d.suggested_queries && d.suggested_queries.length) {
    groups.push(dossierSection(
      "queries the agent will try",
      summarizeItems(d.suggested_queries),
      [el("div", {class:"pill-row"}, ...d.suggested_queries.map(q => el("span", {class:"pill"}, q)))],
    ));
  }
  if (d.cultural_context) {
    groups.push(dossierSection(
      "cultural / artistic context",
      firstSentence(d.cultural_context),
      [el("div", {class:"summary"}, d.cultural_context)],
    ));
  }
  groups.push(el("div", {style:"margin-top:14px"},
    el("button", {onClick: () => buildDossier(id)}, "↻ refresh dossier"),
  ));
  sec.appendChild(el("div", {class:"card dossier"}, ...groups));
}

async function buildDossier(id) {
  const btn = document.getElementById("build-dossier-btn") || event?.target;
  if (btn) { btn.disabled = true; btn.replaceChildren(el("span", {class:"spinner"}), document.createTextNode("researching…")); }
  try { await api.post(`/mindset/${id}/dossier`); }
  catch (e) { alert(e.message); }
  await reloadMindset(id);
}

function renderTile(c) {
  const liked = c.status === "liked";
  const rights = RIGHTS_LABEL[c.rights_status] || RIGHTS_LABEL.unknown;
  const t = el("div", {class: "tile" + (liked ? " liked" : "")});
  if (c.is_new) t.appendChild(el("div", {class:"new-badge"}, "NEW"));
  t.appendChild(el("img", {src: c.thumbnail_url || c.image_url, loading: "lazy", referrerpolicy: "no-referrer"}));
  t.appendChild(el("div", {class:"meta"},
    el("div", {class:"title"}, truncate(c.title || "(untitled)", 90)),
    el("div", {class:"src"},
      el("a", {class:"src-link", href: c.source_page_url || c.image_url, target:"_blank", rel:"noopener"},
        `${sourceName(c.source_id)} · ${c.creator || "unknown"}`,
        el("span", {class:"ext"}, "↗"),
      ),
    ),
    el("div", {class:"score"}, c.judge_score != null ? `score ${c.judge_score.toFixed(1)}` : "unscored"),
    el("div", {class:"reason"}, c.judge_reason ? `“${truncate(c.judge_reason, 140)}”` : ""),
    el("div", {style:"margin-top:6px"}, el("span", {class: "pill " + rights.cls, title: rights.title}, rights.text)),
    el("div", {class:"attribution"}, c.attribution || ""),
  ));
  t.appendChild(el("div", {class:"actions"},
    el("button", {class: liked ? "like-on" : "", onClick: () => fb(c, liked ? "unlike" : "like")}, liked ? "♥ liked" : "♡ like"),
    el("button", {onClick: () => fb(c, "dislike")}, "✕ remove"),
  ));
  return t;
}

async function fb(c, kind) {
  console.log("fb:", kind, "img=", c.id);
  try {
    const send = kind === "unlike" ? {mindset_id: c.mindset_id, kind: "hide", image_id: c.id, note: "unliked"}
                                    : {mindset_id: c.mindset_id, kind, image_id: c.id};
    await api.post("/feedback", send);
  } catch (e) { console.error("fb failed:", e); alert("feedback failed: " + e.message); return; }
  await reloadMindset(c.mindset_id);
}

async function saveDirection(id) {
  const txt = document.getElementById("direction").value.trim();
  if (!txt) return;
  try { await api.post("/feedback", {mindset_id: id, kind: "direction", direction_text: txt}); document.getElementById("direction").value = ""; }
  catch (e) { alert(e.message); }
  await reloadMindset(id);
}

let huntPollTimer = null;

let PUB_MODE = "liked";  // all | liked | top
const TOP_N = 12;        // "top scorers" = the highest-scored dozen

function selectedCandidates(mode, cs) {
  if (mode === "all") return cs.slice();
  if (mode === "liked") return cs.filter(c => c.status === "liked");
  // top scorers: best-scored first, capped at TOP_N
  return cs.filter(c => c.judge_score != null).sort((a, b) => (b.judge_score || 0) - (a.judge_score || 0)).slice(0, TOP_N);
}

function renderPublish(id, m, cs) {
  const sec = document.getElementById("publish-sec");
  if (!sec) return;
  cs = cs || [];
  const counts = {
    all: cs.length,
    liked: selectedCandidates("liked", cs).length,
    top: selectedCandidates("top", cs).length,
  };
  // fall back to a non-empty selection if the current mode has nothing
  if (counts[PUB_MODE] === 0) PUB_MODE = counts.liked ? "liked" : counts.top ? "top" : "all";
  const sel = selectedCandidates(PUB_MODE, cs);
  const connected = !!m.publish_installation_id;

  const segBtn = (mode, label) =>
    el("button", {class: PUB_MODE === mode ? "on" : "", onClick: () => { PUB_MODE = mode; renderPublish(id, m, cs); }}, label);
  const seg = el("div", {class:"seg-row"},
    segBtn("all", `add all (${counts.all})`),
    segBtn("liked", `add liked (${counts.liked})`),
    segBtn("top", `add top scorers (${counts.top})`),
  );

  let grid;
  if (!sel.length) {
    grid = el("div", {class:"pub-empty"}, "nothing in this selection yet — like some images, or run a hunt.");
  } else {
    grid = el("div", {class:"pub-grid"});
    const shown = sel.slice(0, 11);
    for (const c of shown) grid.appendChild(el("img", {src: c.thumbnail_url || c.image_url, loading:"lazy", referrerpolicy:"no-referrer", title: c.title || ""}));
    if (sel.length > shown.length) grid.appendChild(el("div", {class:"more"}, `+${sel.length - shown.length}`));
  }

  const pubBtn = el("button", {class:"primary", id:"publish-btn", onClick: () => publishSelection(id, sel)},
    sel.length ? `↗ publish ${sel.length} to design xdm` : "nothing to publish");
  pubBtn.disabled = !sel.length;

  const autoCheckbox = el("input", {type:"checkbox", id:"autopub-check"});
  autoCheckbox.checked = connected;
  autoCheckbox.addEventListener("change", () => onAutoPublishToggle(id, autoCheckbox.checked));
  const autoLabel = el("label", {class:"checkbox-row", for:"autopub-check"},
    autoCheckbox,
    el("span", {}, "auto-publish on a schedule"),
    el("span", {class:"hint"}, connected ? "· connected · scheduled runs coming soon" : "· connects your design xdm account"),
  );

  const foot = el("div", {class:"pub-foot"}, pubBtn, autoLabel);
  if (connected) foot.appendChild(el("button", {id:"disconnect-btn", onClick: () => disconnectInstallation(id)}, "disconnect"));

  sec.replaceChildren(el("h2", {}, "publish"),
    el("div", {class:"card"},
      el("div", {class:"theme", style:"margin-bottom:10px"}, "publish a board to design xdm — pick what to include:"),
      seg, grid, foot,
    )
  );
}

async function publishSelection(id, sel) {
  if (!window.agentAuth?.current()) { alert("sign in to the agent first (top-right) to publish."); return; }
  if (!sel.length) return;
  const btn = document.getElementById("publish-btn");
  if (!btn) return;
  btn.disabled = true;
  btn.replaceChildren(el("span", {class:"spinner"}), document.createTextNode("publishing…"));
  try {
    const m = await api.get(`/mindset/${id}`);
    const r = await api.post(`/publish/mindset/${id}`, { board_name: m.name, image_ids: sel.map(c => c.id), max_images: 200 });
    // Swap the button for a green "published" status + a board link the user can pick.
    const count = r.image_count != null ? r.image_count : sel.length;
    const done = el("button", {class:"primary published"}, `✓ published ${count}`);
    const after = [done];
    if (r.board_url) after.push(el("a", {class:"published-link", href: r.board_url, target:"_blank", rel:"noopener"}, "open board ↗"));
    btn.replaceWith(...after);
  } catch (e) {
    btn.disabled = false;
    btn.classList.remove("published");
    btn.textContent = `↗ publish ${sel.length} to design xdm`;
    alert("publish failed: " + e.message);
  }
}

async function onAutoPublishToggle(id, checked) {
  if (checked) await connectInstallation(id);
  else await disconnectInstallation(id, true);
}

async function connectInstallation(id) {
  if (!window.agentAuth?.current()) {
    alert("sign in to the agent first (top-right), then turn on auto-publish.");
    await reloadMindset(id);  // resync the checkbox
    return;
  }
  try {
    const scopes = ["board.create"];
    const result = window.agentAuth.isEmbedded
      ? await window.agentAuth.requestInstall(AGENT_ID, scopes)
      : await window.agentAuth.connectViaPopup(AGENT_ID, scopes);
    if (!result || !result.installation_id) throw new Error("no installation id returned");
    await api.post(`/installations/${id}`, { installation_id: result.installation_id });
  } catch (e) {
    const msg = String(e.message || e);
    if (msg !== "cancelled" && msg !== "popup closed") alert("connect failed: " + msg);
  } finally {
    await reloadMindset(id);  // reflect real connected state (and reset checkbox on cancel)
  }
}

async function disconnectInstallation(id, skipConfirm) {
  if (!skipConfirm && !confirm("Disconnect this mindset from design xdm? The agent will no longer be able to publish on your behalf.")) {
    await reloadMindset(id);
    return;
  }
  try {
    await api.del(`/installations/${id}`);
  } catch (e) {
    alert("disconnect failed: " + e.message);
  } finally {
    await reloadMindset(id);
  }
}

async function hunt(id) {
  const btn = document.getElementById("hunt-btn");
  if (btn) { btn.disabled = true; btn.replaceChildren(el("span", {class:"spinner"}), document.createTextNode("hunting…")); }
  const feed = document.getElementById("feed");
  if (feed) feed.replaceChildren(el("div", {class:"feed-loading"}, el("span", {class:"spinner"}), "starting hunt…"));

  let hunt_id;
  try {
    const r = await api.post("/hunt", {mindset_id: id});
    hunt_id = r.hunt_id;
  } catch (e) { notifyError(e); resetHuntButton(); return; }

  let lastTraceLen = -1;
  const poll = async () => {
    try {
      const h = await api.get(`/hunt/${id}/${hunt_id}`);
      const f = document.getElementById("feed");
      if (f && (h.trace?.length || 0) !== lastTraceLen) {
        lastTraceLen = h.trace?.length || 0;
        renderProgress(h, f);
      }
      if (h.status === "completed" || h.status === "failed") {
        if (huntPollTimer) { clearInterval(huntPollTimer); huntPollTimer = null; }
        resetHuntButton();
        // brief pause so the user sees the final "done" line, then reload feed
        setTimeout(() => reloadMindset(id), 900);
      }
    } catch (e) {
      console.warn("poll failed:", e);
    }
  };
  if (huntPollTimer) clearInterval(huntPollTimer);
  huntPollTimer = setInterval(poll, 1500);
  poll();
}

function resetHuntButton() {
  const btn = document.getElementById("hunt-btn");
  if (btn) { btn.disabled = false; btn.textContent = "hunt now"; }
}

const STEP_LABEL = {
  queued:           s => ["queued", "sub"],
  start:            s => [`start  (mindset v${s.mindset_version})`, "sub"],
  reflecting:       s => ["rewriting rubric from your feedback…", ""],
  reflected:        s => [`✓ rubric rewritten → v${s.new_version}`, "done"],
  reflect_skipped:  s => [`no rubric rewrite — ${s.reason || ""}`, "sub"],
  planning:         s => ["scout is planning a hunt…", ""],
  planned:          s => [`✓ plan: ${s.n_searches} searches — “${(s.reasoning||"").slice(0,180)}${(s.reasoning||"").length>180?"…":""}”`, "done"],
  searching:        s => [`fanning out to ${s.n_searches} sources in parallel…`, ""],
  source_done:      s => [`↳ ${s.source_id} · “${s.query}” → ${s.found} found (${s.done}/${s.of})`, s.found ? "sub" : "warn"],
  searched:         s => [`✓ search complete: ${s.n_raw} raw candidates`, "done"],
  deduping:         s => [`deduping ${s.of} candidates (perceptual hash)…`, ""],
  deduped:          s => [`✓ ${s.n_unique} unique  (${s.removed} dropped as dupes)`, "done"],
  checking_rights:  s => [`licence check on ${s.of} candidates…`, ""],
  rights_checked:   s => [`✓ ${s.n_cleared} cleared  (${s.n_dropped} dropped: no licence)`, "done"],
  judging:          s => [`Gemini judging ${s.of} images against the rubric…`, ""],
  judge_progress:   s => [`↳ judged ${s.done}/${s.of}`, "sub"],
  judged:           s => [`✓ judged ${s.n_judged}  ·  avg ${s.avg_score}  ·  ${s.above_threshold} ≥ ${7.0}`, "done"],
  curating:         s => ["curating: diversity + ranking…", ""],
  curated:          s => [`✓ ${s.n_kept} kept`, "done"],
  done:             s => [`✓ done — ${s.n_kept} new images in ${(s.duration_ms/1000).toFixed(1)}s`, "done"],
  error:            s => [`✕ error: ${s.error}`, "err"],
};

function renderProgress(h, container) {
  container.replaceChildren();
  const card = el("div", {class:"card progress-card"});
  const last = (h.trace || [])[h.trace.length - 1] || {};
  const inFlight = h.status !== "completed" && h.status !== "failed";
  card.appendChild(el("div", {class:"progress-head"},
    inFlight ? el("span", {class:"spinner"}) : el("span", {}, h.status === "failed" ? "✕" : "✓"),
    el("strong", {}, inFlight ? "hunting…" : (h.status === "failed" ? "hunt failed" : "hunt complete")),
    el("span", {class:"now"}, inFlight && last.step ? `· ${last.step.replace(/_/g, " ")}` : ""),
  ));
  const log = el("div", {class:"trace"});
  for (const step of (h.trace || [])) {
    const fn = STEP_LABEL[step.step] || (s => [`${s.step}: ${JSON.stringify(s).slice(0, 120)}`, "sub"]);
    const [text, cls] = fn(step);
    const t = (step.t || "").slice(11, 19);
    log.appendChild(el("div", {class: "step " + (cls || "")}, `[${t}] ${text}`));
  }
  card.appendChild(log);
  container.appendChild(card);
}

async function renderTrace(id) {
  $app.replaceChildren();
  $app.appendChild(el("div", {}, el("a", {href:`#/mindset/${id}`}, "← back to mindset")));
  $app.appendChild(el("h2", {}, "most recent hunt trace"));
  try {
    const hs = await api.get(`/hunts/${id}`);
    if (!hs.length) { $app.appendChild(el("div", {class:"empty"}, "no hunts")); return; }
    const h = hs[0];
    const card = el("div", {class:"card trace"});
    for (const step of h.trace || []) {
      const ln = el("div", {class:"step"},
        `[${step.t?.slice(11,19) || ""}] `, step.step + ": ",
        JSON.stringify({...step, t: undefined, step: undefined}),
      );
      card.appendChild(ln);
    }
    $app.appendChild(card);
  } catch (e) { $app.appendChild(el("div", {class:"empty"}, e.message)); }
}

function renderUser(u) {
  if (!$userSlot) return;
  $userSlot.replaceChildren();
  if (window.agentAuth?.isEmbedded) {
    if (u) $userSlot.appendChild(el("span", {class:"user-info"}, "as ", el("b", {}, u.email || u.displayName || "you")));
    return;
  }
  if (u) {
    $userSlot.appendChild(el("span", {class:"user-info"}, "signed in as ", el("b", {}, u.email || u.displayName || "you")));
    $userSlot.appendChild(el("button", {onClick: () => window.agentAuth.signOut()}, "sign out"));
  } else {
    $userSlot.appendChild(el("button", {class:"primary", onClick: () => window.agentAuth.signIn().catch(e => alert(e.message))}, "sign in with design xdm"));
  }
}

async function bootstrap() {
  if (!window.agentAuth) {
    await new Promise(r => window.addEventListener("agent-auth-ready", r, { once: true }));
  }
  await window.agentAuth.firstAuthDecision();
  renderUser(window.agentAuth.current());
  window.agentAuth.onChange((u) => { renderUser(u); route(); });
  await refreshHealth();
  route();
}

window.addEventListener("hashchange", route);
bootstrap();
