const $app = document.getElementById("app");

const api = {
  async get(p) { const r = await fetch(p); if (!r.ok) throw new Error(`${p}: ${r.status}`); return r.json(); },
  async post(p, body) { const r = await fetch(p, {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify(body || {})}); if (!r.ok) { const t = await r.text(); throw new Error(`${p}: ${r.status} ${t.slice(0,200)}`);} return r.json(); },
};

const RIGHTS_LABEL = {
  clear:   { text: "free to use",     cls: "good", title: "Licensed for any use; attribution preserved." },
  caveat:  { text: "credit required", cls: "warn", title: "Editorial use and/or attribution required — full string saved with the image." },
  unknown: { text: "rights unknown",  cls: "",     title: "License could not be confirmed — surfaced only if explicitly enabled." },
};

let SOURCES = {};

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
        el("input", {type:"text", id:"name", value:"m", placeholder:"name (e.g. 'microscopy feed')"}),
        el("input", {type:"text", id:"theme", value:"microscopy", placeholder:"theme phrase (e.g. 'microscopy')"}),
        el("button", {class:"primary", id:"create-btn", onClick: createMindset}, "create"),
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
  } catch (e) { alert(e.message); btn.disabled = false; btn.textContent = "create"; }
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
      ),
    )
  ));
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
    el("div", {class:"src"}, `${sourceName(c.source_id)} · ${c.creator || "unknown"}`),
    el("div", {class:"score"}, c.judge_score != null ? `score ${c.judge_score.toFixed(1)}` : "unscored"),
    el("div", {class:"reason"}, c.judge_reason ? `“${truncate(c.judge_reason, 140)}”` : ""),
    el("div", {style:"margin-top:6px"}, el("span", {class: "pill " + rights.cls, title: rights.title}, rights.text)),
    el("div", {class:"attribution"}, c.attribution || ""),
  ));
  t.appendChild(el("div", {class:"actions"},
    el("button", {class: liked ? "like-on" : "", onClick: () => fb(c, liked ? "unlike" : "like")}, liked ? "♥ liked" : "♡ like"),
    el("button", {onClick: () => fb(c, "dislike")}, "✕ remove"),
    el("button", {onClick: () => window.open(c.source_page_url || c.image_url, "_blank")}, "↗ source"),
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

async function hunt(id) {
  const btn = document.getElementById("hunt-btn");
  if (btn) { btn.disabled = true; btn.replaceChildren(el("span", {class:"spinner"}), document.createTextNode("hunting…")); }
  const feed = document.getElementById("feed");
  if (feed) feed.replaceChildren(el("div", {class:"feed-loading"}, el("span", {class:"spinner"}), "starting hunt…"));

  let hunt_id;
  try {
    const r = await api.post("/hunt", {mindset_id: id});
    hunt_id = r.hunt_id;
  } catch (e) { alert("hunt start failed: " + e.message); resetHuntButton(); return; }

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

window.addEventListener("hashchange", route);
refreshHealth().then(route);
