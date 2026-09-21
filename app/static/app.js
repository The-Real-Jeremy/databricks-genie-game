/* THE GENIE BAKE-OFF — four weeks, five blanks each, one genie.
 *
 * No framework, no bundler, no CDN. The whole client is this file plus sprites.js.
 *
 * ⏱ The clock lives on the SERVER; this file only tells it when the player is actually looking. A tick
 * is sent every `tick_every_s` seconds and ONLY while the tab is visible and the open week is the one on
 * screen — so closing the laptop simply stops the ticks, and the server credits nothing for the gap.
 */
"use strict";
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const S = {
  game: null, week: null, session: null, conversation: null,
  theme: null, themeName: null, lamp: null, scene: null, sceneName: null,
  ticker: null, asking: false,
  // the anchor the visible clock is drawn from, and the paint loop that draws it.
  clock: null, clockPaint: null,
  // which leaderboard tab is showing — "all", or a week number.
  lbTab: "all",
};

async function api(path, body, timeoutMs) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ session_id: S.session }, body)) }
    : {};
  // ⛔ THE CLIENT HAD NO TIMEOUT AT ALL. greet() awaited fetch and would wait as long as the server did —
  //    so the server's own bound was the only bound, and when that was exceeded the lamp simply kept
  //    thinking. A health check that can hang forever is not a health check.
  let ctl = null, timer = null;
  if (timeoutMs && typeof AbortController === "function") {
    ctl = new AbortController();
    opt.signal = ctl.signal;
    timer = setTimeout(() => ctl.abort(), timeoutMs);
  }
  let r;
  try {
    r = await fetch(path, opt);
  } catch (e) {
    if (timer) clearTimeout(timer);
    if (ctl && ctl.signal.aborted) {
      const err = new Error("no answer within " + Math.round(timeoutMs / 1000) + "s");
      err.timedOut = true;
      throw err;
    }
    throw e;
  }
  if (timer) clearTimeout(timer);
  let d = null;
  try { d = await r.json(); } catch { d = null; }
  if (!r.ok || (d && d.ok === false && d.error && !d.verdict)) {
    const e = new Error((d && d.error) || `HTTP ${r.status}`);
    e.payload = d; e.status = r.status;
    throw e;
  }
  return d;
}

/* ------------------------------------------------------------------ theme */
function applyTheme(theme) {
  S.theme = theme; S.themeName = theme.name;
  const root = document.documentElement;
  Object.entries(theme.tokens || {}).forEach(([k, v]) => root.style.setProperty("--" + k, v));
  $(".wm-main").textContent = theme.wordmark || "THE GENIE BAKE-OFF";
  $(".wm-sub").textContent = theme.wordmark_sub || "";
  try { localStorage.setItem("dd.theme", theme.name); } catch { /* private window */ }
}

/* ------------------------------------------------------------------ boot */
async function boot() {
  // ⭐ FIRST, before the await. The overlay exists to cover a first impression this project
  //    measured at ~4.9s, so a loading animation created after the payload arrives would animate an
  //    empty room. Same stage as the play page, on the transparent art.
  startBootLamp();
  const g = await api("/api/game");
  S.game = g;
  applyTheme(g.theme);
  let saved = null;
  try { saved = localStorage.getItem("dd.theme"); } catch { /* ignore */ }
  if (saved && saved !== g.theme.name) {
    try { applyTheme(await api("/api/theme?name=" + encodeURIComponent(saved))); } catch { /* ignore */ }
  }
  // the viewer's email, not their first name. `email` is already in the viewer payload, so this
  // needs no server change. Fall back through the other identity fields rather than printing
  // nothing — an empty corner reads as a broken header.
  $("#whoName").textContent = g.viewer.email || g.viewer.name || g.viewer.user_key || "";
  $$("[data-view='operator']").forEach(b => { b.hidden = !g.is_operator; });
  S.week = g.active_week;

  S.lamp = window.Sprites.lampStage($("#lampCanvas"));
  newChat();
  renderStarters(g.season.starters || []);
  render();

  // ⛔ The overlay going away does NOT stop its stage: ImageStage holds a rAF, a visibilitychange
  //    listener, a resize listener and a ResizeObserver. Hidden is not destroyed.
  stopBootLamp();
  $("#boot").hidden = true;
  $(".topbar").hidden = false;
  $("main").hidden = false;

  try { S.session = (await api("/api/session", { week: S.week })).session_id; } catch { /* ok */ }
  startTicker();
  startClockPaint();
  // ⭐ ASK GENIE SOMETHING IMMEDIATELY, FRESH, EVERY OPEN. After first paint and never before it: the page
  //    is already usable while this runs, which is what keeps it feeling quick. It is also a live health
  //    check, so its FAILURE has to be visible — see greet().
  greet();
}

/* ⭐ the loading animation above "Waking the genie…".

   It is the PLAY-PAGE stage, deliberately: `Sprites.lampStage` on `genie-lamp-nostar.png` +
   `genie-star.png`, both measured RGBA with a fully transparent background (71.4% and 68.3% of pixels at
   alpha 0, all four corners 0), so "clear background" needed no new asset — the overlay's own `--bg`
   shows through the canvas. A second, boot-only lamp would have been a thing that could drift from the
   one he was pointing at.

   State `thinking`: the flame flickers and the star bobs. That is the honest reading of "Waking the
   genie" — it is not talking to you yet. */
function startBootLamp() {
  const c = $("#bootCanvas");
  if (!c || !window.Sprites) return;
  try {
    S.bootLamp = window.Sprites.lampStage(c);
    S.bootLamp.show("thinking");
  } catch (e) {
    // A boot screen that throws is worse than one without a lamp: the bar and the line still say
    // "loading", so swallow it rather than replacing the whole overlay with an error.
    S.bootLamp = null;
  }
}

function stopBootLamp() {
  if (S.bootLamp) { S.bootLamp.destroy(); S.bootLamp = null; }
}

/* ------------------------------------------------------------------ render */
function render() {
  const g = S.game;
  const warn = $("#stateWarn");
  warn.hidden = g.state_ok !== false;
  warn.textContent = g.state_warning || "";
  renderHome(g);
  renderWeeks(g);
  const w = g.weeks.find(x => x.week === S.week) || g.weeks[0];
  renderWidgets(w);
  renderChallenge(g, w);
  renderClock(w);
  $("#whoPts").textContent = `${g.total_points} pts · ${g.total_solved}/${g.total_blanks}`;
}

/* ------------------------------------------------------------------ the home page */
function renderHome(g) {
  // The greeting is the FIRST NAME — "Welcome, Sam!". The email is in the top-right corner, which is a different question from how to greet somebody.
  const first = (g.viewer.first_name || "").trim()
             || (g.viewer.name || "").trim().split(/\s+/)[0]
             || "";
  $("#homeGreeting").textContent = first ? `Welcome, ${first}!` : "Welcome!";

  // the operator's client message. `hidden` unless there is something to say — the requirement was
  // an empty setting to render nothing, and an empty <p> still occupies its line-height and margin.
  // ⛔ textContent, never innerHTML: one operator types this and every player's page renders it.
  const cm = $("#homeClientMsg");
  const cmText = (g.client_message || "").trim();
  cm.textContent = cmText;
  cm.hidden = !cmText;

  // What the game IS, in the season's own words, so the home page cannot drift from the content pack.
  const se = g.season || {};
  renderLead(se.premise || "");
  $("#homeSub").textContent = se.subtitle || "";

  // the four numbers in the Instructions bullets come from the server's own constants, so the
  // copy cannot claim a price the game does not charge. The literals in index.html are a fallback for a
  // failed payload, not the source — and they are the same values, so a stale one is not a lie.
  const sc = g.scoring || {};
  const num = (id, val) => { if (val != null) $(id).textContent = val; };
  num("#hiBase", sc.base);
  num("#hiPerMin", sc.per_min);
  num("#hiFloor", sc.floor);
  num("#hiHint", sc.hint_cost);

  // His own progress, only once there is some — a big "0 points" is a discouraging way to greet
  // somebody who has not started, and it is also indistinguishable from a failed read.
  const box = $("#homeScore");
  const show = g.state_ok !== false && (g.total_points > 0 || g.total_solved > 0);
  box.hidden = !show;
  if (show) {
    $("#homeScorePts").textContent = g.total_points;
    $("#homeScoreSolved").textContent = `${g.total_solved} of ${g.total_blanks} blanks filled`;
  }
  // Nothing started yet: offer the way in instead of an empty half a hero.
  const cta = $("#homeCta");
  const playable = (g.weeks || []).filter(w => w.status !== "locked");
  const next = playable.find(w => !w.done) || playable[0];
  cta.hidden = show || !next;
  if (!cta.hidden) {
    $("#homeCtaNum").textContent = next.week;
    $("#homeCtaNote").textContent = next.status === "available"
      ? "your clock starts when you unlock it" : "pick up where you left off";
    $("#homeCtaBtn").onclick = () => goWeek(next.week);
  }

  // The same honest warning the play view carries: if the read failed, the numbers here are not progress.
  const hw = $("#homeWarn");
  hw.hidden = g.state_ok !== false;
  hw.textContent = g.state_warning || "";

  const rel = (g.weeks || []).filter(w => w.status !== "locked").length;
  $("#homeWeeksNote").textContent = rel
    ? `${rel} of ${g.season.total_weeks} released so far — pick one to play.`
    : "Nothing released yet. The first week will appear here when it opens.";
}

/* ⭐ the home-page copy ends "Find out more here." and `here` has to be the link
   to databricks.com/product/genie/one — the anchor, not a bare URL printed beside it.

   ⛔ BUILT FROM DOM NODES, NEVER innerHTML. The premise is CONTENT, loaded from a season pack, and content
   must not be able to inject markup into this page. textContent on the parts plus one created <a> gives the
   link with no injection surface at all.
   ⛔ AND IT IS THE **LAST** "here", not the first: the copy says "Don't stop here, use Genie to talk to any
   data you work with! Find out more here." Anchoring the first would link the wrong sentence — and it would
   look right in a screenshot, because both words read the same. */
const GENIE_ONE_URL = "https://www.databricks.com/product/genie/one";

function renderLead(text) {
  const el = $("#homeLead");
  el.textContent = "";
  const i = text.lastIndexOf("here");
  if (i < 0) { el.textContent = text; return; }
  el.appendChild(document.createTextNode(text.slice(0, i)));
  const a = document.createElement("a");
  a.href = GENIE_ONE_URL;
  a.target = "_blank";
  a.rel = "noopener noreferrer";        // an external link out of the app
  a.textContent = text.slice(i, i + 4);
  el.appendChild(a);
  el.appendChild(document.createTextNode(text.slice(i + 4)));
}

function renderWeeks(g) {
  const ol = $("#weeks");
  ol.innerHTML = "";
  g.weeks.forEach(w => {
    const li = document.createElement("li");
    // ⛔ A LOCKED WEEK IS NOT A CONTROL. Everything that makes a card operable is now
    //    conditional on its status, because  attached both handlers to EVERY card and left CSS to
    //    say "not a button" with nothing but a cursor. The visible symptom was worse than "it
    //    navigates": goWeek(3) ran, the server clamped the unreleased week away, and the player
    //    landed on WEEK 1 — a different week than the one they clicked, with no explanation.
    const locked = w.status === "locked";
    li.className = `wk wk-${w.status}` + (w.week === S.week ? " on" : "");
    // Mirror a native `<button disabled>` exactly: still announced as a button, explicitly
    // unavailable, and NOT in the tab order. Dropping the role would announce three siblings as
    // buttons and this one as a bare list item, understating what it is — and `aria-disabled` on a
    // generic role is widely ignored, so the role is the thing that carries the state.
    li.setAttribute("role", "button");
    if (locked) li.setAttribute("aria-disabled", "true");
    else li.tabIndex = 0;
    const mark = w.status === "done" ? "✓" : w.status === "locked" ? "🔒" : String(w.week);
    li.innerHTML =
      `<span class="wk-n">${mark}</span>` +
      `<span class="wk-body"><b>${esc(w.title)}</b>` +
      `<i>${w.status === "locked" ? "not released yet"
           : w.status === "available" ? "ready to unlock"
           : w.status === "done" ? `finished · ${w.points} pts`
           : `${w.solved}/${w.of} filled · ${w.points} pts`}</i></span>` +
      `<span class="wk-bar"><b style="width:${Math.round(w.solved / w.of * 100)}%"></b></span>` +
      `<span class="wk-go">${w.status === "locked" ? "" : "Open →"}</span>`;
    // These cards ARE the navigation now, so a click both picks the week and takes you to it.
    // and ONLY when the week is playable. There are TWO listeners and a keyboard path
    // here, not one — removing only the click would have left Enter and Space still navigating.
    if (!locked) {
      const go = () => goWeek(w.week);
      li.addEventListener("click", go);
      li.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    }
    ol.appendChild(li);
  });
}

function renderWidgets(w) {
  const ol = $("#widgets");
  ol.innerHTML = "";
  (w.widgets || []).forEach(q => {
    const li = document.createElement("li");
    li.className = `wg wg-${q.state}`;
    const glyph = q.state === "correct" ? "✓" : q.state === "wrong" ? "✕" : q.n;
    // Plain "Answer 1…5", not the clue's domain label — that was asked for, and the label was the
    // thing making this row read like a second week selector.
    li.innerHTML = `<span class="wg-mark">${glyph}</span>` +
      `<span class="wg-text"><b>Answer ${q.n}</b>` +
      `<i>${q.state === "correct" ? "+" + q.points : q.state === "wrong" ? "not yet" : "empty"}</i></span>`;
    /* the hover reveal is GONE from BOTH places it lived — the `li.title` native tooltip that was
       on this line, and the `.bl-ask` span on the blank — and the question sits behind the Hint button
       instead. That is a deliberate choice, and the reason given was: "the question is already very obvious and acts
       as a hint", which is why nobody has to author twenty hints. */
    const hb = hintButton(q);
    if (hb) li.appendChild(hb);
    if (w.unlocked) {
      li.tabIndex = 0;
      li.addEventListener("click", () => focusBlank(q.clue_id));
      li.addEventListener("keydown", e => { if (e.key === "Enter") focusBlank(q.clue_id); });
    }
    ol.appendChild(li);
  });
}

/* ══════════════════════════════════════════════════════════════════════════════════════════════════
   THE HINT BUTTON, ITS DISABLED STATE, AND THE POPUP

   ⛔ EVERY VALUE HERE IS READ OFF THE PAYLOAD AND NOTHING IS COMPUTED. The penalty and the
   disabled-once-correct rule belong to the data lane, because a -5 that lives in the page is wiped by a
   refresh and the button comes back. The contract, on the widget AND the blank:

     hint_cost     what a FIRST reveal costs. THE LABEL IS RENDERED FROM THIS, never from a literal 5,
                   so the button cannot promise a price the server does not charge.
     hint_taken    this player already revealed this one — a recorded event, survives a reload.
     hint_penalty  points already deducted, 0 or 5: the number the arithmetic USED, not a recomputation.
     hint_enabled  whether a click is accepted. FALSE if and only if the blank is SOLVED. ⚠️ It stays
                   TRUE while hint_taken is true, because a second look is free and re-opens the popup.

   ⚠️ A LEGACY VOCABULARY LIVES ONE CHARACTER AWAY. `app/lib/content.py` already carries `hints_taken`,
   `hints_available` and `next_hint_cost` from the archived 8-week packs. `hints_taken` and `hint_taken`
   differ by a single letter, so the two sets are NOT interchangeable — only the four above are this
   contract, and a grep for "hint" will show you both.

   ⭐ AND IF THE FIELDS ARE ABSENT THERE IS NO BUTTON — fail closed, because this control SPENDS the
   player's points and a button whose price the server has not declared must not be clickable. The
   absence is announced to the console ONCE, so "it never appeared" cannot be mistaken for "it is off".
   ══════════════════════════════════════════════════════════════════════════════════════════════════ */
const hintInflight = new Set();
let hintFieldsWarned = false;

function hintButton(q) {
  if (!q || q.hint_cost == null) {
    if (!hintFieldsWarned) {
      hintFieldsWarned = true;
      console.warn("[hint] no hint_cost on the widget payload — the Hint button is withheld until the " +
                   "server sends hint_cost/hint_taken/hint_penalty/hint_enabled.");
    }
    return null;
  }
  const b = document.createElement("button");
  b.type = "button";
  b.className = "wg-hint";
  // The label states the charge THIS click makes: the cost on a first reveal, nothing on a re-open.
  b.textContent = q.hint_taken ? "Hint" : `Hint (-${q.hint_cost})`;
  b.dataset.clue = q.clue_id || "";
  b.dataset.hintTaken = q.hint_taken ? "1" : "0";
  b.dataset.hintCost = String(q.hint_cost);
  if (q.hint_penalty != null) b.dataset.hintPenalty = String(q.hint_penalty);
  if (q.hint_enabled === false) {
    b.disabled = true;
    b.dataset.hintDisabled = "solved";
    // The rule: disabled once the answer is right, whether or not the hint was revealed earlier.
    b.title = "Answered — no hint needed";
  }
  b.addEventListener("click", e => {
    e.stopPropagation();          // the chevron focuses the blank; one click must not do both
    if (!b.disabled) revealHint(b.dataset.clue, b);
  });
  // Enter/Space on the button must not also reach the chevron's keydown handler.
  b.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") e.stopPropagation(); });
  return b;
}

async function revealHint(clueId, btn) {
  if (!clueId || hintInflight.has(clueId)) return;
  hintInflight.add(clueId);
  btn.classList.add("wg-hint-busy");
  try {
    const r = await api("/api/hint", { clue_id: clueId, week: S.week });
    showHintPopup(r, btn);
    /* The score is the server's arithmetic, so it is re-read rather than adjusted here. Same defensive
       shape as submitBlank: a response that omits `weeks` degrades to a refetch, never to an exception. */
    if (Array.isArray(r.weeks)) S.game.weeks = r.weeks;
    if (typeof r.total_points === "number") S.game.total_points = r.total_points;
    if (!Array.isArray(r.weeks)) {
      try { S.game = await api("/api/game?week=" + S.week); } catch { /* keep what we have */ }
    }
    render();
  } catch (e) {
    showHintPopup({ error: (e && e.message) || "Could not fetch that hint." }, btn);
  } finally {
    hintInflight.delete(clueId);
    btn.classList.remove("wg-hint-busy");
  }
}

let hintKeyHandler = null;
let hintAwayHandler = null;

function closeHintPopup() {
  const el = document.getElementById("hintPop");
  if (el) el.remove();
  if (hintKeyHandler) { document.removeEventListener("keydown", hintKeyHandler, true); hintKeyHandler = null; }
  if (hintAwayHandler) { document.removeEventListener("mousedown", hintAwayHandler, true); hintAwayHandler = null; }
}

/* The popup CHROME. ⛔ What it SAYS is the server's: the reveal text is read from the response and is
   never composed, cached or defaulted here. The question itself is the hint, so there is no
   hint copy in this repo that could drift from the content pack — and if the response carries no text,
   this says so rather than showing an empty box, because an empty box reads as a broken feature. */
function showHintPopup(r, anchor) {
  closeHintPopup();
  const wrap = document.createElement("div");
  wrap.id = "hintPop";
  wrap.className = "hintpop";
  wrap.setAttribute("role", "dialog");
  wrap.setAttribute("aria-label", "Hint");
  wrap.innerHTML =
    `<div class="hp-head"><b>Hint</b><button type="button" class="hp-x" aria-label="Close hint">✕</button></div>` +
    `<p class="hp-body"></p><p class="hp-foot"></p>`;
  const body = wrap.querySelector(".hp-body");
  const foot = wrap.querySelector(".hp-foot");
  /* ⛔ THE FIELD IS `hint`, NOT `ask`, AND I HAD IT THE OTHER WAY ROUND. The contract reached me as
     "carrying the question text" and I assumed `ask`; the server returns
     `"hint": clue.get("ask")` — the value is the clue's question, but the KEY is `hint`. This worked
     only because the fallback was written defensively before either half existed. The server's real
     name is read FIRST now, and `ask` stays as the fallback rather than being deleted, because a
     primary read of a key that does not exist is one edit away from silently showing the
     no-text branch. Verified against origin/main's app.py, not against the relay. */
  const text = r && (r.hint != null ? r.hint : r.ask);
  const charged = r && typeof r.hint_penalty === "number" ? r.hint_penalty : null;
  if (r && r.error) {
    wrap.classList.add("hp-err");
    body.textContent = r.error;
  } else if (text == null || String(text) === "") {
    wrap.classList.add("hp-err");
    body.textContent = "The server did not send hint text for this answer.";
  } else {
    body.textContent = String(text);
  }
  if (r && r.error) foot.textContent = "";
  else if (r && r.already) {
    foot.textContent = charged
      ? `Already revealed — ${charged} points were deducted earlier. This look is free.`
      : "Already revealed. This look is free.";
  } else if (charged != null) {
    foot.textContent = charged ? `${charged} points deducted for this answer.` : "No points deducted.";
  } else foot.textContent = "";
  wrap.dataset.hintAlready = r && r.already ? "1" : "0";
  if (charged != null) wrap.dataset.hintPenalty = String(charged);
  document.body.appendChild(wrap);

  // Placed under the button it belongs to, then clamped so it cannot push the page sideways — the same
  // mistake `.bl-ask` made, which put 52px of horizontal scroll on every view for something invisible.
  const rc = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;
  const pw = wrap.offsetWidth || 260;
  if (rc) {
    let left = rc.left + rc.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, document.documentElement.clientWidth - pw - 8));
    wrap.style.left = `${Math.round(left)}px`;
    wrap.style.top = `${Math.round(rc.bottom + 6)}px`;
  } else {
    wrap.style.left = "50%";
    wrap.style.top = "20%";
  }
  wrap.querySelector(".hp-x").addEventListener("click", closeHintPopup);
  /* Capture-phase, and the ONLY Escape listener in this app — checked, because two overlays both
     listening on the document means one Escape closes both. It is removed on close, so there is never a
     second one to collide with. */
  hintKeyHandler = e => { if (e.key === "Escape") { e.stopPropagation(); closeHintPopup(); } };
  document.addEventListener("keydown", hintKeyHandler, true);
  hintAwayHandler = e => { if (!wrap.contains(e.target)) closeHintPopup(); };
  document.addEventListener("mousedown", hintAwayHandler, true);
  const x = wrap.querySelector(".hp-x");
  if (x && x.focus) x.focus();
}

function renderChallenge(g, w) {
  const c = g.case;
  $("#weekBadge").textContent = `WEEK ${w.week} OF ${g.season.total_weeks}`;
  $("#weekTitle").textContent = c ? c.title : w.title;
  $("#challengeLine").textContent = c ? (c.challenge || "") : "";
  $("#storyLine").textContent = c ? (c.story || "") : "";

  const sceneName = (c && c.scene) || "receipts";
  if (S.sceneName !== sceneName) {
    if (S.scene) S.scene.destroy();      // not just stop(): a stopped stage keeps its resize listeners
    S.scene = window.Sprites.sceneStage($("#sceneCanvas"), sceneName);
    S.sceneName = sceneName;
  }

  const open = w.status === "open" || w.status === "done";
  $("#memo").hidden = !open;
  $("#lockedPanel").hidden = open;
  if (!open) {
    const avail = w.status === "available";
    $("#lockedMsg").textContent = avail
      ? "This week is ready. The clock starts the moment you unlock it — and it pauses whenever you are looking somewhere else."
      : "This week has not been released yet. It will open up here when it does.";
    $("#unlockBtn").hidden = !avail;
    $("#unlockNum").textContent = w.week;
    $("#unlockNote").textContent = avail
      ? `Each blank is worth ${g.scoring.base} points, less ${g.scoring.per_min} for every whole minute you spend on the week, never below ${g.scoring.floor}. Wrong answers cost nothing.`
      : "";
    return;
  }
  renderMemo(g, c, w);
}

function renderMemo(g, c, w) {
  if (!c || !c.letter) return;
  $("#memoTo").textContent = c.letter.to;
  $("#memoSubject").textContent = c.letter.subject;
  const body = $("#memoBody");
  body.innerHTML = "";
  c.letter.paras.forEach(para => {
    const p = document.createElement("p");
    para.forEach(part => {
      if (part.t !== undefined) { p.appendChild(document.createTextNode(part.t)); return; }
      p.appendChild(blankEl(part));
    });
    body.appendChild(p);
  });
  const done = w.done;
  $("#memoFoot").hidden = !done;
  if (done) $("#memoFootMsg").textContent =
    `All five blanks filled — ${w.points} points for week ${w.week}.`;
}

function blankEl(b) {
  const wrap = document.createElement("span");
  wrap.className = "bl" + (b.solved ? " bl-ok" : "");
  wrap.dataset.clue = b.blank;
  const n = document.createElement("sup");
  n.className = "bl-n";
  n.textContent = b.n;
  if (b.solved) {
    const v = document.createElement("b");
    v.className = "bl-val";
    v.textContent = b.value;
    wrap.appendChild(v);
    wrap.appendChild(n);
    // the little superscript "+40" beside each filled answer is gone by request. The blank's
    // INDEX superscript (.bl-n) stays — it is a different element and it is what ties a blank to the
    // answer chevron above the memo. The points are still on the chevron and in the header total.
    return wrap;
  }
  const inp = document.createElement("input");
  inp.type = "text";
  inp.className = "bl-in";
  inp.size = Math.max(6, Math.min(22, b.width || 10));
  inp.placeholder = "?";
  /* ⛔ THIS LABEL USED TO BE `b.ask`, AND THAT WOULD HAVE LEFT THE PRICED TEXT FREE. Once the question
     costs points, handing it to the accessibility tree as the input's accessible name gives it away to
     any screen reader for nothing — the same leak as the two hover mechanisms, in the one place nobody
     looks. It is also the more robust label: it does not depend on the server still sending `ask` for an
     UNSOLVED blank, which the new hint contract no longer requires it to. "Answer 3" is what the chevron
     above says, so the name now matches what is on screen. */
  inp.setAttribute("aria-label", b.n != null ? `Answer ${b.n}` : "fill in the blank");
  inp.dataset.clue = b.blank;
  inp.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); submitBlank(b.blank, inp.value, wrap, inp); }
  });
  inp.addEventListener("blur", () => { if (inp.value.trim()) submitBlank(b.blank, inp.value, wrap, inp); });
  inp.addEventListener("input", () => { wrap.classList.remove("bl-bad"); hideNudge(wrap); });
  wrap.appendChild(inp);
  wrap.appendChild(n);
  /* the `.bl-ask` hover span is GONE — it was the second of the two places the question leaked for
     free, and it is now behind the Hint button on the chevron. The input's `aria-label` above still
     carries `b.ask`, which is deliberate and is NOT the same leak: a screen-reader user needs to know
     which blank they are in, and that label is not a reward gated behind a price. */
  return wrap;
}

/* ⏱ "The timer does not smoothly tick second by second ... It seems to update randomly."
 *
 * It did, and this is why: the visible time was `w.focus_label`, a string FORMATTED ON THE SERVER, and the
 * only thing that ever replaced it was a tick response. Ticks are sent every `tick_every_s` (15s) and only
 * while the player is looking, so the display moved in ~15-second jumps whose arrival also carried the
 * round-trip — which is exactly what "updates randomly" looks like. Nothing was wrong with the CLOCK; the
 * bug was that the display was a REFRESHED VALUE rather than a RUNNING one.
 *
 * So the server stays the authority for how much focus time has accrued, and the display is drawn locally
 * from an ANCHOR: {seconds credited, the moment we were told}. Between ticks the paint loop advances it
 * against the wall clock; every tick re-anchors it to the server's number. The seconds digit now moves
 * once per second because something is actually counting, not because a fetch happened to land.
 */
function renderClock(w) {
  const box = $("#clockbox");
  const live = w.status === "open";
  box.hidden = !(live || w.status === "done");
  box.classList.toggle("paused", !live);
  anchorClock(w);
  paintClock();
}

function anchorClock(w) {
  const shown = w && (w.status === "open" || w.status === "done");
  if (!shown) { S.clock = null; return; }
  const server = Math.max(0, Number(w.focus_s) || 0);
  const same = S.clock && S.clock.week === w.week;
  // ⛔ NEVER LET THE CLOCK RUN BACKWARDS. render() is called on every answer, and it re-anchors from the
  //    /api/game payload — which can be older than what we have already extrapolated past. Snapping to it
  //    would visibly rewind the timer mid-game. For one week focus only ever accrues, so the newer of the
  //    two is the better estimate, and a tick that credits MORE than we guessed still snaps us forward.
  //    The cost is that if we ever over-counted, the display stays slightly ahead until the server catches
  //    up. A clock that is a second fast is a far smaller lie than one that jumps backwards.
  const seconds = same ? Math.max(server, S.clock.seconds) : server;
  S.clock = { week: w.week, seconds: seconds, live: w.status === "open", last: now() };
}

function now() {
  return (typeof performance === "object" && performance.now) ? performance.now() : Date.now();
}

/* The server's own two formulas, from the constants the server itself published in `scoring`. This is not
   a second source for either number: it is the same arithmetic on the same constants, re-anchored to the
   server's value every tick. Rendering the seconds locally while leaving `worth` to arrive 15s later would
   have put two disagreeing facts side by side in one box. */
function fmtFocus(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function worthFor(seconds) {
  const sc = (S.game && S.game.scoring) || {};
  const base = Number(sc.base), per = Number(sc.per_min), floor = Number(sc.floor);
  // If the constants are not there, say nothing rather than invent a score.
  if (!isFinite(base) || !isFinite(per) || !isFinite(floor)) return null;
  return Math.max(base - per * Math.floor(Math.max(0, seconds) / 60), floor);
}

function paintClock() {
  const c = S.clock;
  if (!c) return;
  const t = now();
  // Only advance while the player is actually looking, because that is the only time the SERVER credits
  // the week. A display that ran on regardless would drift away from the score it is explaining.
  if (c.live && looking()) c.seconds += (t - c.last) / 1000;
  c.last = t;
  $("#clkTime").textContent = fmtFocus(c.seconds);
  const worth = worthFor(c.seconds);
  if (worth !== null) $("#clkWorth").textContent = worth;
}

/* 250ms, not 1000ms: a one-second interval is not aligned to the second boundary it is displaying, so the
   digit appears to hesitate and occasionally skip. Four cheap text writes a second keeps it honest. */
function startClockPaint() {
  if (S.clockPaint) clearInterval(S.clockPaint);
  S.clockPaint = setInterval(paintClock, 250);
}

function esc(s) {
  return String(s === undefined || s === null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* ------------------------------------------------------------------ actions */
/* From the home screen: show the board, then load the week. showView first so the click feels
   immediate and the stages get a box to measure before the fetch returns. */
function goWeek(week) {
  // ⛔ , THE SECOND LOCK: refuse at the FUNCTION, not only at the listener. The card is one
  //    caller; anything added later inherits this guard instead of quietly re-opening the door. A
  //    locked week has no play view to show — /api/game clamps an unreleased week to the released
  //    one, so proceeding would land the player on a week they did not choose.
  const wk = ((S.game && S.game.weeks) || []).find(x => x.week === week);
  if (wk && wk.status === "locked") return;
  showView("play");
  if (week === S.week) { render(); return; }
  selectWeek(week);
}

async function selectWeek(week) {
  if (week === S.week) return;
  S.week = week;
  renderWeeks(S.game);
  try {
    S.game = await api("/api/game?week=" + week);
    S.week = S.game.active_week;
  } catch (e) { /* keep what we have; the tracker already moved */ }
  render();
}

async function unlock() {
  const btn = $("#unlockBtn");
  btn.disabled = true;
  btn.textContent = "Opening…";
  try {
    await api("/api/unlock", { week: S.week });
    S.game = await api("/api/game?week=" + S.week);
    render();
  } catch (e) {
    $("#unlockNote").textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Unlock week <span id="unlockNum">' + S.week + "</span>";
  }
}

function focusBlank(clueId) {
  const el = document.querySelector(`.bl[data-clue="${clueId}"]`);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  const inp = el.querySelector("input");
  if (inp) inp.focus();
  el.classList.add("bl-flash");
  setTimeout(() => el.classList.remove("bl-flash"), 900);
}

function showNudge(wrap, msg, tone) {
  hideNudge(wrap);
  const n = document.createElement("span");
  n.className = "bl-nudge tone-" + (tone || "bad");
  n.textContent = msg;
  wrap.appendChild(n);
}
function hideNudge(wrap) {
  const old = wrap.querySelector(".bl-nudge");
  if (old) old.remove();
}

const inflight = new Set();
const solvedLocally = new Set();
async function submitBlank(clueId, value, wrap, inp) {
  const v = (value || "").trim();
  if (!v || inflight.has(clueId)) return;
  // A double-click used to write two answer_correct rows. It could not inflate a score — the leaderboard
  // takes MAX per user and blank — but it did inflate the operator's correct_lodges, and a count that is
  // wrong for a reason nobody remembers is worse than no count.
  if (solvedLocally.has(clueId)) return;
  inflight.add(clueId);
  wrap.classList.add("bl-checking");
  try {
    const r = await api("/api/answer", { clue_id: clueId, value: v, week: S.week });
    wrap.classList.remove("bl-checking");
    if (r.verdict === "correct") {
      // Defensive on purpose: this used to do `r.weeks.reduce(...)` unconditionally, and the one branch
      // that omitted `weeks` was the one a real player reaches with two tabs open. The throw was caught by
      // the handler below and its message was rendered as the wrong-answer nudge — a raw
      // "Cannot read properties of undefined" painted red on a CORRECT answer, with the player's own score
      // frozen behind it. A missing field must degrade to "refresh from the server", never to an exception.
      if (Array.isArray(r.weeks)) {
        S.game.weeks = r.weeks;
        S.game.total_solved = r.weeks.reduce((a, w) => a + (w.solved || 0), 0);
      }
      if (typeof r.total_points === "number") S.game.total_points = r.total_points;
      if (r.letter && S.game.case) S.game.case.letter = r.letter;
      solvedLocally.add(clueId);
      if (!Array.isArray(r.weeks) || !r.letter) {
        try { S.game = await api("/api/game?week=" + S.week); } catch { /* keep what we have */ }
      }
      render();
      // the specified lines, on the specified events: every blank correct is "Great job!", any other correct answer is the
      // still-need-more line. Checked AFTER render(), so `week_done` reflects the answer just accepted.
      const wk = (S.game.weeks || []).find(x => x.week === S.week);
      cfoSays(r.week_done || (wk && wk.done) ? "done" : "correct");
      const el = document.querySelector(`.bl[data-clue="${clueId}"]`);
      if (el) { el.classList.add("bl-pop"); setTimeout(() => el.classList.remove("bl-pop"), 700); }
      return;
    }
    wrap.classList.add("bl-bad");
    cfoSays("wrong");
    showNudge(wrap, r.message || "Not that one.", r.verdict === "near" ? "near" : "bad");
    if (inp) { inp.focus(); inp.select(); }
    if (Array.isArray(r.weeks)) {
      S.game.weeks = r.weeks;
      renderWeeks(S.game);
      renderWidgets(r.weeks.find(w => w.week === S.week) || S.game.weeks[0]);
    }
  } catch (e) {
    // NOT painted as a wrong answer: this is the app failing, not the player. A 429 from the
    // wrong-answer throttle is its own amber message, and anything else says so plainly instead of
    // masquerading as a verdict.
    wrap.classList.remove("bl-checking");
    const cool = e.payload && e.payload.cooldown_s;
    if (!cool) wrap.classList.add("bl-bad");
    showNudge(wrap, cool ? e.message : "Could not check that just now — " + e.message,
              cool ? "near" : "bad");
  } finally {
    inflight.delete(clueId);
  }
}

/* ⭐ the CFO reacts. Only week 1's stage is his — weeks 2-4 are the drawn scenes and have no
   `say`, so the capability is FEATURE-DETECTED rather than assumed from the week number. His three lines
   live in sprites.js beside the art they are painted over, so this file cannot drift from the agreed wording. */
function cfoSays(which) {
  const lines = (window.Sprites && window.Sprites.cfoLines) || {};
  if (S.scene && typeof S.scene.say === "function" && lines[which]) {
    S.scene.say(lines[which], which === "done" ? 4200 : 3200);
  }
}

/* ------------------------------------------------------------------ the genie chat */
function chatLine(who, text, extra, kind) {
  const wrap = document.createElement("div");
  wrap.className = "msg msg-" + who + (kind === "auto" ? " msg-auto" : "");
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = text;
  wrap.appendChild(b);
  if (extra) wrap.appendChild(extra);
  $("#chat").appendChild(wrap);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return wrap;
}

function mood(m, label) {
  $("#genieStage").dataset.mood = m;
  $("#genieMood").textContent = label;
  const set = m === "thinking" ? "thinking" : m === "typing" ? "typing"
            : m === "talking" ? "talking" : m === "asleep" ? "asleep" : "idle";
  if (S.lamp) S.lamp.show(set);
}

/* ⭐ A STATE THAT DID NOT EXIST WAS NEEDED. The star was asked to circle "when the answer coming back
   is being typed out" — and nothing here typed anything out: the answer arrived whole and was assigned in
   one go, so there were only thinking and talking. Rather than fold that request into `thinking`, which is a
   different moment and would have made the circle fire while the lamp was still waiting, the answer is now
   REVEALED progressively and that reveal is its own state, `typing`.
   Bounded on purpose: ~14ms a character but never more than 1.1s in total, so a long answer is not slow to
   read. Under prefers-reduced-motion it is assigned instantly, because a typewriter is exactly the kind of
   motion that setting is asking us not to make. */
function typeOut(el, text) {
  const full = String(text == null ? "" : text);
  const reduced = window.Sprites && window.Sprites.reducedMotion && window.Sprites.reducedMotion();
  if (reduced || full.length < 2) { el.textContent = full; return Promise.resolve(); }
  const total = Math.min(1100, full.length * 14);
  const step = Math.max(1, Math.round(full.length / (total / 16)));
  return new Promise(res => {
    let i = 0;
    el.textContent = "";
    const iv = setInterval(() => {
      i = Math.min(full.length, i + step);
      el.textContent = full.slice(0, i);
      $("#chat").scrollTop = $("#chat").scrollHeight;
      if (i >= full.length) { clearInterval(iv); res(); }
    }, 16);
  });
}

async function ask(ev) {
  if (ev) ev.preventDefault();
  const inp = $("#chatInput");
  const q = inp.value.trim();
  if (q.length < 3 || S.asking) return;
  S.asking = true;
  inp.value = "";
  $("#chatBtn").disabled = true;
  chatLine("you", q);
  $("#starters").hidden = true;
  const pending = chatLine("genie", "…");
  mood("thinking", "thinking");
  const t0 = Date.now();
  const tickLabel = setInterval(() => {
    pending.querySelector(".bubble").textContent = "thinking… " + Math.round((Date.now() - t0) / 1000) + "s";
  }, 1000);
  try {
    const r = await api("/api/chat", { question: q, week: S.week, conversation_id: S.conversation });
    clearInterval(tickLabel);
    if (!r.ok) {
      pending.querySelector(".bubble").textContent = r.error || "The genie went quiet. Try again.";
      pending.classList.add("msg-err");
      mood("idle", "ready");
      return;
    }
    S.conversation = r.answer.conversation_id || S.conversation;
    const a = r.answer;
    const t = (a.tables || []).find(x => x.rows && x.rows.length);
    mood("typing", "answering");
    await typeOut(pending.querySelector(".bubble"), a.text || "(no answer text)");
    if (t) pending.appendChild(tableEl(t));
    mood("talking", "answering");
    // ⭐  ITEMS 2+3: 2600ms -> 4200ms. `talking` is now where the SPARKLES are, and it is the state
    //    that begins AFTER typeOut has finished (typeOut is bounded to 1.1s), so this window is the whole
    //    of what a person would call "Genie is talking". At 2.6s the new mark would have been a flicker
    //    at the edge of a glance; the answer is still being read at 4.2s.
    setTimeout(() => mood("idle", "ready"), 4200);
  } catch (e) {
    clearInterval(tickLabel);
    // ⛔ A STALE CONVERSATION ID WAS A PERMANENT STUCK STATE: every later question failed too, because the
    //    failure path never cleared it and the player had no way to know that "New chat" was the cure.
    //    Dropping it here means the next question starts a fresh conversation by itself.
    S.conversation = null;
    pending.querySelector(".bubble").textContent = scrubbed(e.message)
      + " — your next question will start a fresh conversation.";
    pending.classList.add("msg-err");
    mood("idle", "ready");
  } finally {
    S.asking = false;
    $("#chatBtn").disabled = false;
    inp.focus();
  }
}

/* Platform error text can carry the workspace's internal user id and other ids a player has no business
   reading. Nothing is lost by removing them: they mean nothing to the person and everything to a log. */
function scrubbed(msg) {
  return String(msg || "")
    .replace(/\b\d{10,}\b/g, "…")
    .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "…")
    .replace(/\b[0-9a-f]{32}\b/gi, "…")
    .trim() || "the genie did not answer";
}

function tableEl(t) {
  const wrap = document.createElement("div");
  wrap.className = "restab";
  const tb = document.createElement("table");
  if (t.schema && t.schema.length) {
    const tr = document.createElement("tr");
    t.schema.forEach(h => { const th = document.createElement("th"); th.textContent = h; tr.appendChild(th); });
    tb.appendChild(tr);
  }
  t.rows.slice(0, 12).forEach(row => {
    const tr = document.createElement("tr");
    row.forEach(c => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
    tb.appendChild(tr);
  });
  wrap.appendChild(tb);
  if (t.rows.length > 12) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = `${t.rows.length} rows, first 12 shown`;
    wrap.appendChild(p);
  }
  return wrap;
}

function renderStarters(list) {
  // The same examples the Genie space itself carries. An empty chat panel should read as an invitation,
  // and a first question that works teaches the loop faster than any instruction.
  const box = $("#starters");
  box.innerHTML = "";
  list.slice(0, 3).forEach(q => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = q;
    b.addEventListener("click", () => { $("#chatInput").value = q; ask(); });
    box.appendChild(b);
  });
  box.hidden = !list.length;
}

/* The opening question. Uncached by construction — there is no store to read from, so it is a real call
   every time, which is the point: if it comes back, Genie is working for this viewer right now.
   Three outcomes and all of them visible, because a lamp that thinks forever cannot be told apart from a
   genie that is merely slow, and that would quietly turn this health check into decoration. */
async function greet() {
  const q = (S.game && S.game.greeting_question) || "What can you do?";
  chatLine("you", q, null, "auto");
  const pending = chatLine("genie", "waking up…");
  mood("thinking", "waking");
  const t0 = Date.now();
  const clock = setInterval(() => {
    pending.querySelector(".bubble").textContent = "waking up… " + Math.round((Date.now() - t0) / 1000) + "s";
  }, 1000);
  try {
    // Bounded on BOTH sides: the server's budget is 60s, so the client gives it 75s and then stops
    // waiting. Whichever fires, the player sees a failed lamp rather than one that thinks forever.
    const r = await api("/api/chat", { question: q, week: S.week, greeting: true }, 75000);
    clearInterval(clock);
    if (!r.ok) throw new Error(r.error || "the genie did not answer");
    if (!r.answer || !r.answer.text) throw new Error("the genie answered with nothing");
    S.conversation = r.answer.conversation_id || null;
    mood("typing", "answering");
    await typeOut(pending.querySelector(".bubble"), r.answer.text || "(no answer text)");
    S.greetMs = Date.now() - t0;
    mood("talking", "answering");
    setTimeout(() => mood("idle", "ready"), 4200);     // the same dwell as an answer, 3000 -> 4200
  } catch (e) {
    clearInterval(clock);
    // The visible failed state: the lamp goes dark and says so. The game still works, and the player can
    // try a question of their own — so a one-off is recoverable without a reload.
    // BT-C: at 120 simultaneous opens the Apps ingress returns a bare 502 for about a fifth of them while
    // GENIE IS FINE. The lamp was honest that something was wrong and wrong about WHAT, so a transport
    // failure now says transport rather than blaming the genie.
    const transport = e.timedOut || /HTTP 5\d\d|Failed to fetch|NetworkError|load failed/i.test(e.message);
    pending.querySelector(".bubble").textContent = transport
      ? "The app could not be reached just now (" + scrubbed(e.message) + "). That is this page talking to "
        + "the server, not the genie — the game still works, try asking me something."
      : "I could not reach the genie just now (" + scrubbed(e.message) + "). The game still works — try "
        + "asking me something, and it may come back.";
    pending.classList.add("msg-err");
    mood("asleep", "not answering");
  }
}

/* ⭐ "When I click new chat, no need to ask the warm up question, just put a message there —
   'Type your question below'". So New chat no longer spends a Genie round trip on a greeting it does not
   need; it leaves the panel with an invitation instead.

   ⛔ THE BOOT GREETING IS DELIBERATELY UNTOUCHED. greet() on open is not a pleasantry — it is the app's
   live health check, and its FAILURE is the only visible sign that Genie is unreachable (the lamp goes to
   `asleep` and the mood pill reads "not answering"). Removing the re-greet from this BUTTON keeps that
   check; removing it from boot() would delete the product's only Genie liveness signal.
   Measured side benefit: load was one Genie call per open PLUS one per New-chat click, and this removes
   the second — see the scale measurements, where New-chat re-greeting was named as a load multiplier. */
function newChat(regreet) {
  S.conversation = null;
  $("#chat").innerHTML = "";
  mood("idle", "ready");
  $("#starters").hidden = false;
  if (regreet === true) greet();
  else chatInvite();
}

/* The empty-panel invitation. A bare empty box reads as broken, which is what the greeting was
   incidentally covering up. */
function chatInvite() {
  const el = document.createElement("p");
  el.className = "chat-invite";
  el.textContent = "Type your question below";
  $("#chat").appendChild(el);
}

/* ------------------------------------------------------------------ keeping the ask box on screen */
/* ⛔ THE ONE INTERACTION THE GAME IS BUILT AROUND WAS BELOW THE FOLD at 1728, 1440, 1280 and 1024, and a
   measured JS max-height on the genie column used to be what kept it on screen.
   ⭐ THAT PROPERTY IS NOW STRUCTURAL, NOT MEASURED — see "" in app.css. The play view is exactly
   as tall as the viewport, the memo and the chat are the two parts that give, and the ask row is on screen
   because the layout cannot push it off. It was needed anyway: the complaint was blank space at
   the bottom AND the chat growing the page as it filled up, which are the same cause.
   ⚠️ DO NOT REINTRODUCE A JS max-height HERE. An inline max-height on #genie fights the flex layout that
   now guarantees this, and the symptom is a column that will not fill its row. */

/* ------------------------------------------------------------------ the pausing clock */
function looking() {
  // "Looking at that week" = the tab is visible AND this week is the one on screen AND the Play view is
  // the one showing. Anything else — another tab, the leaderboard, a different week — is not looking, so
  // no tick is sent and the server credits nothing.
  const w = (S.game && S.game.weeks || []).find(x => x.week === S.week);
  return !document.hidden && $("#view-play").classList.contains("on") && !!w && w.status === "open";
}

function startTicker() {
  if (S.ticker) clearInterval(S.ticker);
  const every = ((S.game && S.game.tick_every_s) || 15) * 1000;
  S.ticker = setInterval(tick, every);
  tick();
}

async function tick() {
  if (!looking()) return;
  try {
    const r = await api("/api/tick", { week: S.week });
    if (!r.running) return;
    const w = (S.game.weeks || []).find(x => x.week === S.week);
    if (w) {
      // focus_s is what the local clock re-anchors to; the server's pre-formatted label and worth are
      // kept on the object for anything else that reads them, but the DISPLAY is drawn from focus_s.
      w.focus_s = r.focus_s; w.focus_label = r.focus_label; w.worth_now = r.worth_now;
      renderClock(w);
    }
  } catch (e) { /* a missed tick costs the player a little decay, which is the harmless direction */ }
}

/* ------------------------------------------------------------------ the other two views */
function showView(name) {
  $$(".view").forEach(v => v.classList.toggle("on", v.id === "view-" + name));
  $$(".nav button").forEach(b => b.classList.toggle("on", b.dataset.view === name));
  if (name === "roster") loadRoster();
  // BOTH LANES ADDED A BRANCH HERE, and both are kept: the data lane's operator view loads its paginated
  // log alongside the settings, and the game lane re-fits the two canvas stages when the play view becomes
  // visible. They are independent, so the merge is additive — neither behaviour is a choice over the other.
  if (name === "operator") { applyOpLock(); }
  // The stages are built while their view is hidden, where a canvas has no box and Stage.fit() correctly
  // declines to guess one. Their ResizeObserver fires when the box appears, so this is belt-and-braces
  // for the one case it does not: a view shown with the window never resized.
  if (name === "play") {
    if (S.lamp) S.lamp.fit();
    if (S.scene) S.scene.fit();
  }
}

/* ITEMS 10 + 12. Three independent loads, deliberately not one: a failure of the five-figure strip must
   not blank the board under it, and vice versa. Each renders its own honest message. */
async function loadRoster() {
  renderLbTabs();
  loadLbStats();
  loadLbBoard();
}

function renderLbTabs() {
  const box = $("#lbTabs");
  const total = (S.game && S.game.season && S.game.season.total_weeks) || 4;
  const tabs = [{ key: "all", label: "Overall" }];
  for (let i = 1; i <= total; i++) tabs.push({ key: String(i), label: "Week " + i });
  box.innerHTML = "";
  tabs.forEach(t => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = t.label;
    b.dataset.lb = t.key;
    b.setAttribute("role", "tab");
    b.className = String(S.lbTab) === t.key ? "on" : "";
    b.setAttribute("aria-selected", String(S.lbTab) === t.key ? "true" : "false");
    b.addEventListener("click", () => {
      if (String(S.lbTab) === t.key) return;
      S.lbTab = t.key;
      renderLbTabs();
      loadLbBoard();
    });
    box.appendChild(b);
  });
}

async function loadLbBoard() {
  const board = $("#lbBoard"), firsts = $("#lbFirst");
  const tab = String(S.lbTab);
  // "Overall" still needs a week parameter because the endpoint always scopes first_blood to one; the
  // week we are looking at is the sensible default and is what the board used before tabs existed.
  const week = tab === "all" ? (S.week || 1) : Number(tab);
  // ⭐  D5: BOTH SCOPES ARRIVE IN ONE PAYLOAD, so switching between Overall and the week you are
  //    already on must not pay for the round trip twice. Measured before this: three tab clicks
  //    (week 1 -> Overall -> week 2) cost THREE requests, and the middle one asked for byte-identical
  //    data — on the deployment that is ~4.9s of warehouse for a tab that re-renders from what the
  //    client already holds. Keyed on the week because `first_blood` is per-week; TTL'd at 20s so this
  //    de-duplicates rapid tab switching rather than pinning a stale board.
  const fresh = S.lbCache && S.lbCache.week === week && (Date.now() - S.lbCache.at) < 20000;
  if (!fresh) board.innerHTML = `<p class="muted">Loading…</p>`;
  firsts.innerHTML = "";
  const d = fresh ? S.lbCache.data
                  : await api("/api/leaderboard?week=" + week).catch(e => ({ error: e.message }));
  if (!d.error && !fresh) S.lbCache = { week, at: Date.now(), data: d };
  if (d.error) { board.innerHTML = `<p class="muted">${esc(d.error)}</p>`; return; }
  const scope = tab === "all" ? "all_time" : "this_week";
  const counts = nameCounts(d.all_time, d.this_week, d.first_blood);
  board.innerHTML = lbCount(d, scope, d.week) + (tab === "all"
    ? lbTable("All four weeks", d.all_time, d.you, counts)
    : lbTable("Week " + d.week, d.this_week, d.you, counts));
  // First-to-fill belongs to a single week, so it is shown on a week tab and not under "Overall",
  // where it would be a week's worth of detail wearing an all-time heading.
  // the same tag as the board, from the identity first_blood now carries (`user_key` here is the
  // player_id, aliased — the same value and the same rule, so one person cannot wear two tags).
  firsts.innerHTML = (tab !== "all" && d.first_blood && d.first_blood.length)
    ? `<h3>First to fill each blank · week ${d.week}</h3><ul class="firsts">` +
      d.first_blood.map(f => {
        const name = playerLabel(f);
        const tag = (counts[name] || 0) > 1
          ? ` <span class="who-tag" title="a short tag so two players with the same name can be told apart">·${esc(identTag(f.user_key))}</span>`
          : "";
        return `<li><b>${esc(f.label)}</b><span>${esc(name)}${tag}</span></li>`;
      }).join("") + "</ul>"
    : "";
}

/* ⭐  D5 — "For overall, week 1, week 2 etc leaderboards, can we also display total players for that
   filter? So we know how many people have participated in the week 2 questions, for example."

   `players_attempted` is the headline because "participated" means they answered something — it is the
   closest of the four to the source sentence. ⛔ AND THE DENOMINATOR IS IN THE LABEL, because "players who
   answered" and "players who opened the week" are different numbers and whichever shipped bare would be
   read as the other. The other three are shown beside it, each named, rather than left out — they are the
   difference between "seven people tried" and "nine people looked".

   Both scopes arrive in ONE payload, so switching between Overall and the week you are on costs no
   round trip: this reads the scope out of the response the board already has and computes nothing.
   An absent `player_counts` says so; a 0 prints as 0, because a 0 is a real count. */
function lbCount(d, scope, week) {
  const pc = d.player_counts && d.player_counts[scope];
  const where = scope === "all_time" ? "across all four weeks" : `in week ${week}`;
  if (!pc || typeof pc !== "object") {
    return `<p class="lbcount lbcount-none">The player count is not available for this filter.</p>`;
  }
  const n = k => (k in pc ? pc[k] : null);
  const show = v => (v === null ? "?" : String(v));
  // ⛔ THE SINGULAR CASE IS THE ONE A REAL DEPLOYMENT IS IN. A local board seeded with six players, so
  //    "players answered" read correctly every time I looked at it, and the served card said
  //    "1 players answered" to the only person using it. A count of 1 is not an edge case here — it is
  //    the state of every week until a second person plays. `?` keeps the plural, which reads as neutral.
  const plural = (v, one, many) => (v === 1 ? one : many);
  const opened = scope === "all_time" ? "opened the game" : `unlocked week ${week}`;
  return `<div class="lbcount"><b>${esc(show(n("players_attempted")))}</b>` +
    `<span>${esc(plural(n("players_attempted"), "player", "players"))} answered a question ${esc(where)}</span>` +
    `<i>${esc(show(n("players_opened")))} ${esc(opened)} · ` +
    `${esc(show(n("players_scored")))} got at least one right · ` +
    `${esc(show(n("players_asked")))} asked Genie</i></div>`;
}

/* ══ The five figures, PRODUCED by the data lane's GET /api/leaderboard/stats.
 *
 * ⛔ THE EMPTY TEST IS `=== null`, NEVER FALSINESS. The producer returns null for an average with no
 *    denominator and a NUMBER otherwise — so `if (!x)` would hide a real 0 behind "not available yet".
 *    Those are different claims: 0 means they played and scored nothing; null means nobody has played.
 * ⛔ AND NOTHING HERE IS COMPUTED LOCALLY. Points are deduplicated per (user, clue) with MAX on the
 *    server, exactly as the board beneath is; a client-side sum would double-count a re-solved blank and
 *    put two disagreeing scores on one page.
 */
const LB_STATS = [
  { key: "players", label: "players" },
  { key: "total_questions", label: "questions asked" },
  { key: "avg_score", label: "average score" },
  { key: "avg_questions_per_player", label: "questions per player" },
  { key: "avg_correct_per_player", label: "correct per player" },
];

async function loadLbStats() {
  const box = $("#lbStats");
  box.innerHTML = `<p class="lbstats-msg">Loading the numbers…</p>`;
  let d;
  try {
    d = await api("/api/leaderboard/stats");
  } catch (e) {
    // A 503 ("the roster is not configured") and a network failure both land here. Say so — never draw
    // five zeros, which is a confident claim that nobody has played.
    box.innerHTML = `<p class="lbstats-msg">The summary numbers are not available just now` +
      ` — ${esc(scrubbed(e.message))}. The board below is unaffected.</p>`;
    return;
  }
  if (!d || typeof d !== "object") {
    box.innerHTML = `<p class="lbstats-msg">The summary numbers are not available just now.</p>`;
    return;
  }
  // Read defensively and let the type catch up: a key that is absent is reported as missing rather than
  // rendered as a dash that looks like a legitimate "no denominator yet".
  const missing = LB_STATS.filter(m => !(m.key in d)).map(m => m.key);
  box.innerHTML = LB_STATS.map(m => {
    const v = d[m.key];
    if (!(m.key in d)) return `<div class="st st-none"><b>?</b><i>${esc(m.label)}</i></div>`;
    if (v === null) return `<div class="st st-none"><b>—</b><i>${esc(m.label)}<br>no data yet</i></div>`;
    return `<div class="st"><b>${esc(v)}</b><i>${esc(m.label)}</i></div>`;
  }).join("") + (missing.length
    ? `<p class="lbstats-msg">Missing from the summary: ${esc(missing.join(", "))}.</p>` : "");
}

/* ⭐  D6 — "we don't need to display the email IDs but we identify users uniquely by email ID".
   Both halves of that sentence, and they pull in opposite directions.

   ⛔ THE ROW'S `user_key` IS NOW AN EMAIL ADDRESS. The data lane's aggregate identity is
   `COALESCE(NULLIF(TRIM(user_email),''), user_key)` and the board query returns it AS `user_key`, so the
   old fallback `display_name || user_key` printed `first.last@example.com` on the leaderboard the moment a
   player had no display name. Proven on a seeded board, not reasoned about. An earlier note
   called that fallback "not an email leak (it is a numeric workspace id on the deployment)", which was
   true when it was written and a later change made it false — which is exactly why a new fact has to
   be applied BACKWARDS to what is already written.

   So: an email is never rendered. A row with no name is "Player". And because two people genuinely called
   the same thing must stay two rows he can tell apart, a colliding name earns a SHORT STABLE TAG derived
   from the identity — three base-36 characters of an FNV-1a hash. It reveals nothing (it is not
   reversible, and anyone who could guess the email already has it), it is stable across tabs and weeks
   because it is a function of the identity alone, and it appears ONLY where there is a collision, so the
   ordinary board carries no noise. */
function identTag(id) {
  let h = 0x811c9dc5;
  const str = String(id == null ? "" : id);
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  return h.toString(36).slice(-3).padStart(3, "0");
}

/* The viewer's identity BY THE AGGREGATE RULE, so "me" on the board is the same person the board thinks.
   ⛔ `you` from the server is the viewer's `user_key`, while a row is keyed on the email — so
   `r.user_key === you` silently stopped matching anybody with an email and the highlight was dead. Both
   are compared, because a player whose rows predate the email column is still keyed on user_key. The
   COALESCE is spelled out here deliberately: any client-side identity comparison has to
   use exactly the producer's rule, or the strip and the board disagree about who a player is. */
function viewerIds() {
  const v = (S.game && S.game.viewer) || {};
  const email = String(v.email || "").trim();
  return [email || null, v.user_key || null].filter(Boolean);
}

/* ⭐ ONE COLLISION SET FOR EVERY LIST ON THE PAGE. The board and the first-to-fill list are on screen
   together, so counting names per-list would tag a person on one and not the other and invite exactly the
   question the tag exists to answer. Both renderers take this map, and both derive the tag from the same
   field, because the data lane deliberately projected first_blood's identity as the SAME `player_id` the
   board uses: had one list carried the workspace id and the other the email, one person would have worn
   two different tags on one page, which reads as two people rather than as missing information. */
function playerLabel(r) {
  /* ⛔ THE SAME THING HAS TWO FIELD NAMES, AND KNOWING ONLY ONE OF THEM PRINTS A PLACEHOLDER OVER A REAL
     NAME. The board query aliases `MAX(display_name) AS display_name`; `first_blood` projects
     `user_display_name`. An earlier build shipped `display_name || "Player"`, which rendered
     "Player" for every entry in the first-to-fill list, where a real name belongs. The
     previous code read `display_name || user_key` and rendered an EMPTY string there for the same reason,
     so the list has never shown a name — mine merely made the absence visible.
     Found by reading the PAYLOAD'S KEYS rather than the query text, which is the only place the two
     spellings are visible at once. */
  return r.display_name || r.user_display_name || "Player";
}

function nameCounts() {
  /* ⛔ COUNT DISTINCT PEOPLE PER NAME, NOT ROWS. Counting rows put a tag on EVERY row on the board,
     because the same player appears in `all_time` AND `this_week` AND possibly `first_blood` — so every
     name "collided" with itself and the no-noise property this tag exists for was destroyed. Caught by
     LOOKING at the rendered board, where five uniquely-named players all wore tags.
     A row with no identity contributes the empty string, so several of them collapse to ONE — which is
     the honest answer: with no identity they cannot be told apart, and giving them a shared tag would
     say they are one person. */
  const byName = {};
  [].slice.call(arguments).forEach(list => (list || []).forEach(r => {
    const k = playerLabel(r);
    (byName[k] = byName[k] || {})[String(r.user_key == null ? "" : r.user_key)] = 1;
  }));
  const out = {};
  Object.keys(byName).forEach(k => { out[k] = Object.keys(byName[k]).length; });
  return out;
}

function lbTable(title, rows, you, counts) {
  if (!rows || !rows.length) return `<h3>${esc(title)}</h3><p class="muted">Nothing here yet.</p>`;
  const mine = new Set(viewerIds().concat(you ? [you] : []));
  const labelOf = playerLabel;
  const seen = counts || nameCounts(rows);
  const collides = rows.some(r => seen[labelOf(r)] > 1);
  return `<h3>${esc(title)}</h3><table class="lbt"><tr><th class="rank">#</th><th>Player</th>` +
    `<th class="num">Filled</th><th class="num">Points</th></tr>` +
    rows.map((r, i) => {
      const name = labelOf(r);
      const tag = seen[name] > 1
        ? ` <span class="who-tag" title="a short tag so two players with the same name can be told apart">·${esc(identTag(r.user_key))}</span>`
        : "";
      return `<tr${mine.has(r.user_key) ? ' class="me"' : ""}><td class="rank">${i + 1}</td>` +
        `<td class="who-cell">${esc(name)}${tag}</td>` +
        `<td class="num">${r.solved}</td><td class="num">${r.points}</td></tr>`;
    }).join("") + "</table>" +
    (collides ? `<p class="muted small">Two players share a name — the short tag tells them apart.</p>` : "");
}

async function loadOperator() {
  const s = await api("/api/settings").catch(e => ({ error: e.message }));
  const rows = $("#releaseRows");
  rows.innerHTML = "";
  if (s.error) { rows.innerHTML = `<p class="muted">${esc(s.error)}</p>`; }
  else {
    (s.weeks || []).forEach(w => {
      const id = "rel" + w.week;
      const lab = document.createElement("label");
      lab.className = "relrow";
      lab.innerHTML = `<input type="checkbox" id="${id}" value="${w.week}"${(s.released || []).includes(w.week) ? " checked" : ""}>` +
        `<span><b>Week ${w.week} · ${esc(w.title)}</b><i>${esc(w.challenge || "")}</i></span>`;
      rows.appendChild(lab);
    });
    $("#releaseMsg").textContent = s.current && s.current.set_by
      ? `last changed by ${s.current.set_by}` : "never changed — week 1 is released by default";
  }
  // show the message that is actually in force, so an operator edits the live line rather
  // than typing over a blank box and wondering whether the old one is still showing to players.
  if (!s.error) {
    const cur = (s.current && s.current.client_message) || "";
    $("#clientMsgInput").value = cur;
    $("#clientMsgMsg").textContent = cur ? "showing on the home page now" : "nothing is shown";
  }
  const st = await api("/api/stats").catch(e => ({ error: e.message }));
  $("#opStats").innerHTML = st.error ? `<p class="muted">${esc(st.error)}</p>` : statsHtml(st);
  // ⭐ "Where did the scores go?" has an answer on the page. The marker row survives the wipe it records,
  //    so this is read from the log rather than from anything the page remembers.
  const lr = (st && st.last_reset) || {};
  $("#resetLast").textContent = lr.reset_at
    ? `Last reset ${lr.reset_at} by ${lr.by_name || lr.by_email || "an operator"}` +
      (Number(lr.resets) > 1 ? ` · ${lr.resets} resets in total` : "")
    : "Never reset.";
  if (st && st.reset_confirm_word) $("#resetPhrase").textContent = st.reset_confirm_word;
  $("#opLogWhere").innerHTML = st.error ? "" :
    `every row the app has written, newest first${(st.storage || {}).mode === "local"
      ? " — from the local SQLite file" : ""}`;
}

// ── THE STATS TILES ARE GONE ───────────────────────────────────────────────────
// "In the operator section, lets remove the stats widgets. Instead, lets just have 5 stats widgets at the
// top of the leader board". The five live on the LEADERBOARD now, from /api/leaderboard/stats, rendered
// there rather than here — so this function no longer draws tiles at all.
//
// WHAT DELIBERATELY SURVIVED, because removing it would lose the only warning that the numbers lie: the
// dropped-rows banner. `degraded` means rows that were meant to be recorded are GONE, so every figure —
// including the five now on the leaderboard — is understated by an unknown amount. The
// widgets were asked to go, not the alarm, and a silent understatement of the adoption number is exactly
// what this build has already paid for once.
//
// The raw `Log writer: {"queue_depth":0,...}` blob he called out is gone: the same facts are now a
// readable line, and the log ROWS he actually wanted are in the panel below.
function statsHtml(st) {
  const w = st.log_writer || {};
  const store = st.storage || {};
  const bits = [];
  if (store.mode === "local") {
    bits.push(`local SQLite — <code>${esc(store.target || "?")}</code>`);
    // ⭐ Said where a human reads it, not left to be discovered after a redeploy.
    bits.push(`<b>ephemeral</b>: a restart or redeploy loses this file — export regularly, restore below`);
  } else {
    bits.push(`Delta table — <code>${esc(store.target || st.table || "?")}</code>`);
  }
  if (w.written !== undefined) bits.push(`${w.written} row(s) written this session`);
  if (w.queue_depth) bits.push(`${w.queue_depth} queued`);
  if (w.last_error) bits.push(`last error: ${esc(String(w.last_error))}`);

  return ((w && w.degraded)
      ? `<p class="warn">⛔ The activity writer has dropped ${w.dropped_rows} row(s), so every number on
         the leaderboard is understated by an unknown amount. Last loss:
         ${esc(JSON.stringify(w.last_drop))}</p>`
      : "") +
    `<p class="muted small">${bits.join(" · ")}</p>`;
}

/* ── the activity log, paginated ──────────────────────────────────────────────────────── */
// Columns chosen to be the ones an operator reads across, narrowest first. question_text last because it
// is the only wide one and pushing it right keeps every other column aligned down the page.
const LOG_COLS = ["event_ts", "event_type", "user_display_name", "user_email", "week_index",
                  "clue_id", "verdict", "points", "latency_ms", "genie_status", "extracted_value",
                  "session_id", "app_build", "question_text"];
const LOGS = { page: 1, size: 50, pages: 1, total: 0 };

async function loadLogs() {
  const t = $("#logTable");
  $("#logPageMsg").textContent = "loading…";
  let d;
  try {
    d = await api(`/api/logs?page=${LOGS.page}&size=${LOGS.size}`);
  } catch (e) {
    t.innerHTML = "";
    $("#logPageMsg").textContent = e.message;
    return;
  }
  LOGS.pages = d.pages || 1; LOGS.total = d.total || 0; LOGS.page = d.page || 1;
  const rows = d.rows || [];
  const head = `<thead><tr>${LOG_COLS.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>`;
  const body = rows.length
    ? `<tbody>${rows.map(r => `<tr>${LOG_COLS.map(c => {
        const v = r[c];
        const txt = (v === null || v === undefined || v === "") ? "" : String(v);
        // title= so a truncated cell is still readable without a layout that reflows per row.
        // The verdict cell gets a value class so the three outcomes can be coloured from the same
        // good/near/bad tokens the game uses, instead of this table inventing a second palette.
        const extra = (c === "verdict" && txt) ? ` v-${esc(txt)}` : "";
        return `<td class="c-${esc(c)}${extra}" title="${esc(txt)}">${esc(txt.length > 120 ? txt.slice(0, 120) + "…" : txt)}</td>`;
      }).join("")}</tr>`).join("")}</tbody>`
    : `<tbody><tr><td colspan="${LOG_COLS.length}" class="muted">no rows yet</td></tr></tbody>`;
  t.innerHTML = head + body;
  const from = LOGS.total ? (LOGS.page - 1) * LOGS.size + 1 : 0;
  const to = Math.min(LOGS.page * LOGS.size, LOGS.total);
  $("#logPageMsg").textContent = `${from}–${to} of ${LOGS.total} · page ${LOGS.page} of ${LOGS.pages}`;
  $("#logPrev").disabled = LOGS.page <= 1;
  $("#logNext").disabled = LOGS.page >= LOGS.pages;
}

/* ── restore from a CSV ───────────────────────────────────────────────────────────────── */
let IMPORT_FILE = null;

function importReport(d, dry) {
  // ⭐ ORDERING IS REPORTED AS PROMINENTLY AS SUCCESS. A restore that recovers every point and silently
  //    re-ranks the board LOOKS complete, so "unrecoverable" is rendered as a warning, not a footnote.
  const cls = d.ordering === "authoritative" ? "muted" : "warn";
  const lines = [
    `<p><b>${dry ? "Nothing written yet — this is a preview." : "Imported."}</b> ` +
    `${esc(String(d.source || ""))}: ${d.rows_in_file} row(s) in the file, ` +
    (dry ? `${d.would_write} would be written, ${d.already_present} already present.`
         : `${d.written} written, ${d.skipped_already_present} already present.`) + `</p>`,
    `<p class="${cls}"><b>Ordering: ${esc(String(d.ordering || "?"))}.</b> ${esc(String(d.ordering_note || ""))}</p>`,
  ];
  if (d.synthetic_ids) {
    lines.push(`<p class="muted small">${d.synthetic_ids} row(s) had no event id, so a stable one was
      derived from the row's own contents — re-importing this file is still a no-op.</p>`);
  }
  if ((d.columns_withheld || []).length) {
    lines.push(`<p class="muted small">Withheld on purpose (never imported):
      ${esc((d.columns_withheld || []).join(", "))} — that is the answer key.</p>`);
  }
  if ((d.columns_ignored || []).length) {
    lines.push(`<p class="muted small">Ignored unknown column(s): ${esc((d.columns_ignored || []).join(", "))}</p>`);
  }
  if (d.event_types) {
    lines.push(`<p class="muted small">${Object.entries(d.event_types)
      .map(([k, n]) => `${esc(k)} ${n}`).join(" · ")}</p>`);
  }
  if (d.receipt_error) {
    lines.push(`<p class="warn small">The import succeeded but its receipt row failed to write:
      ${esc(String(d.receipt_error))}</p>`);
  }
  return lines.join("");
}

async function importPost(dry) {
  if (!IMPORT_FILE) return;
  const msg = $("#importMsg");
  msg.innerHTML = `<p class="muted">${dry ? "checking" : "importing"} ${esc(IMPORT_FILE.name)}…</p>`;
  try {
    const r = await fetch(`/api/import?filename=${encodeURIComponent(IMPORT_FILE.name)}${dry ? "&dry=1" : ""}`,
                          { method: "POST", body: IMPORT_FILE, headers: { "Content-Type": "text/csv" } });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      // The server's refusal text is written to be read by a person, so it is shown verbatim rather
      // than replaced with a generic failure.
      msg.innerHTML = `<p class="warn">${esc(d.error || `import failed (${r.status})`)}</p>`;
      $("#importGo").disabled = true;
      return;
    }
    msg.innerHTML = importReport(d, dry);
    $("#importGo").disabled = dry ? false : true;
    if (!dry) {
      IMPORT_FILE = null;
      $("#importFile").value = "";
      LOGS.page = 1;
      await loadLogs();
      await loadOperator();
      // The board and the player's own state both moved under them.
      S.game = await api("/api/game?week=" + S.week).catch(() => S.game);
      render();
    }
  } catch (e) {
    msg.innerHTML = `<p class="warn">${esc(e.message)}</p>`;
  }
}

/* ══════════════════════════════════════════════════════════ RESET THE GAME
 * "please add a reset button in the operator dashboard which resets the game and everyone's stats"
 *
 * ⛔ THE SERVER, NOT THIS FILE, IS WHAT MAKES IT SAFE — and that is why the flow has three steps instead
 *    of one button. `/api/operator/reset` refuses (409) unless `/api/operator/reset/prepare` has minted a
 *    token, and prepare only mints one after running the IMPORTER'S OWN PARSER over the backup it just
 *    built. It also refuses (400) unless the exact confirm phrase is sent. This UI follows that contract;
 *    it does not invent it, and it reads the phrase from the server rather than hard-coding it, so the page
 *    cannot drift from what the endpoint demands.
 *
 * ⭐ THE 409 IS NOT AN ERROR PATH. Pressing the last button before taking a backup is the server doing its
 *    job, so it is reported as the flow working rather than as a failure.
 */
let RESET = { token: null, phrase: "", url: null, rows: 0, took: false };

function resetArm() {
  // The last button needs BOTH the backup taken away and the phrase typed. Either alone is not consent.
  const typed = ($("#resetConfirm").value || "").trim();
  $("#resetGo").disabled = !(RESET.token && RESET.took && RESET.phrase && typed === RESET.phrase);
}

async function resetPrepare() {
  const msg = $("#resetMsg"), btn = $("#resetPrepare");
  btn.disabled = true;
  msg.innerHTML = `<p class="muted">building the backup and checking it can be restored…</p>`;
  try {
    const r = await fetch("/api/operator/reset/prepare", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.can_reset === false) {
      // ⛔ No token means no reset. Shown as a refusal with its reason, which is the safe direction.
      msg.innerHTML = `<p class="warn">${esc(d.error || `could not prepare a backup (${r.status})`)}</p>`;
      RESET = { token: null, phrase: "", url: null, rows: 0, took: false };
      $("#resetDownload").hidden = true;
      $("#resetConfirmRow").hidden = true;
      resetArm();
      return;
    }
    if (RESET.url) URL.revokeObjectURL(RESET.url);
    RESET.token = d.token;
    RESET.phrase = d.confirm_word || "";
    RESET.rows = d.rows || 0;
    RESET.took = false;
    RESET.url = URL.createObjectURL(new Blob([d.backup_csv || ""], { type: "text/csv" }));
    const dl = $("#resetDownload");
    dl.href = RESET.url;
    dl.download = `bake-off-backup-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.csv`;
    dl.hidden = false;
    $("#resetPhrase").textContent = RESET.phrase;
    $("#resetConfirmRow").hidden = false;
    $("#resetConfirm").value = "";
    const kinds = Object.entries(d.by_event_type || {})
      .sort((a, b) => b[1] - a[1]).map(([k, n]) => `${esc(k)} ${n}`).join(" · ");
    msg.innerHTML =
      `<p class="muted small">Backup ready: <b>${d.rows}</b> rows, ${Math.round((d.bytes || 0) / 1024)} KB` +
      `${d.ordering === "partial" ? " (ordering partial — some rows predate the app's own clock)" : ""}.</p>` +
      `<p class="muted small">The reset will delete <b>${d.will_delete}</b> rows and keep <b>${d.will_keep}</b>` +
      ` (${(d.kept_types || []).map(esc).join(", ")}). Weeks <b>${(d.released_now || []).join(", ") || "none"}</b>` +
      ` stay released.</p>` +
      (kinds ? `<p class="muted small">${kinds}</p>` : "") +
      `<p class="muted small">Download the backup, then type the phrase — the server will not accept the` +
      ` reset without both.</p>`;
  } catch (e) {
    msg.innerHTML = `<p class="warn">${esc(e.message)}</p>`;
  } finally {
    btn.disabled = false;
    resetArm();
  }
}

async function resetRun() {
  const msg = $("#resetMsg"), btn = $("#resetGo");
  btn.disabled = true;
  msg.innerHTML = `<p class="muted">resetting…</p>`;
  try {
    const r = await fetch("/api/operator/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: RESET.token, confirm: ($("#resetConfirm").value || "").trim() }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      // ⭐ THE REFUSALS ARE THE FLOW WORKING. 409 = no verified backup behind this click; 400 = the phrase
      //    does not match. Neither is a fault, so neither is dressed as one.
      const why = d.needs_backup
        ? "Take the backup first — the server will not reset without one. Nothing has been deleted."
        : d.needs_confirm
          ? `The phrase has to match exactly. Nothing has been deleted.`
          : (d.error || `reset refused (${r.status})`);
      msg.innerHTML = `<p class="${d.needs_backup || d.needs_confirm ? "muted" : "warn"}">${esc(why)}</p>`;
      if (d.needs_backup) { RESET.token = null; RESET.took = false; }
      return;
    }
    RESET = { token: null, phrase: "", url: null, rows: 0, took: false };
    $("#resetDownload").hidden = true;
    $("#resetConfirmRow").hidden = true;
    msg.innerHTML =
      `<p><b>${esc(d.message || "Reset.")}</b></p>` +
      `<p class="muted small">Cleared in memory as well as in the table: ${
        (d.cleared || []).map(esc).join(", ")}.</p>`;
    // Everything on the page moved: the log, the stats, the board and this player's own state.
    LOGS.page = 1;
    await loadLogs();
    await loadOperator();
    S.game = await api("/api/game?week=" + S.week).catch(() => S.game);
    render();
  } catch (e) {
    msg.innerHTML = `<p class="warn">${esc(e.message)}</p>`;
  } finally {
    resetArm();
  }
}

async function saveRelease() {
  const weeks = $$("#releaseRows input:checked").map(i => Number(i.value));
  $("#releaseMsg").textContent = "saving…";
  try {
    const r = await api("/api/settings", { unlockable: weeks, note: $("#releaseNote").value || "" });
    $("#releaseMsg").textContent = "released: " + (r.released.join(", ") || "none");
    S.game = await api("/api/game?week=" + S.week);
    render();
  } catch (e) { $("#releaseMsg").textContent = e.message; }
}

/* Sends ONLY `client_message` — deliberately no `unlockable`, because the release list is not
   this control's business and a save that carried a stale copy of it could move which weeks are open.
   The server treats an absent key as "leave it alone" and an empty string as "clear it", which is what
   makes emptying the box the way to remove the line. See the note on /api/settings in app.py. */
async function saveClientMsg() {
  const text = $("#clientMsgInput").value.trim();
  $("#clientMsgMsg").textContent = "saving…";
  try {
    const r = await api("/api/settings", { client_message: text });
    const now = (r.current && r.current.client_message) || "";
    $("#clientMsgMsg").textContent = now ? "showing on the home page now" : "cleared — nothing is shown";
    // Re-read rather than trusting the echo: the home page renders from /api/game, so that is the payload
    // worth proving changed.
    S.game = await api("/api/game?week=" + S.week);
    render();
  } catch (e) { $("#clientMsgMsg").textContent = e.message; }
}


/* ══ The operator unlock wall ═══════════════════════════════════════════════════════════
 * Three states, and the third is the one that is easy to forget:
 *   unlocked                  -> show the operator content
 *   locked, password required  -> show the form
 *   locked, NO password set    -> show WHY, because there is nothing the operator can type. Drawing an
 *                                 empty form here would be a dead end that looks like a wrong password.
 *
 * ⛔ AND THE CONTENT IS NOT MERELY HIDDEN — it is not LOADED. loadOperator()/loadLogs() are the calls
 *    that fetch the log rows and the settings, and every one of those endpoints is server-gated anyway;
 *    firing them while locked would just paint the page with 403s. Hiding a div would also be no kind of
 *    protection, which is why the real gate is server-side and this is only the UI following it.
 */
function applyOpLock() {
  const g = S.game || {};
  const unlocked = !!g.operator_unlocked;
  const required = !!g.operator_password_required;
  const lock = $("#opLock"), body = $("#opBody");
  if (!lock || !body) return;
  lock.hidden = unlocked;
  body.hidden = !unlocked;
  if (unlocked) { loadOperator(); loadLogs(); return; }
  $("#opLockMsg").textContent = required
    ? "This page needs the operator password."
    : (g.operator_locked_reason || "The operator page is closed for this deployment.");
  // No password configured means there is nothing to type, so do not offer a box.
  // ⚠️ UNREACHABLE AS SHIPPED: `operator_password_required` is always true, because an unset
  //    password falls back to the built-in default. Kept for a build that removes that fallback.
  $("#opLockForm").hidden = !required;
  $("#opLockErr").hidden = true;
  if (required) $("#opLockPw").focus();
}

async function opUnlock(ev) {
  if (ev) ev.preventDefault();
  const pw = $("#opLockPw").value;
  const err = $("#opLockErr"), btn = $("#opLockGo");
  if (!pw) { err.hidden = false; err.textContent = "Enter the password."; return; }
  btn.disabled = true;
  err.hidden = true;
  try {
    const r = await fetch("/api/operator/unlock", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      // The server's wording is shown verbatim: it is the thing that says whether a password is
      // configured at all, and it never echoes the value.
      // ⛔ IT NO LONGER SAYS "attempts left" —  DELETED THE LOCKOUT deliberately, and this
      //    comment still described it. A stale comment about a removed throttle is an invitation to
      //    reinstate the throttle, which app.py warns against by name:  counted attempts PER IDENTITY,
      //    so a tester verifying the gate spent the real operator's budget. Verified on the deployment:
      //    eight consecutive wrong passwords, eight identical 401s, no counter and no cooling-off.
      err.hidden = false;
      err.textContent = d.error || `could not unlock (${r.status})`;
      return;
    }
    // ⭐ CLEAR THE FIELD before re-rendering. Leaving the password sitting in a DOM input after a
    //    successful unlock means it survives in the page for as long as the tab is open.
    $("#opLockPw").value = "";
    S.game = await api("/api/game?week=" + S.week);
    applyOpLock();
    render();
  } catch (e) {
    err.hidden = false;
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------------ wiring */
$("#wordmark").addEventListener("click", async () => {
  const next = S.themeName === S.game.theme.name ? S.game.alt_theme_name : S.game.theme.name;
  try { applyTheme(await api("/api/theme?name=" + encodeURIComponent(next))); } catch { /* ignore */ }
});
$$(".nav button").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
$("#chatForm").addEventListener("submit", ask);
$("#chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
});
// false, so New chat invites a question instead of asking one. See newChat().
$("#newChatBtn").addEventListener("click", () => newChat(false));
$("#unlockBtn").addEventListener("click", unlock);
$("#releaseSave").addEventListener("click", saveRelease);
$("#clientMsgSave").addEventListener("click", saveClientMsg);
$("#opLockForm").addEventListener("submit", opUnlock);
$("#logPrev").addEventListener("click", () => { if (LOGS.page > 1) { LOGS.page--; loadLogs(); } });
$("#logNext").addEventListener("click", () => { if (LOGS.page < LOGS.pages) { LOGS.page++; loadLogs(); } });
$("#logSize").addEventListener("change", e => {
  LOGS.size = Number(e.target.value) || 50; LOGS.page = 1; loadLogs();
});
$("#importFile").addEventListener("change", e => {
  IMPORT_FILE = (e.target.files || [])[0] || null;
  $("#importGo").disabled = true;          // a new file must be CHECKED before it can be imported
  $("#importMsg").innerHTML = IMPORT_FILE
    ? `<p class="muted small">${esc(IMPORT_FILE.name)} — press Check file to see what it would do.</p>` : "";
});
$("#importCheck").addEventListener("click", () => importPost(true));
$("#importGo").addEventListener("click", () => importPost(false));
$("#resetPrepare").addEventListener("click", resetPrepare);
// Clicking the download is what proves the backup left the browser — the file exists only in the response.
$("#resetDownload").addEventListener("click", () => { RESET.took = true; resetArm(); });
$("#resetConfirm").addEventListener("input", resetArm);
$("#resetGo").addEventListener("click", resetRun);
document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

boot().catch(e => {
  // ⛔ This REPLACES the overlay's children, so the canvas is detached while the stage keeps its rAF and
  //    its listeners pointing at an element nobody can see. Destroy before rewriting, not after.
  stopBootLamp();
  $("#boot").innerHTML =
    `<div class="boot-inner"><h1>The desk could not open</h1><p>${esc(e.message)}</p></div>`;
});
