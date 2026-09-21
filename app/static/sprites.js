/* Pixel sprites for the two stages — the genie lamp and the week's scene.
 *
 * Indie-RPG look, drawn as real pixels: small character grids scaled up with smoothing off, which is
 * what gives the chunky Undertale-ish edge. No image files, no sprite sheet, no CDN — the app must work
 * in an air-gapped workspace, and a blocked host hangs rather than failing.
 *
 * ⚡ THE ANIMATION BUDGET, because "quick and responsive" was asked for in the same breath as the
 * animations. Every frame is rasterised ONCE at load into an offscreen canvas, so animating costs one
 * drawImage per frame and nothing else — no per-pixel work, no layout, no rAF storm. Frames advance on a
 * single 8fps timer per stage, and the timer is STOPPED while the tab is hidden and while a stage is off
 * screen. Two stages, sixteen tiny frames, two timers: the page does not get heavier as the game goes on.
 */
(function (global) {
  "use strict";

  /* ── a sprite is rows of single characters; "." is transparent ───────────────────────────────── */
  function sprite(rows) {
    return { w: Math.max.apply(null, rows.map(function (r) { return r.length; })), h: rows.length, rows: rows };
  }

  function blank(w, h) {
    var rows = [];
    for (var y = 0; y < h; y++) rows.push(new Array(w + 1).join("."));
    return { w: w, h: h, rows: rows };
  }

  /* stamp `s` onto `t` at (x,y); "." in the source leaves the target alone */
  function stamp(t, s, x, y) {
    for (var j = 0; j < s.h; j++) {
      var ty = y + j;
      if (ty < 0 || ty >= t.h) continue;
      var row = t.rows[ty].split(""), src = s.rows[j];
      for (var i = 0; i < src.length; i++) {
        var tx = x + i, c = src[i];
        if (c === "." || tx < 0 || tx >= t.w) continue;
        row[tx] = c;
      }
      t.rows[ty] = row.join("");
    }
    return t;
  }

  function rect(t, x, y, w, h, c) {
    for (var j = 0; j < h; j++) {
      var row = t.rows[y + j];
      if (row === undefined) continue;
      var a = row.split("");
      for (var i = 0; i < w; i++) if (x + i >= 0 && x + i < t.w) a[x + i] = c;
      t.rows[y + j] = a.join("");
    }
    return t;
  }

  function dot(t, x, y, c) { return rect(t, x, y, 1, 1, c); }

  /* ── rasterise one grid to an offscreen canvas, one pixel per cell ───────────────────────────── */
  function rasterise(grid, palette) {
    var c = document.createElement("canvas");
    c.width = grid.w; c.height = grid.h;
    var g = c.getContext("2d");
    for (var y = 0; y < grid.h; y++) {
      var row = grid.rows[y];
      for (var x = 0; x < row.length; x++) {
        var col = palette[row[x]];
        if (!col) continue;
        g.fillStyle = col;
        g.fillRect(x, y, 1, 1);
      }
    }
    return c;
  }

  /* ── the animator: pre-rasterised frames, one drawImage per tick, stops when hidden ──────────── */
  function Stage(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.ctx.imageSmoothingEnabled = false;
    this.sets = {};                 // name -> [offscreen canvas]
    this.set = null;
    this.frame = 0;
    this.fps = (opts && opts.fps) || 8;
    this.timer = null;
    this.bg = (opts && opts.bg) || null;
    var self = this;
    this._vis = function () { if (document.hidden) self.stop(); else self.start(); };
    document.addEventListener("visibilitychange", this._vis);
    // One listener per stage, removed by destroy(), so switching week cannot accumulate them. The
    // ResizeObserver is what makes this correct rather than lucky: it fires when the element FIRST gets a
    // box, which is after <main> is unhidden, and again on every layout change.
    this._resize = function () { self.fit(); };
    window.addEventListener("resize", this._resize);
    if (typeof ResizeObserver === "function") {
      this._ro = new ResizeObserver(this._resize);
      this._ro.observe(canvas);
    }
  }

  Stage.prototype.define = function (name, grids, palette) {
    this.sets[name] = grids.map(function (g) { return rasterise(g, palette); });
    return this;
  };

  Stage.prototype.show = function (name) {
    if (!this.sets[name]) return this;
    if (this.set === name) return this;
    this.set = name;
    this.frame = 0;
    this.fit();
    this.draw();
    this.start();
    return this;
  };

  Stage.prototype.destroy = function () {
    this.stop();
    document.removeEventListener("visibilitychange", this._vis);
    window.removeEventListener("resize", this._resize);
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
  };

  /* Match the backing store to the box the CSS gave us. Both stages are the SAME HEIGHT and different
     widths, so neither can rely on a fixed internal size and still land on whole pixels.
     ⛔ A ZERO-SIZED BOX IS NOT A SIZE. The first version clamped it to 32x32 and considered the job done —
     and stages are built while <main> is still `hidden`, so every stage got a 32x32 backing store holding
     the top-left corner of its sprite, which CSS then stretched thirty times across and twelve times down.
     The canvas reported a perfectly plausible 959x298 box the whole time; only the screenshot showed the
     smear. So: refuse to fit an unmeasurable box, and let the ResizeObserver call again when it has one. */
  Stage.prototype.fit = function () {
    var r = this.canvas.getBoundingClientRect();
    var w = Math.round(r.width), h = Math.round(r.height);
    if (w < 8 || h < 8) return this;                 // not laid out yet — decline, do not guess
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
      this.ctx.imageSmoothingEnabled = false;
      this.draw();
    }
    return this;
  };

  Stage.prototype.draw = function () {
    var frames = this.sets[this.set];
    if (!frames || !frames.length) return;
    var img = frames[this.frame % frames.length];
    var W = this.canvas.width, H = this.canvas.height;
    this.ctx.clearRect(0, 0, W, H);
    if (this.bg) { this.ctx.fillStyle = this.bg; this.ctx.fillRect(0, 0, W, H); }
    // integer scale so a pixel stays a square, centred
    var s = Math.max(1, Math.floor(Math.min(W / img.width, H / img.height)));
    var w = img.width * s, h = img.height * s;
    this.ctx.imageSmoothingEnabled = false;
    this.ctx.drawImage(img, Math.floor((W - w) / 2), Math.floor((H - h) / 2), w, h);
  };

  /* ⛔ THE CANVAS DID NOT HONOUR prefers-reduced-motion, AND THE CSS DID — so the page half-claimed a
     setting it half-ignored, which is worse than not claiming it. Verified by an independent control run:
     the sprites animated identically with the setting on and off, because nothing in this file ever asked.
     With it on, a stage renders its FIRST FRAME and never advances: the art is all still there, it simply
     holds still. The query is re-read on every start, so toggling the OS setting takes effect without a
     reload, and a browser too old for matchMedia keeps the animation rather than losing the art. */
  function reducedMotion() {
    try {
      return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) {
      return false;
    }
  }

  Stage.prototype.start = function () {
    if (this.timer || document.hidden) return;
    if (reducedMotion()) { this.frame = 0; this.draw(); return; }
    var self = this;
    this.timer = setInterval(function () {
      var frames = self.sets[self.set];
      if (!frames || frames.length < 2) return;      // a single-frame set needs no timer work
      self.frame++;
      self.draw();
    }, Math.round(1000 / this.fps));
  };

  Stage.prototype.stop = function () {
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
  };

  /* ══════════════════════════════════════════════════ the lamp ══════════════════════════════════ */
  /* Drawn from the project's genie logo rather than from a generic oil lamp: a flat PALE SALMON silhouette —
   * curled handle on the LEFT, low bowl, spout sweeping up to the RIGHT — a solid CORAL rounded base bar
   * beneath it, and a four-pointed CORAL SPARKLE above where a flame would be. No flame, no brass.
   *
   * ⚠️ The palette is NOT eyedroppered from the logo tile. The logo's lamp is pale because it sits on
   * WHITE; this panel is near-black, so the same fill is a different contrast problem. The shape and the
   * sparkle are taken faithfully, the fills were chosen by MEASURED contrast against this panel
   * — the brief said "closer to", not "identical".
   */
  var LAMP_PAL = {
    p: "#F2C4BC",   // the logo's pale salmon body
    q: "#DDA49B",   // one step down, for the inner edge so the silhouette is not a flat blob
    c: "#EF6A4E",   // the logo's coral — sparkle and base bar
    C: "#FF9B82",   // sparkle highlight
    e: "#7A3326",   // the genie's eyes and mouth, same family
    s: "#E3A9A0",   // smoke
    S: "#F7D9D3",   // smoke, lit
    w: "#39506B", W: "#8FA6D8"     // the starfield, unchanged
  };

  /* handle · bowl · spout, as one silhouette. 40 wide. */
  var LAMP_BODY = sprite([
    "......................................pp",
    ".....................................ppp",
    "...................................ppp..",
    ".................................ppp....",
    "...............................ppp......",
    "..............................pp........",
    "....pppppppppppppppppppppppppppp........",
    "..pppppppppppppppppppppppppppppp........",
    ".pppqqqpppppppppppppppppppppppp.........",
    ".ppq...qppppppppppppppppppppp...........",
    ".pq.....pppppppppppppppppp..............",
    "..q....ppppppppppppppppp................",
    "...qqqppppppppppppppp...................",
    "......ppppppppppppp.....................",
    ".......ppppppppp........................"
  ]);

  var LAMP_BASE = sprite([
    "..cccccccccccc..",
    ".cccccccccccccc.",
    "cccccccccccccccc"
  ]);

  /* The four-pointed sparkle, three sizes so it can breathe. Concave between the points, which is what
     makes it read as a sparkle rather than a plus sign. */
  function sparkle(size) {
    if (size === 0) return sprite([
      "..C..",
      "..C..",
      "CCCCC",
      "..C..",
      "..C.."
    ]);
    if (size === 1) return sprite([
      "...c...",
      "...C...",
      "..CCC..",
      "cCCCCCc",
      "..CCC..",
      "...C...",
      "...c..."
    ]);
    return sprite([
      ".....c.....",
      ".....C.....",
      "....CCC....",
      "....CCC....",
      "...CCCCC...",
      "cCCCCCCCCCc",
      "...CCCCC...",
      "....CCC....",
      "....CCC....",
      ".....C.....",
      ".....c....."
    ]);
  }

  /* a smoky genie head, in the lamp's own palette, three mouth shapes */
  function genieHead(mouth) {
    var rows = [
      "...sSSSSSs...",
      "..sSSSSSSSs..",
      ".sSSSSSSSSSs.",
      "sSSSSSSSSSSSs",
      "sSSeSSSSSeSSs",
      "sSSSSSSSSSSSs",
      ".sSSSSSSSSSs.",
      "..sSSeeeSSs..",
      "...sSSSSSs...",
      "....ssssss..."
    ];
    rows[7] = mouth === "open" ? "..sSeeeSSs..." : mouth === "wide" ? ".sSeeeeeSSs.."
            : "..sSSeeSSSs..";
    return sprite(rows);
  }

  function lampScene(opts) {
    var t = blank(44, 34);
    var stars = [[2, 3], [8, 1], [15, 5], [30, 2], [40, 6], [5, 11], [38, 14], [24, 2]];
    for (var i = 0; i < stars.length; i++) dot(t, stars[i][0], stars[i][1], (i % 3) ? "w" : "W");
    if (opts.head) stamp(t, opts.head, 14, 1 + (opts.headY || 0));
    else stamp(t, sparkle(opts.spark === undefined ? 2 : opts.spark), 13 + (opts.sparkDx || 0),
               4 + (opts.sparkDy || 0));
    if (opts.smoke) for (var j = 0; j < opts.smoke.length; j++) {
      dot(t, opts.smoke[j][0], opts.smoke[j][1], opts.smoke[j][2] || "s");
    }
    var x = 2 + (opts.dx || 0), y = 15 + (opts.dy || 0);
    stamp(t, LAMP_BODY, x, y);
    stamp(t, LAMP_BASE, x + 10, y + 15);
    return t;
  }

  function lampSets(stage) {
    // RESTING — the sparkle breathes and the lamp lifts a pixel. Two frames, no more.
    stage.define("idle", [
      lampScene({ spark: 2, dy: 0 }),
      lampScene({ spark: 1, sparkDx: 2, sparkDy: 2, dy: 1 })
    ], LAMP_PAL);

    // THINKING — the sparkle shrinks to a glint, the lamp rattles, smoke climbs in a column.
    stage.define("thinking", [
      lampScene({ spark: 1, sparkDx: 2, sparkDy: 2, dx: 0, smoke: [[34, 12, "S"], [36, 9, "s"]] }),
      lampScene({ spark: 0, sparkDx: 3, sparkDy: 3, dx: 1, smoke: [[35, 10, "S"], [36, 7, "s"], [34, 5, "s"]] }),
      lampScene({ spark: 1, sparkDx: 2, sparkDy: 2, dx: 0, smoke: [[36, 8, "S"], [34, 6, "s"], [36, 3, "s"]] }),
      lampScene({ spark: 0, sparkDx: 3, sparkDy: 3, dx: -1, smoke: [[34, 7, "S"], [36, 4, "s"]] })
    ], LAMP_PAL);

    // TALKING — the smoke has become a head, and the mouth moves.
    stage.define("talking", [
      lampScene({ head: genieHead("open"), smoke: [[33, 13, "S"], [35, 15, "s"]] }),
      lampScene({ head: genieHead("shut"), smoke: [[34, 13, "S"], [33, 15, "s"]] }),
      lampScene({ head: genieHead("wide"), headY: -1, smoke: [[33, 13, "S"], [35, 16, "s"]] }),
      lampScene({ head: genieHead("shut"), smoke: [[34, 14, "S"], [33, 15, "s"]] })
    ], LAMP_PAL);

    // ASLEEP — the failed state. The sparkle is GONE and the lamp is dim, so a genie that could not be
    // reached is visibly different from one that is thinking. A lamp that thinks forever is the worst
    // outcome: it is indistinguishable from slow, and it silently turns the health check into nothing.
    stage.define("asleep", [
      lampScene({ spark: 0, sparkDx: 90, dy: 1 })      // sparkle stamped off-canvas
    ], { p: "#6E5A57", q: "#5A4846", c: "#7A4438", C: "#7A4438", e: "#3A2A28",
         s: "#4A3C3A", S: "#5A4846", w: "#2A3A4E", W: "#4A5E7A" });
    return stage;
  }

  /* ══════════════════════════════════════════════ the week scenes ═══════════════════════════════ */
  /* Composed from motifs rather than hand-drawn at 64x24, so four scenes cost four short functions. */
  var SCENE_PAL = {
    k: "#1B3139", d: "#2E4A55", p: "#F4F1EA", P: "#FFFFFF", i: "#8FA2AA",
    r: "#FF3621", R: "#FF7A66", y: "#FFAB00", g: "#00A972", b: "#2272B4",
    n: "#9A5F17", N: "#D89A2B", w: "#C9D3D7", o: "#7A5230", c: "#EF6A4E",
    f: "#E8B893", K: "#3A2C22"        // a face tone and hair, so the CFO reads as a person not a statue
  };

  var JAR = sprite([
    ".iiii.",
    "iddddi",
    "iNnnNi",
    "iNnnNi",
    "iNnnNi",
    ".iiii."
  ]);
  var PIN = sprite([".r.", "rrr", ".r.", ".k."]);
  var COIN = sprite([".yy.", "yNNy", "yNNy", ".yy."]);

  function sceneBase() {
    var t = blank(64, 24);
    rect(t, 0, 21, 64, 3, "d");                       // desk edge
    return t;
  }

  /* WEEK 1 — the CFO leaning in and asking for the report, quickly. A figure behind the table with a
     speech bubble, and on the table the baked goods and the receipts the memo is about. The bubble is the
     thing that moves: it is what makes him read as ASKING rather than standing there. */
  var COOKIE = sprite([
    ".nnnn.",
    "nncnnn",
    "nnnncn",
    "ncnnnn",
    ".nnnn."
  ]);
  var CROISSANT = sprite([
    "..NNN..",
    ".NNnNN.",
    "NNn.nNN",
    ".NN.NN."
  ]);

  function cfoScene(phase) {
    var t = sceneBase();
    // ── the CFO, left of frame and behind the table
    rect(t, 6, 3, 9, 2, "K");                        // hair
    rect(t, 7, 5, 7, 6, "f");                        // face
    rect(t, 8, 7, 2, 1, "k"); rect(t, 12, 7, 2, 1, "k");    // eyes
    rect(t, 9, 9, 4, 1, "k");                        // mouth, open — he is mid-sentence
    rect(t, 4, 11, 13, 9, "b");                      // suit
    rect(t, 9, 11, 3, 7, "P");                       // shirt
    rect(t, 10, 12, 1, 6, "r");                      // tie
    rect(t, 16, 14, 5, 2, "b");                      // sleeve reaching toward the table
    rect(t, 20, 14, 3, 2, "f");                      // and the hand at the end of it
    // ── the speech bubble: two beats, so he reads as ASKING rather than standing there
    var up = (phase % 2) === 0;
    var by = 1 + (up ? 0 : 1), bw = up ? 18 : 16;
    rect(t, 24, by, bw, 10, "P");
    rect(t, 24, by, bw, 1, "k"); rect(t, 24, by + 9, bw, 1, "k");
    rect(t, 24, by, 1, 10, "k"); rect(t, 23 + bw, by, 1, 10, "k");
    rect(t, 25, by + 10, 3, 2, "P"); rect(t, 25, by + 11, 3, 1, "k");   // the tail
    rect(t, 28, by + 2, 2, 5, "r"); rect(t, 28, by + 8, 2, 1, "r");     // "!"
    rect(t, 34, by + 2, 7, 7, "k");                                     // a clock: quickly
    rect(t, 35, by + 3, 5, 5, "P");
    rect(t, 37, by + 5, 1, 1, "k");
    rect(t, 37, by + 3 + (up ? 0 : 1), 1, 2, "k");                      // the hand sweeps
    rect(t, 37 + (up ? 1 : 0), by + 5, 2, 1, "k");
    // ── the table, and on it the receipts the memo is about and the goods it is selling
    rect(t, 2, 20, 60, 2, "o");
    rect(t, 24, 13, 9, 7, "p");                      // the receipt roll, standing clear of the figure
    for (var y = 14; y < 20; y += 2) rect(t, 25, y, 7, 1, "i");
    // loose receipts spilling off the pad onto the table, and they are what moves
    var fold = [[33, 18], [35, 19]];
    for (var i = 0; i < fold.length; i++) {
      var on = ((i + phase) % 2) === 0;
      rect(t, fold[i][0] + (on ? 0 : 1), fold[i][1], 7, 1, on ? "P" : "p");
    }
    rect(t, 42, 19, 18, 1, "c");                     // the plate they all sit ON, so nothing floats
    stamp(t, COOKIE, 43, 14);
    stamp(t, COOKIE, 50, 14);
    stamp(t, CROISSANT, 43, 10);                     // on the shelf edge behind, not mid-air
    rect(t, 43, 9, 7, 1, "o");                       // ... which needs a shelf under it
    rect(t, 56, 15, 4, 4, "N"); rect(t, 55, 14, 6, 1, "n");   // a coffee cup beside them
    return t;
  }

  function shelfScene(phase) {
    var t = sceneBase();
    rect(t, 4, 14, 56, 2, "o");                       // the shelf
    rect(t, 4, 5, 56, 1, "o");
    for (var i = 0; i < 6; i++) {
      var x = 6 + i * 9;
      stamp(t, JAR, x, 8);
      if (i === 1) {                                  // the hero jar, glinting
        var on = (phase % 2) === 0;
        rect(t, x + 1, 9, 4, 1, on ? "P" : "w");
        dot(t, x + 5, 8, on ? "y" : "N");
      }
    }
    rect(t, 6, 17, 10, 3, "p"); rect(t, 7, 18, 8, 1, "i");     // a price label
    return t;
  }

  function mapScene(phase) {
    var t = sceneBase();
    rect(t, 3, 3, 58, 17, "b");                       // the map sheet
    // continents, blocky on purpose
    rect(t, 7, 6, 12, 7, "g"); rect(t, 9, 13, 6, 3, "g");
    rect(t, 26, 5, 9, 5, "g"); rect(t, 28, 10, 6, 6, "g");
    rect(t, 42, 6, 13, 6, "g"); rect(t, 46, 12, 7, 4, "g");
    var pins = [[48, 4], [50, 9], [30, 4], [12, 5], [52, 12], [10, 12]];
    for (var i = 0; i < pins.length; i++) {
      var lift = (i === (phase % pins.length)) ? -1 : 0;       // one pin bobs at a time
      stamp(t, PIN, pins[i][0], pins[i][1] + lift);
    }
    return t;
  }

  /* A round lens, drawn as a pixel circle. The first version was a rectangle and read as a white CARD
     rather than a magnifying glass — found by looking at it, not by any check. */
  var LENS = sprite([
    "....kkkk....",
    "..kkPPPPkk..",
    ".kPPPPPPPPk.",
    ".kPPPPPPPPk.",
    "kPPPPPPPPPPk",
    "kPPPPPPPPPPk",
    "kPPPPPPPPPPk",
    "kPPPPPPPPPPk",
    ".kPPPPPPPPk.",
    ".kPPPPPPPPk.",
    "..kkPPPPkk..",
    "....kkkk...."
  ]);

  function magnifierScene(phase) {
    var t = sceneBase();
    rect(t, 4, 8, 28, 13, "p");                       // the receipt under inspection
    for (var y = 10; y < 20; y += 2) rect(t, 6, y, 18, 1, "i");
    var lx = 22 + phase * 4;                          // the glass sweeps across it
    stamp(t, LENS, lx, 4);
    rect(t, lx + 3, 9, 6, 1, "r"); rect(t, lx + 3, 12, 4, 1, "r");   // what the lens magnifies
    rect(t, lx + 10, 15, 3, 3, "o");                  // the handle, angled away from the lens
    rect(t, lx + 12, 17, 3, 4, "o");
    rect(t, 52, 3, 3, 9, "k"); rect(t, 48, 2, 10, 2, "y");    // a desk lamp, top right
    rect(t, 49, 4, 8, 1, "y");
    return t;
  }

  var SCENES = { cfo: cfoScene, shelf: shelfScene, map: mapScene, magnifier: magnifierScene };
  SCENES.receipts = cfoScene;        // the old name, so an older pack still renders something

  function sceneStage(canvas, name) {
    var st = new Stage(canvas, { fps: 3 });           // a scene breathes; it does not flicker
    var build = SCENES[name] || cfoScene;
    st.define(name, [build(0), build(1), build(2), build(1)], SCENE_PAL);
    st.show(name);
    // fit() AFTER show: without it the canvas kept its 480x180 HTML attributes while CSS stretched the
    // box to 1270x300 — 2.6x across and 1.7x down, so every pixel was a smeared rectangle. Caught by
    // looking at it; the canvas reported a perfectly sensible size the whole time.
    st.fit();
    return st;
  }

  /* ══════════════════════════════════════════════════════════════════════════════════════════════
     THE TWO ANIMATIONS, FROM THE PROJECT'S OWN ART

     Two stills were supplied, to be animated. They are served by this app from /art/ — no
     external request, same rule as everything else here.

     ⛔ THREE THINGS ABOUT THE ART THAT WOULD OTHERWISE SHIP AS BUGS. All three are measurements taken
        off the files, not impressions of them:

     1. THE CFO STILL ALREADY HAS "Just give me a report!" PAINTED INTO THE PIXELS, inside a white-bordered
        bubble at x[529..1071] y[51..282] of the asset. The procedural `cfoScene` below drew its OWN bubble
        with a "!" and a clock in it, so using the art AND that scene would have shown the man saying two
        things at once. The art wins and the drawn bubble is gone — week 1 no longer routes to cfoScene.
        What animates instead is a glow that travels the bubble's own border, so he still reads as ASKING
        rather than standing there, which is what the drawn bubble's two-beat wobble was for.

     2. THE SOURCE FILES ARE LETTERBOXED ONTO BLACK, and not symmetrically in the way you would guess.
        The lamp is 1024x1024 of which the art occupies x[232..874] y[129..696] — it is NOT centred, and
        there is 327px of dead space under it against 129 above, so dropping the raw square into a panel
        renders a small lamp sitting high with a gap beneath. The CFO is 2816x1536 with a 328px black bar
        on the LEFT *and* a 328px one on the RIGHT. Both assets are pre-cropped to their real content, and
        the lamp's black is keyed to alpha so it floats on the panel's own gradient instead of shipping a
        visible box on a themed page.

     3. THEIR ASPECT RATIOS ARE NOTHING ALIKE — 1.13:1 for the lamp against 1.41:1 for the CFO — while both
        stages are deliberately ONE height (asked for, and x1.5 of it since).
        So neither is allowed to dictate the layout: each is fitted to whatever box the CSS gives it. The
        week stage is much wider than the art, so the CFO's own edge columns are stretched outward to carry
        the room to the panel edges; the lamp needs no such thing because it is transparent.
     ══════════════════════════════════════════════════════════════════════════════════════════════ */

  function clock() {
    return (typeof performance === "object" && performance.now) ? performance.now() : Date.now();
  }

  /* An image-backed stage with procedural motion on top. Same surface as Stage (fit/show/destroy) so the
     app can hold either kind without knowing which it has. */
  function ImageStage(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.artW = opts.artW;                 // declared, so geometry works before the image arrives
    this.artH = opts.artH;
    this.mode = opts.mode || "contain";    // "contain" | "extend" (carry the scene to the panel edges)
    this.pad = opts.pad || 0;              // fraction of the height left clear above and below
    this.paint = opts.paint;               // (ctx, g, t, state) -> the animated layers
    this.state = opts.state || "idle";
    this.raf = null;
    this.ready = false;
    this.failed = false;
    this.t0 = clock();
    var self = this;

    // a stage may carry EXTRA layers. The lamp needs them because the star moves, and a star
    // baked into one bitmap cannot move — so the art is split into lamp-without-star plus star.
    this.extra = {};
    var pending = 1 + Object.keys(opts.layers || {}).length;
    var done = function () { if (--pending === 0) { self.ready = true; self.fit(); self.draw(); self.start(); } };
    Object.keys(opts.layers || {}).forEach(function (k) {
      var im = new Image();
      im.onload = done;
      im.onerror = function () { self.failed = true;
        if (global.console) console.error("[sprites] could not load " + opts.layers[k]); done(); };
      im.src = opts.layers[k];
      self.extra[k] = im;
    });
    this.img = new Image();
    this.img.decoding = "sync";
    this.img.onload = done;
    // ⛔ A MISSING ASSET MUST NOT LEAVE AN EMPTY PANEL THAT LOOKS LIKE A STYLING BUG. If the image cannot
    //    be fetched the stage says so once, in the console, and paints the panel's own ink so the caption
    //    still has something to sit on.
    this.img.onerror = function () {
      self.failed = true;
      if (global.console) console.error("[sprites] could not load " + opts.src);
      done();
    };
    this.img.src = opts.src;

    this._vis = function () { if (document.hidden) self.stop(); else self.start(); };
    document.addEventListener("visibilitychange", this._vis);
    this._resize = function () { self.fit(); };
    global.addEventListener("resize", this._resize);
    if (typeof ResizeObserver === "function") {
      this._ro = new ResizeObserver(this._resize);
      this._ro.observe(canvas);
    }
  }

  /* Backing store at up to 2x the CSS box. The procedural Stage above deliberately works at 1:1 because
     it scales whole sprite pixels by an integer; this one is resampling a photograph, where a retina
     backing store is simply sharper. Capped at 2 so a 3x phone does not pay for a 9x fill. */
  ImageStage.prototype.fit = function () {
    var r = this.canvas.getBoundingClientRect();
    var w = Math.round(r.width), h = Math.round(r.height);
    if (w < 8 || h < 8) return this;              // not laid out yet — decline, do not guess
    var dpr = Math.min(2, global.devicePixelRatio || 1);
    var W = Math.round(w * dpr), H = Math.round(h * dpr);
    if (this.canvas.width !== W || this.canvas.height !== H) {
      this.canvas.width = W;
      this.canvas.height = H;
      this.draw();
    }
    return this;
  };

  /* Where the art lands inside the canvas, plus a mapper from art coordinates to canvas ones so the
     animated layers can be positioned against measurements taken off the file. */
  ImageStage.prototype.geom = function () {
    var W = this.canvas.width, H = this.canvas.height;
    // Fit by HEIGHT, because both stages deliberately share one. `pad` keeps a transparent subject off
    // the panel's own border — the lamp's pedestal sat exactly on it and read as clipped.
    var inner = H * (1 - 2 * this.pad);
    var s = inner / this.artH;
    var w = this.artW * s;
    var x = (W - w) / 2;
    var y = (H - inner) / 2;
    return {
      W: W, H: H, s: s, x: x, y: y, w: w, h: inner,
      ax: function (n) { return x + n * s; },     // art x -> canvas x
      ay: function (n) { return y + n * s; },     // art y -> canvas y
      as: function (n) { return n * s; }          // art length -> canvas length
    };
  };

  /* make him say something for a while. `t` in paint is seconds since this stage started, so the
     deadline is stored in the same unit x1000 to avoid a second clock. */
  ImageStage.prototype.say = function (text, ms) {
    if (!text) return this;
    ms = ms || 3200;
    this.speech = { text: text, ms: ms, until: (clock() - this.t0) + ms };
    this.start();
    this.draw();
    return this;
  };

  ImageStage.prototype.show = function (state) {
    if (this.state === state) return this;
    this.state = state;
    this.stateAt = clock();          // when this state began, so a state can ramp its own marks in
    this.draw();
    this.start();
    return this;
  };

  ImageStage.prototype.draw = function () {
    var ctx = this.ctx, W = this.canvas.width, H = this.canvas.height;
    if (!W || !H) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!this.ready) return;                      // the CSS panel ink shows through until it loads
    var g = this.geom();
    var t = (clock() - this.t0) / 1000;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";

    if (this.mode === "extend" && g.x > 0.5) {
      // Carry the room out to the panel edges. The week stage is ~3.7:1 and the art is 1.41:1, so there is
      // a lot of panel to fill and "contain" alone would ship two dark slabs — the very thing cropping the
      // letterbox bars off the source was for.
      // ⛔ DO NOT MIRROR THE OUTER SLICE TO FILL THIS. I tried it and LOOKED at it: the gap is ~600 of
      //    1938 canvas px PER SIDE, so the mirrored source was half the artwork and the panel showed
      //    THREE CFOs and THREE speech bubbles, two of them back to front — the exact "two bubbles"
      //    defect this lane was warned about, reintroduced by the fix for a cosmetic one. The subject
      //    occupies the full width of this art; there is no background-only margin to reflect.
      //    So the edge COLUMN is stretched instead. It carries the room's horizontal bands (wall,
      //    skirting, floor) out to the panel edge and cannot duplicate anything, and the vignette below
      //    sinks it so the eye goes to the art.
      var gap = Math.ceil(g.x) + 1;
      ctx.drawImage(this.img, 0, 0, 2, this.artH, 0, g.y, gap, g.h);
      ctx.drawImage(this.img, this.artW - 2, 0, 2, this.artH, Math.floor(g.x + g.w) - 1, g.y, gap, g.h);
    }
    ctx.drawImage(this.img, g.x, g.y, g.w, g.h);
    if (this.mode === "extend" && g.x > 0.5) {
      // and let the mirrored part recede, so the eye goes to the art rather than to the join
      var vg = ctx.createLinearGradient(0, 0, g.W, 0);
      vg.addColorStop(0, "rgba(10,14,20,.86)");
      vg.addColorStop(Math.max(0.001, (g.x * 0.92) / g.W), "rgba(10,14,20,0)");
      vg.addColorStop(Math.min(0.999, (g.x + g.w + (g.W - g.x - g.w) * 0.08) / g.W), "rgba(10,14,20,0)");
      vg.addColorStop(1, "rgba(10,14,20,.86)");
      ctx.fillStyle = vg;
      ctx.fillRect(0, 0, g.W, H);
    }
    if (this.paint) this.paint(ctx, g, t, this.state);
  };

  ImageStage.prototype.start = function () {
    if (this.raf !== null || !this.ready) return this;
    // Same contract as the procedural stage: with prefers-reduced-motion the art is all still there, it
    // simply holds still. Re-read on every start, so toggling the OS setting needs no reload.
    if (reducedMotion()) return this;
    var self = this;
    var step = function () {
      if (document.hidden) { self.raf = null; return; }
      self.draw();
      self.raf = global.requestAnimationFrame(step);
    };
    this.raf = global.requestAnimationFrame(step);
    return this;
  };

  ImageStage.prototype.stop = function () {
    if (this.raf !== null) { global.cancelAnimationFrame(this.raf); this.raf = null; }
    return this;
  };

  ImageStage.prototype.destroy = function () {
    this.stop();
    document.removeEventListener("visibilitychange", this._vis);
    global.removeEventListener("resize", this._resize);
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
  };

  /* ── little drawing helpers for the layers ─────────────────────────────────────────────────── */
  function glow(ctx, cx, cy, r, rgb, a) {
    if (r <= 0 || a <= 0) return;
    var gr = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
    gr.addColorStop(0, "rgba(" + rgb + "," + a + ")");
    gr.addColorStop(0.45, "rgba(" + rgb + "," + (a * 0.38).toFixed(3) + ")");
    gr.addColorStop(1, "rgba(" + rgb + ",0)");
    ctx.fillStyle = gr;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
  }

  function star4(ctx, cx, cy, r, thin, fill, a) {
    ctx.save();
    ctx.globalAlpha = a;
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.moveTo(cx, cy - r);
    ctx.quadraticCurveTo(cx + thin, cy - thin, cx + r, cy);
    ctx.quadraticCurveTo(cx + thin, cy + thin, cx, cy + r);
    ctx.quadraticCurveTo(cx - thin, cy + thin, cx - r, cy);
    ctx.quadraticCurveTo(cx - thin, cy - thin, cx, cy - r);
    ctx.fill();
    ctx.restore();
  }

  /* ⭐ SPARKLES WHILE THE GENIE IS TALKING, by request: while Genie is talking the genie
     animation also shows sparkles.

     ⛔ WHY THIS IS A NEW MARK RATHER THAN A BRIGHTER GLOW. `typing` and `talking` were already separate
     states in the code, but the only difference between them was speed and brightness (sp 4.0 vs 5.0,
     lift 1.45 vs 1.6, 8 motes vs 10) — a difference nobody can NAME, which is exactly why he perceives
     the two as one undifferentiated moment. So each state now has its own KIND of motion:
        thinking : the flame FLICKERS   (three non-commensurate sines — )
        typing   : the star CIRCLES     (loopiness ~0.78 — )
        talking  : SPARKLES             (this)
     Three kinds, not three speeds. A person can say which one they are looking at.

     Positions are polar around the star's own moving centre, so the field belongs to the star rather than
     to the canvas, and every distance is in ART px through g.as() so it scales with the panel.
     Each sparkle has its own orbit rate AND its own twinkle rate, both non-commensurate with the others —
     in lockstep they read as one throbbing ring, which is the failure mode this avoids. */
  var SPARKS = [
    { ang: 0.00, rad: 132, orb: 0.55, tw: 3.1, sz: 21 },
    { ang: 0.85, rad: 104, orb: -0.42, tw: 4.3, sz: 15 },
    { ang: 1.70, rad: 152, orb: 0.31, tw: 2.6, sz: 24 },
    { ang: 2.55, rad: 88, orb: -0.61, tw: 5.1, sz: 13 },
    { ang: 3.40, rad: 140, orb: 0.47, tw: 3.7, sz: 18 },
    { ang: 4.25, rad: 112, orb: -0.35, tw: 4.9, sz: 14 },
    { ang: 5.10, rad: 160, orb: 0.39, tw: 2.9, sz: 22 },
    { ang: 5.95, rad: 96, orb: -0.52, tw: 4.1, sz: 17 }
  ];
  function sparkles(ctx, g, t, cx, cy, hue, gain) {
    for (var i = 0; i < SPARKS.length; i++) {
      var s = SPARKS[i];
      var a = s.ang + t * s.orb;
      // A twinkle that spends real time NEAR ZERO, so sparkles wink in and out instead of all sitting
      // there at half brightness: sin^4 is ~0 for most of its period and peaks briefly.
      var q = Math.pow(0.5 + 0.5 * Math.sin(t * s.tw + s.ang * 2.3), 4);
      var breathe = 1 + 0.10 * Math.sin(t * 1.9 + i);
      var r = g.as(s.rad * breathe);
      /* The vertical component is flattened so the field sits inside the panel rather than reaching for
         its top edge. ⛔ A NARROWER 0.64 WAS TRIED AND REVERTED: a clipping check thresholded at alpha>20
         reported paint in the canvas's first row, I reasoned out a cause (arms of the outermost sparkle),
         narrowed the field — and the count did not improve, because the cause was wrong. What is actually
         up there is 3 pixels of a glow's outermost tail at maxAlpha 21/255, transient, with the first row
         of real ink at y=32, BELOW the art frame's own top edge. So nothing was clipped, the arithmetic
         had named the wrong thing, and the narrowing only cost amplitude on the item whose whole point is
         more of it. The check now gates on alpha>80. */
      var x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r * 0.82;   // flattened, sits better in the panel
      var size = g.as(s.sz) * (0.35 + 0.85 * q) * gain;
      if (size < 0.4) continue;
      star4(ctx, x, y, size, Math.max(0.8, size * 0.18), "#fff", (0.20 + 0.70 * q) * gain);
      glow(ctx, x, y, size * 1.9, hue, (0.06 + 0.26 * q) * gain);
    }
  }

  /* ── THE GENIE LAMP ────────────────────────────────────────────────────────────────────────────
     Anchors measured on the asset (643x568): the sparkle's white core sits at ~(280,120) and the
     spout flame at ~(528,215). The four existing states are kept — resting, thinking,
     talking and the failed one — because the chat drives them and `asleep` is the only visible sign
     that Genie did not answer. */
  /* Anchors measured on the asset (643x568). The star was EXTRACTED as its own layer (a 19,828px connected
     component at x[156..403] y[0..251], verified not to contain the lamp body before it was erased from the
     base image), so `star` is where its centre belongs when it is at rest. */
  var LAMP = { sparkle: { x: 280, y: 126 }, flame: { x: 528, y: 215 }, w: 643, h: 568,
               star: { cx: 280, cy: 126, w: 248, h: 252 } };

  /* A flame FLICKER, not a pulse. Three sines whose periods do not divide into each other, so the sum never
     settles into a visible loop the way a single sine does — which is the difference between a flame and a
     throb. Kept strictly positive and centred near 1. */
  function flicker(t) {
    return 0.72
      + 0.16 * Math.sin(t * 11.3)
      + 0.08 * Math.sin(t * 19.7 + 1.1)
      + 0.06 * Math.sin(t * 31.1 + 2.3);
  }

  function lampPaint(ctx, g, t, state) {
    var asleep = state === "asleep";
    var fx = g.ax(LAMP.flame.x), fy = g.ay(LAMP.flame.y);

    /* ── THE STAR, drawn as its own layer so it can actually move ──────────────────────────
       resting  : bobs up and down — "so it looks more lively"
       thinking : bobs a little quicker
       typing   : a TIGHT CIRCLE, for "when the answer coming back is being typed out"
       asleep   : still, like everything else in that state                                            */
    var star = this.extra && this.extra.star;
    var dx = 0, dy = 0, spin = 0;
    if (!asleep) {
      if (state === "typing") {
        // , "a bit more dramatic": radius 7 -> 12 art px, period 1.12s -> 1.01s (t*5.6 ->
        // t*6.2), tilt +-0.05 -> +-0.09 rad. Still a TIGHT circle — the loopiness measure that
        // distinguishes this state from the bob is a shape, so widening the radius does not blur it.
        var a = t * 6.2;                                  // ~1.01s per revolution
        dx = Math.cos(a) * 12; dy = Math.sin(a) * 12;     // in art px
        spin = Math.sin(a * 0.5) * 0.09;
      } else {
        // bob 6.5 -> 11 art px (x1.69) and rates 1.5/2.5/3.0 -> 1.8/2.9/3.4 rad/s, i.e.
        // periods 4.19/2.51/2.09s -> 3.49/2.17/1.85s. Drift 1.6 -> 3.0 art px, still at 0.37x the bob
        // rate so it stays a drift rather than a second oscillation.
        // ⛔ HEADROOM CHECKED, NOT ASSUMED: the star's top edge sits at art y=0 and `pad` 0.045 leaves
        //    ~28 art px of clear panel above it, so an 11px rise cannot clip. Asserted on painted
        //    pixels in the probe (top canvas row stays empty).
        var sp = state === "thinking" ? 2.9 : state === "talking" ? 3.4 : 1.8;
        dy = Math.sin(t * sp) * 11;                        // the bob
        dx = Math.sin(t * sp * 0.37) * 3.0;                // a touch of drift so it is not a piston
      }
    }
    if (star && star.width) {
      var swp = g.as(LAMP.star.w), shp = g.as(LAMP.star.h);
      var scx = g.ax(LAMP.star.cx + dx), scy = g.ay(LAMP.star.cy + dy);
      ctx.save();
      ctx.translate(scx, scy);
      if (spin) ctx.rotate(spin);
      ctx.drawImage(star, -swp / 2, -shp / 2, swp, shp);
      ctx.restore();
    }
    var sx = g.ax(LAMP.sparkle.x + dx), sy = g.ay(LAMP.sparkle.y + dy);

    if (asleep) {
      // It has to LOOK failed, not merely stop: the flame is out and the whole lamp is dimmed. The mood
      // pill beside it goes red in CSS, and the two together are unmistakable.
      ctx.save();
      ctx.globalCompositeOperation = "source-atop";
      ctx.fillStyle = "rgba(8,14,20,.62)";
      ctx.fillRect(0, 0, g.W, g.H);
      ctx.restore();
      return;
    }

    // one speed and one brightness per state, so the lamp reads differently at a glance
    // lift 1.25/1.45/1.6 -> 1.40/1.62/1.90 (the glow's radius multiplier) and motes
    // 3/7/8/10 -> 4/10/12/15. `sp` (the breath rate) is unchanged for thinking and typing on purpose:
    // those two are now told apart by KIND of motion, and speeding them up would trade a difference a
    // person can name for one they cannot.
    var cfg = state === "thinking" ? { sp: 3.1, lift: 1.40, motes: 10, hue: "255,160,150" }
            : state === "typing"   ? { sp: 4.0, lift: 1.62, motes: 12, hue: "255,195,175" }
            : state === "talking"  ? { sp: 5.0, lift: 1.90, motes: 15, hue: "255,210,190" }
            :                        { sp: 1.15, lift: 1.0, motes: 4, hue: "255,140,130" };

    var pulse = 0.5 + 0.5 * Math.sin(t * cfg.sp);
    ctx.save();
    ctx.globalCompositeOperation = "lighter";

    /* "can we have the flame flicker when thinking?" — while THINKING the flame's size and
       brightness come from flicker(), which is three non-commensurate sines rather than the smooth breath
       every other state uses. That is what makes it read as a flame rather than a pulse. */
    var fl = state === "thinking" ? flicker(t) : (0.82 + 0.34 * pulse);
    glow(ctx, fx, fy, g.as(90) * fl * cfg.lift, cfg.hue,
         (state === "thinking" ? 0.22 + 0.34 * fl : 0.30 + 0.26 * pulse));
    if (state === "thinking") {
      // a second, tighter core so the flicker is visible at the flame itself and not only in its halo
      glow(ctx, fx, fy, g.as(34) * (0.6 + 0.5 * flicker(t * 1.7 + 0.6)), "255,235,215", 0.30);
    }

    // the sparkle twinkling. Drawn INSIDE the baked star and brightened rather than over its outline —
    // a drawn star any larger would show a second edge against the one in the pixels.
    // the star's own twinkle 34 -> 42 art px, its halo 46 -> 56, alpha 0.30+0.42 -> 0.34+0.50.
    var tw = 0.5 + 0.5 * Math.sin(t * cfg.sp * 1.7 + 1.1);
    star4(ctx, sx, sy, g.as(42) * (0.55 + 0.30 * tw), g.as(6.5), "#fff", 0.34 + 0.50 * tw);
    glow(ctx, sx, sy, g.as(56) * (0.7 + 0.3 * tw), "220,150,165", 0.18 + 0.24 * tw);

    /* the sparkle field, TALKING ONLY. It is what makes this state nameable — see SPARKS.
       Drawn inside the same "lighter" block as the glows, so a sparkle over the lamp body adds light
       rather than punching a hole in the art. */
    /* ⭐ "The shine animation with the lamp — do it while thinking too."

       ⚠️ WHAT "THE SHINE" REFERS TO IS AN INFERENCE, AND IT IS THE LOAD-BEARING DECISION IN THIS ITEM.
       Nothing in this codebase is named shine/sheen/glint outside two comments in the unreferenced
       procedural fallback. But "do it while thinking TOO" can only mean something that is NOT currently in
       thinking — and after  there is exactly one such thing: the SPARKLE FIELD, which was talking-only.
       Every other lamp animation (the bob, the star's own twinkle, the flame, the motes) already runs in
       thinking, so asking for them there would be asking for what he already has. Stated in the report so
       he can correct it in one word.

       ⛔ AND IT IS AT TWO THIRDS STRENGTH ON PURPOSE, because  built `talking` around this mark being
       ITS signature. Sparkles in both states with the same intensity would undo that distinction — so
       thinking gets a visibly quieter field, and the three states stay orderable AND nameable:
         thinking : flame FLICKERS + a quiet sparkle field   (flame jerk ~16)
         typing   : the star CIRCLES, no sparkles            (loopiness ~0.78)
         talking  : the FULL sparkle field, flame steady     (flame jerk ~2)
       Measured: the talking margin must stay clearly above the thinking margin, and both above idle. */
    if (state === "talking" || state === "thinking") {
      var age = this.stateAt == null ? 1 : Math.min(1, ((clock() - this.stateAt) / 260));
      sparkles(ctx, g, t, sx, sy, cfg.hue, age * (state === "thinking" ? 0.66 : 1));
    }

    // motes lifting off the spout — the clearest signal that the lamp is working, and the thing that
    // makes "thinking" legible without reading the label
    for (var i = 0; i < cfg.motes; i++) {
      var ph = (t * (0.30 + 0.05 * i) * cfg.sp * 0.5 + i / cfg.motes) % 1;
      // rise 150 -> 185 art px, drift 16 -> 26, size 4.5 -> 5.6, alpha 0.75 -> 0.85.
      var rise = g.as(185) * ph * cfg.lift;
      var drift = Math.sin(t * 1.3 + i * 2.1) * g.as(26) * ph;
      var a = (1 - ph) * (1 - ph) * 0.85;
      var sz = g.as(5.6) * (1 - 0.45 * ph);
      ctx.globalAlpha = a;
      ctx.fillStyle = i % 3 === 0 ? "#F5E6E2" : "#E8A0A8";
      ctx.fillRect(fx + drift - sz / 2, fy - rise - sz / 2, sz, sz);
    }
    ctx.restore();
  }

  /* ── WEEK 1, THE CFO ──────────────────────────────────────────────────────────────────────────
     Anchors measured on the asset (1400x996): the baked speech bubble occupies x[529..1071] y[51..282];
     the cut pie's crust centres on (961,452) and the whole pie's on (1186,438).
     Nothing here redraws the bubble — it travels a highlight around the border that is already painted
     in the pixels, so there is exactly one bubble on the panel. */
  var CFO = {
    w: 1400, h: 996,
    bubble: { x: 529, y: 51, w: 542, h: 231 },
    pies: [{ x: 961, y: 410 }, { x: 1186, y: 394 }],
    receipts: [{ x: 800, y: 620 }, { x: 1120, y: 590 }, { x: 1210, y: 600 }],
    /* The hop. He is painted into the scene, so hopping HIM means moving a slab of the picture.
       This slab is his column only: it stops at x=520 and the baked bubble starts at x=529, so nothing of
       the bubble, the table or the pies ever moves. Full height, so the only exposed edges are the very top
       (wall) and the very bottom (floor), which an edge row fills invisibly at a 4px hop. */
    slab: { x: 160, w: 360 }
  };

  /* The three lines, verbatim. Held here so the app cannot drift from them. */
  var CFO_LINES = {
    correct: "Great, but I still need more info!",
    wrong: "That doesn't sound right..",
    done: "Great job!"
  };

  /* Draw a speech bubble OVER the one painted into the art, in the same place and a little larger.
     ⛔ THIS IS THE DELIBERATE CHOICE FOR "never two bubbles arguing on screen": the art already says
     "Just give me a report!" in pixels, so a second bubble placed elsewhere would leave him saying two
     things at once. Covering the painted one means there is exactly ONE bubble on the panel at all times —
     his default line when nothing has happened, and the reaction while one is live. */
  function bubble(ctx, g, text, fade) {
    var b = CFO.bubble, pad = 7;
    var x = g.ax(b.x - pad), y = g.ay(b.y - pad);
    var w = g.as(b.w + pad * 2), h = g.as(b.h + pad * 2);
    var r = g.as(26), bw = Math.max(2, g.as(7));
    ctx.save();
    ctx.globalAlpha = fade;
    // tail first, pointing back at him, matching the painted one's side
    ctx.beginPath();
    ctx.moveTo(x + g.as(6), y + h * 0.42);
    ctx.lineTo(x - g.as(46), y + h * 0.36);
    ctx.lineTo(x + g.as(6), y + h * 0.66);
    ctx.closePath();
    ctx.fillStyle = "#fff"; ctx.fill();
    ctx.fillStyle = "#0b0b0b";
    ctx.beginPath();
    ctx.moveTo(x + g.as(10), y + h * 0.45);
    ctx.lineTo(x - g.as(30), y + h * 0.39);
    ctx.lineTo(x + g.as(10), y + h * 0.61);
    ctx.closePath();
    ctx.fill();
    // the box
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
    else ctx.rect(x, y, w, h);
    ctx.fillStyle = "#fff"; ctx.fill();
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(x + bw, y + bw, w - bw * 2, h - bw * 2, Math.max(0, r - bw));
    else ctx.rect(x + bw, y + bw, w - bw * 2, h - bw * 2);
    ctx.fillStyle = "#0b0b0b"; ctx.fill();
    // the words, wrapped to the box, in the art's own monospace idiom
    var fs = g.as(44);
    ctx.font = "700 " + fs + "px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
    ctx.fillStyle = "#fff";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    var maxw = w - bw * 2 - g.as(28);
    var words = String(text).split(" "), lines = [], line = "";
    for (var i = 0; i < words.length; i++) {
      var probe = line ? line + " " + words[i] : words[i];
      if (ctx.measureText(probe).width > maxw && line) { lines.push(line); line = words[i]; }
      else line = probe;
    }
    if (line) lines.push(line);
    var lh = fs * 1.25, top = y + h / 2 - (lines.length - 1) * lh / 2;
    for (var j = 0; j < lines.length; j++) ctx.fillText(lines[j], x + w / 2, top + j * lh);
    ctx.restore();
  }

  function cfoPaint(ctx, g, t) {
    /* ── The resting hop. A slow, small vertical move of his column only. The gap it opens at the
       top and bottom of the frame is filled from the adjacent row, which at this amplitude is invisible. */
    /* this was the weakest motion in the product — 4 art px, which measured ~2px at canvas
       scale, i.e. not visible. Amplitude 4 -> 11 art px (x2.75), rate 1.6 -> 1.9 rad/s (period 3.93s ->
       3.31s), and the profile is now |sin|^0.62 rather than |sin|, which leaves the floor faster and
       hangs at the top — the difference between a hop and a float.
       ⛔ THE SLAB'S PATCH STRIP GREW WITH IT: the vacated strip is filled by stretching the art's
          bottom TWO ROWS, so at 11px it is a 5.5x stretch of floor rather than a 2x one. Verified by
          LOOKING, because a smeared floor band is not something any assertion here would catch. */
    var hop = -Math.pow(Math.abs(Math.sin(t * 1.9)), 0.62) * 11;   // 0 .. -11 art px, a hop not a float
    if (this.img && this.ready && Math.abs(hop) > 0.2) {
      var sl = CFO.slab;
      var sx = g.ax(sl.x), sw = g.as(sl.w), dy = g.as(hop);
      ctx.save();
      ctx.beginPath(); ctx.rect(sx, g.y, sw, g.h); ctx.clip();
      // repaint the slab shifted, then patch the strip it vacated with the row underneath it
      ctx.drawImage(this.img, sl.x, 0, sl.w, CFO.h, sx, g.y + dy, sw, g.h);
      ctx.drawImage(this.img, sl.x, CFO.h - 2, sl.w, 2, sx, g.y + g.h + dy, sw, Math.ceil(-dy) + 1);
      ctx.restore();
    }

    // steam off the two pies: three wisps each, rising and fading, offset so they do not pulse together
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    for (var p = 0; p < CFO.pies.length; p++) {
      var px0 = g.ax(CFO.pies[p].x), py0 = g.ay(CFO.pies[p].y);
      for (var i = 0; i < 3; i++) {
        // rate 0.34 -> 0.42, rise 120 -> 165 art px, sway 13 -> 19, peak alpha 0.20 -> 0.29.
        var ph = (t * 0.42 + i / 3 + p * 0.41) % 1;
        var rise = g.as(165) * ph;
        var sway = Math.sin(t * 1.5 + i * 2.0 + p) * g.as(19) * (0.35 + ph);
        var a = Math.sin(Math.PI * ph) * 0.29;
        glow(ctx, px0 + sway, py0 - rise, g.as(24) * (0.6 + 0.8 * ph), "235,228,215", a);
      }
    }
    ctx.restore();

    // ── a live reaction covers the painted bubble for a few seconds, then hands it back
    if (this.speech && t * 1000 < this.speech.until) {
      var left = this.speech.until - t * 1000;
      var inFade = Math.min(1, (this.speech.ms - left) / 220);     // ease in
      var outFade = Math.min(1, left / 320);                        // ease out
      bubble(ctx, g, this.speech.text, Math.max(0, Math.min(inFade, outFade)));
      return;                                                      // no border-chase while he is talking
    }

    // the bubble: a highlight running around the border that is already in the art, so he reads as
    // ASKING. Clipped to a ring just inside the painted border, and it fades out between passes so the
    // panel is not perpetually busy.
    var b = CFO.bubble;
    var bx = g.ax(b.x), by = g.ay(b.y), bw = g.as(b.w), bh = g.as(b.h);
    var cycle = 3.4;                 // the border-chase comes round more often, 4.2s -> 3.4s
    var ph2 = (t % cycle) / cycle;
    if (ph2 < 0.55) {
      var k = ph2 / 0.55;
      var per = 2 * (bw + bh);
      var d = k * per;
      // walk the rectangle's perimeter
      var hx, hy;
      if (d < bw) { hx = bx + d; hy = by; }
      else if (d < bw + bh) { hx = bx + bw; hy = by + (d - bw); }
      else if (d < 2 * bw + bh) { hx = bx + bw - (d - bw - bh); hy = by + bh; }
      else { hx = bx; hy = by + bh - (d - 2 * bw - bh); }
      var fade = Math.sin(Math.PI * k);
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      glow(ctx, hx, hy, g.as(58), "255,255,255", 0.34 * fade);   // 46 -> 58 art px, 0.26 -> 0.34
      ctx.restore();
    }

    // the receipts stirring — a hint of movement on the table, at the amplitude of a draught
    ctx.save();
    for (var r = 0; r < CFO.receipts.length; r++) {
      var rc = CFO.receipts[r];
      // the draught picks up — lift 1.6 -> 3.4 art px, alpha 0.07+0.05 -> 0.09+0.08.
      var lift = Math.sin(t * 1.15 + r * 1.9);
      ctx.globalAlpha = 0.09 + 0.08 * (0.5 + 0.5 * lift);
      ctx.fillStyle = "#EFEAE2";
      ctx.fillRect(g.ax(rc.x), g.ay(rc.y) + lift * g.as(3.4), g.as(34), g.as(3));
    }
    ctx.restore();
  }

  /* ══════════════════════════════════════════════════════════════════════════════════════════════
     WEEKS 2, 3 AND 4 FROM THE PROJECT'S ART, ONE BESPOKE MOTION EACH

     Every anchor below was measured on the SHIPPED asset, so `artW`/`artH` are the file's own
     coordinates and no number here needs converting. those files were made offline and
     records what was cropped out of each.

     ⛔ EACH MOTION HAS AN AT-REST CONTROL THAT READS ZERO, and it is not a nicety: at rest every one
     of these returns BEFORE drawing anything, so the panel is the untouched bitmap. That is what makes
     the moving numbers mean something — and it is also what `prefers-reduced-motion` gets, because
     ImageStage.start() never schedules a frame under it.
     ══════════════════════════════════════════════════════════════════════════════════════════════ */

  /* ── WEEK 2, THE SHELF REVIEW — notes and coins falling and bouncing off the table ───────────── */
  var SHELF = {
    w: 1393, h: 760,
    /* `landY` is per item because the table is in perspective: money dropped at the front lands lower
       than money dropped at the back, and one shared floor line read as a glass shelf. The periods are
       NON-COMMENSURATE on purpose — in lockstep seven coins read as one object, which is the same
       failure the lamp's SPARKS arrangement is arranged to avoid. */
    drops: [
      { x: 168, r: 17, kind: "coin", landY: 596, per: 2.30, ph: 0.00 },
      { x: 402, r: 15, kind: "coin", landY: 566, per: 2.90, ph: 0.37 },
      { x: 596, r: 30, kind: "note", landY: 604, per: 3.70, ph: 0.61 },
      { x: 815, r: 16, kind: "coin", landY: 546, per: 2.60, ph: 0.18 },
      { x: 1004, r: 18, kind: "coin", landY: 620, per: 3.10, ph: 0.79 },
      { x: 1198, r: 28, kind: "note", landY: 574, per: 4.30, ph: 0.44 },
      { x: 1310, r: 15, kind: "coin", landY: 600, per: 2.45, ph: 0.92 }
    ],
    /* Sampled from the art by histogram rather than by eye: the gold on this table means (246,201,48)
       over 3,982 px and the notes mean (98,156,76) over 2,328, so the falling money is the same money
       that is already lying there. */
    gold: "#F6C930", goldRim: "#A8731C", goldTop: "#FFE98A",
    note: "#62A04C", noteDark: "#40692A", noteInk: "#D8ECC4"
  };

  /* Fall, strike, bounce twice with a decaying rebound, rest, then leave. `u` is the item's own phase,
     so each returns to exactly its start and the loop has no seam. */
  function dropY(u, top, land) {
    if (u < 0.52) { var k = u / 0.52; return top + (land - top) * k * k; }   // gravity, not a glide
    if (u < 0.78) { var k2 = (u - 0.52) / 0.26; return land - Math.sin(Math.PI * k2) * (land - top) * 0.19; }
    if (u < 0.92) { var k3 = (u - 0.78) / 0.14; return land - Math.sin(Math.PI * k3) * (land - top) * 0.06; }
    return land;
  }

  function ellipsePath(ctx, cx, cy, rx, ry) {
    if (ctx.ellipse) { ctx.beginPath(); ctx.ellipse(cx, cy, rx, ry, 0, 0, 2 * Math.PI); return; }
    ctx.save(); ctx.translate(cx, cy); ctx.scale(1, Math.max(0.001, ry / rx));
    ctx.beginPath(); ctx.arc(0, 0, rx, 0, 2 * Math.PI); ctx.restore();
  }

  function coinMark(ctx, cx, cy, r, spin, S) {
    var ry = Math.max(r * 0.20, Math.abs(Math.cos(spin)) * r);   // spinning, so it thins and fills again
    ellipsePath(ctx, cx, cy, r, ry);
    ctx.fillStyle = S.gold; ctx.fill();
    ctx.lineWidth = Math.max(1, r * 0.17); ctx.strokeStyle = S.goldRim; ctx.stroke();
    ellipsePath(ctx, cx - r * 0.26, cy - ry * 0.34, r * 0.32, ry * 0.32);
    ctx.fillStyle = S.goldTop; ctx.fill();
  }

  function noteMark(ctx, cx, cy, w, spin, S) {
    var h = w * 0.46;
    ctx.save();
    ctx.translate(cx, cy); ctx.rotate(Math.sin(spin) * 0.5);      // tumbling as it falls
    ctx.fillStyle = S.note; ctx.fillRect(-w / 2, -h / 2, w, h);
    ctx.lineWidth = Math.max(1, w * 0.05); ctx.strokeStyle = S.noteDark;
    ctx.strokeRect(-w / 2, -h / 2, w, h);
    ctx.fillStyle = S.noteInk; ctx.fillRect(-w * 0.20, -h * 0.16, w * 0.40, h * 0.32);
    ctx.restore();
  }

  function shelfPaint(ctx, g, t) {
    var S = SHELF;
    ctx.save();
    for (var i = 0; i < S.drops.length; i++) {
      var d = S.drops[i];
      var u = (((t / d.per) + d.ph) % 1 + 1) % 1;
      var y = dropY(u, -70, d.landY);
      ctx.globalAlpha = u > 0.92 ? Math.max(0, (1 - u) / 0.08) : 1;   // leave, rather than blink out
      if (d.kind === "coin") coinMark(ctx, g.ax(d.x), g.ay(y), g.as(d.r), t * 7.3 + i, S);
      else noteMark(ctx, g.ax(d.x), g.ay(y), g.as(d.r * 2), t * 3.1 + i * 1.7, S);
    }
    ctx.restore();
  }

  /* ── WEEK 3, THE EXPANSION MAP — some of the cookies pulsing in size ─────────────────────────── */
  var MAP3 = {
    w: 1360, h: 760,
    /* ⭐ WHY THIS WORKS WITHOUT SMEARING THE MAP: drawing the art scaled by `s` about a cookie's centre
       and clipping to radius r*s fills the whole disc from source radius <= r, so nothing outside r is
       ever dragged outward. `r` is each cookie's MAXIMUM measured extent, not its median — at the
       median the clip cuts the cookie's own edge in the directions where it reaches further, which
       reads as a circular notch. The ocean that falls inside r magnifies to ocean, which is why all
       four sit on open water: on land, cookie tan and continent tan are the same colours and the
       transition cannot be measured at all (a radius probe there collapsed to 3px).
       ⛔ Radii come only from probes that TERMINATED. Two candidates reported the search cap of 52 in
       some directions — never having found ocean — and a cap is a guess, not a measurement, so they
       are not in this list. */
    cookies: [
      { x: 585, y: 122, r: 26, per: 2.90, ph: 0.00 },     // iced cookie, North Atlantic
      { x: 868, y: 435, r: 26, per: 3.70, ph: 0.31 },     // macaron, Indian Ocean
      { x: 1337, y: 487, r: 19, per: 4.30, ph: 0.58 },    // cookie, west Pacific. r is its measured
      // max extent with NO margin added, unlike the others: at r=21 the pulsed clip (r x 1.11 =
      // 23.3) reached 1360.3 on a 1360px asset and was cut by the frame. Caught by the test, not
      // by looking -- 0.3px of a clipped arc is not something the eye finds.
      { x: 843, y: 505, r: 38, per: 5.10, ph: 0.77 }      // macaron pair, south Indian Ocean
    ],
    /* 0.15, not the 0.11 this shipped with first. MEASURED ON THE SERVED PAGE: the stage draws this
       art at scale 0.689, so 0.11 grew each cookie's radius by only 1.44-2.88 canvas px.  raised the
       CFO's hop from 4 art px for exactly this reason, having measured that "4 art px measured ~2px at
       canvas scale, i.e. not visible". 0.15 puts the growth at 2.0-3.9 canvas px while every disc still
       fits inside the art -- the tightest is the west-Pacific cookie at 1337 + 19x1.15 = 1358.9 of 1360,
       which is asserted against the file rather than against this comment. */
    amp: 0.15
  };

  function mapPaint(ctx, g, t) {
    if (!this.img || !this.ready) return;
    for (var i = 0; i < MAP3.cookies.length; i++) {
      var c = MAP3.cookies[i];
      var s = 1 + MAP3.amp * (0.5 - 0.5 * Math.cos(2 * Math.PI * (((t / c.per) + c.ph) % 1)));
      if (s < 1.002) continue;           // AT REST: the art itself, untouched. The control that reads 0.
      var cx = g.ax(c.x), cy = g.ay(c.y);
      ctx.save();
      ctx.beginPath(); ctx.arc(cx, cy, g.as(c.r) * s, 0, 2 * Math.PI); ctx.clip();
      ctx.translate(cx, cy); ctx.scale(s, s); ctx.translate(-cx, -cy);
      ctx.drawImage(this.img, g.x, g.y, g.w, g.h);
      ctx.restore();
    }
  }

  /* ── WEEK 4, THE CONCENTRATION CHECK — the auditor moving closer to and further from the jar ─── */
  var AUDIT = {
    w: 998, h: 760,
    /* ⛔⛔ THE PAINTED BUBBLE, AND WHY `bubble()` IS NOT REUSED HERE. Measured by connectivity — the
       balloon and its tail are ONE 8-connected white component of 27,097 px — at white x[362..788]
       y[134..244], tail tapering down-RIGHT to its tip at (675,244), black outline 17-22px thick.
       This is the week-1 situation and the EXACT INVERSE of week 1's idiom:
           week 1 CFO : interior (37,37,37) near-black, white border, white text, tail LEFT
           week 4     : interior (252,252,251) white,   black border, black text, tail DOWN-RIGHT
       `bubble()` draws #fff border -> #0b0b0b interior -> #fff text with a left tail, sized from the
       CFO's 38.7%-wide rect; the auditor's is 46.6% wide and 29.0% high. So reusing it would paint a
       black bubble over a white one, and THE FAILURE MODES INVERT TOO: on week 1 a misfit sliver is
       white-on-white and invisible, here it is a bright white halo.
       ⭐ So week 4 is given NO reaction line at all. `cfoSays()` feature-detects `scene.say`, this
       stage does not define one, and there is therefore exactly ONE bubble on the panel — the painted
       one. Putting words in his mouth was never asked for; the geometry above is recorded so that
       whoever is asked for it later has the numbers and does not reach for the CFO's function. */
    bubble: { x: 362, y: 134, w: 427, h: 111, tail: { x: 675, y: 244 } },
    /* HIS COLUMN, and why it is a feathered full-height slab rather than a rectangle like CFO.slab.
       A HORIZONTAL move is invisible across a VERTICAL boundary wherever the content is horizontal
       bands — wall, desk edge, desk surface, all of which shift horizontally into themselves — and
       breaks only on objects crossing it. So the boundary is the column with the lowest MEASURED
       horizontal gradient over the band that is not restored afterwards, y[250..488]: x=676 scores
       p95 63 / mean 15.8 / max 86, against 110-255 for every other candidate.
       ⛔ NO HORIZONTAL SEAM EXISTS ON THIS COMPOSITION, which is why the CFO's rectangle cannot be
       copied: the hat crown reaches y=206 at x[802..886] but only y=232 at x[746..800], while the
       balloon's outline bottom is y~213 — so the wall band common to the whole column is EMPTY, and a
       probe for it correctly returned nothing rather than a number. Full height instead means the top
       and bottom edges are the art's own edges and cost no seam at all. */
    slab: { x: 676, feather: 22, steps: 6 },
    /* Putting the bubble back, unshifted, so only HE moves. Two rects because the safe depth differs:
       at x[676..745] the balloon reaches y~236 and the brim does not begin until y~272, while at
       x[746..801] the balloon stops at y~214 and the hat crown starts at 232. Neither rect contains
       one pixel of hat — which is the whole reason this is split rather than one bounding box, since
       the bounding box of bubble+tail DOES reach into the crown. */
    keep: [{ x: 676, y: 112, w: 70, h: 144 }, { x: 746, y: 112, w: 56, h: 118 }],
    /* 12 art px, not the 6 this shipped with first, and the reason is the project's OWN precedent
       rather than taste. MEASURED ON THE SERVED PAGE by cross-correlating his row band against frame 0:
       6 art px gave 4 canvas px peak-to-peak (predicted 4.13 at scale 0.689) = 2 CSS px at dpr 2 -- and
        raised the CFO's hop away from precisely that figure, recording that "4 art px measured ~2px at
       canvas scale, i.e. not visible". 12 art px gives ~8.3 canvas px, against the CFO's 11 art px at
       ITS scale 0.526 = 5.8, so this reads as a deliberate lean rather than a jitter. The feather is 22
       art px and so still wider than the travel, which is what keeps the ramp working. */
    amp: 12, rate: 1.55
  };

  function auditPaint(ctx, g, t) {
    if (!this.img || !this.ready) return;
    var A = AUDIT, sl = A.slab;
    // 0 .. -amp: toward the jar, which is to his LEFT, and back out again. Where this is ~0 the panel
    // is the untouched bitmap — the at-rest control.
    var dx = -A.amp * (0.5 - 0.5 * Math.cos(t * A.rate));
    if (Math.abs(dx) < 0.2) return;
    var px = g.as(dx), wArt = A.w - sl.x, dstX = g.ax(sl.x), dstW = g.as(wArt);
    var fEnd = g.ax(sl.x + sl.feather);
    ctx.save();
    ctx.beginPath(); ctx.rect(fEnd, g.y, g.x + g.w - fEnd, g.h); ctx.clip();
    ctx.drawImage(this.img, sl.x, 0, wArt, A.h, dstX + px, g.y, dstW, g.h);
    // the strip he vacated at the art's right edge, filled by stretching its last COLUMN — a column,
    // not a row, because the move is horizontal. (The CFO's vertical hop patches from a row.)
    ctx.drawImage(this.img, A.w - 2, 0, 2, A.h, g.x + g.w + px, g.y, Math.ceil(-px) + 1, g.h);
    ctx.restore();
    // The boundary column still crosses the magnifier's left rim, so the shift is RAMPED in over
    // `feather` art px rather than landing as a step. Six bands, because 22 one-pixel draws of the
    // whole slab per frame buys nothing the eye can see.
    var bandArt = sl.feather / sl.steps;
    for (var i = 0; i < sl.steps; i++) {
      var x0 = g.ax(sl.x + i * bandArt), x1 = g.ax(sl.x + (i + 1) * bandArt);
      ctx.save();
      ctx.globalAlpha = (i + 1) / (sl.steps + 1);
      ctx.beginPath(); ctx.rect(x0, g.y, Math.max(1, x1 - x0), g.h); ctx.clip();
      ctx.drawImage(this.img, sl.x, 0, wArt, A.h, dstX + px, g.y, dstW, g.h);
      ctx.restore();
    }
    for (var k = 0; k < A.keep.length; k++) {
      var r = A.keep[k];
      ctx.drawImage(this.img, r.x, r.y, r.w, r.h, g.ax(r.x), g.ay(r.y), g.as(r.w), g.as(r.h));
    }
  }

  /* The three image-backed week stages, keyed by the scene name the content pack already uses. */
  var WEEK_ART = {
    shelf: { src: "/art/week2-shelf.webp", d: SHELF, paint: shelfPaint },
    map: { src: "/art/week3-map.webp", d: MAP3, paint: mapPaint },
    magnifier: { src: "/art/week4-auditor.webp", d: AUDIT, paint: auditPaint }
  };

  global.Sprites = {
    reducedMotion: reducedMotion,

    /* the lamp is the project's art now. `lampSets`/`lampScene` below it are kept, not deleted —
       they are the fallback if the asset ever cannot be served, and they still document the four
       states. Nothing else in the app changed: it still calls show("thinking") and so on. */
    lampStage: function (canvas) {
      var st = new ImageStage(canvas, {
        src: "/art/genie-lamp-nostar.png", artW: LAMP.w, artH: LAMP.h,
        layers: { star: "/art/genie-star.png" },
        mode: "contain", pad: 0.045, state: "idle", paint: lampPaint
      });
      st.fit();
      return st;
    },

    /* The procedural lamp, if it is ever wanted back. */
    lampStageDrawn: function (canvas) {
      var st = new Stage(canvas, { fps: 8 });
      lampSets(st);
      st.show("idle");
      st.fit();
      return st;
    },

    /* all four weeks are the project's art now — week 1 the CFO, and weeks 2-4 the shelf, the map and
       the auditor, each with its own motion. The drawn `SCENES` below are KEPT rather than deleted, on
       the same reasoning as `lampStageDrawn`: they are what renders if an asset cannot be served, and
       they still document what each week is about. */
    sceneStage: function (canvas, name) {
      if (name === "cfo") {
        var st = new ImageStage(canvas, {
          src: "/art/week1-cfo.jpg", artW: CFO.w, artH: CFO.h,
          mode: "extend", paint: cfoPaint
        });
        st.fit();
        return st;
      }
      var art = WEEK_ART[name];
      if (art) {
        var sw = new ImageStage(canvas, {
          src: art.src, artW: art.d.w, artH: art.d.h,
          mode: "extend", paint: art.paint
        });
        sw.fit();
        return sw;
      }
      return sceneStage(canvas, name);
    },
    sceneNames: Object.keys(SCENES),
    cfoLines: CFO_LINES,
    /* The measured anchor tables, exposed so a test or probe asserts against the numbers this file
       actually uses rather than against a copy of them. */
    weekArt: { shelf: SHELF, map: MAP3, audit: AUDIT }
  };
})(window);
