"""The four-week game: the pausing clock, the scoring formula, and the letter the player fills in.

Three things live here because they are the three things the rebuild added, and they are coupled: what a
blank is worth depends on the clock, and the clock only runs while the player is looking at that week.

⏱ THE CLOCK, AND WHY IT IS AN ACCUMULATOR RATHER THAN A START TIME
THE REQUIREMENT: a player's clock for a week starts when that week is available and they click to
unlock it, and it pauses whenever they are not looking at that week. A pausing clock cannot be
`now - unlocked_at`; it has to be a running total of the time actually spent, which is a different data
shape and the reason `player_weeks()` persists a figure instead of recomputing one.

The honest version has to survive a closed laptop. So the browser sends a tick only while the tab is
VISIBLE and that week is the one on screen, and the server credits the real gap between ticks only when
it is small — `TICK_GRACE_S` — and credits NOTHING for a larger gap, restarting from that moment. A
player who closes the lid for three hours therefore banks nothing but loses at most one tick, and no
amount of client silence can invent time. The failure direction is deliberate: an unreliable tick loses
the player a few seconds of decay, which is the harmless side.
"""
import threading, time

# ── the scoring formula. One line, so the numbers can be changed without reading any of the above:
#    a blank is worth 100, minus 5 for every whole minute that week has been open in front of you,
#    never below 40. Wrong answers cost nothing, ever — asking is the whole point of this game.
POINTS_BASE = 100
DECAY_PER_MIN = 5
POINTS_FLOOR = 40

# ── THE HINT PENALTY: the hint button opens a pop-up revealing the hint for that blank, and
#    permanently adds -5 to that blank's score.
#
#    ONE number, here, because it is charged in three places that have to agree: the `points` on the
#    `hint_used` log row, the SQL that nets a score out of that row, and the BUTTON'S OWN LABEL. The client
#    reads it from the payload as `hint_cost` instead of printing a literal 5 — a label that can disagree
#    with the charge is the defect that avoids.
HINT_PENALTY = 5

TICK_EVERY_S = 15          # what the browser is asked to send
TICK_GRACE_S = 40          # a gap longer than this credits zero — see the module docstring
PERSIST_EVERY_MS = 30_000  # how much new focus time is worth a row in the log table


def points_for_focus(focus_s, base=POINTS_BASE):
    """What a blank is worth after `focus_s` seconds of attention on its week."""
    whole_minutes = int(max(focus_s, 0) // 60)
    return max(int(base) - DECAY_PER_MIN * whole_minutes, POINTS_FLOOR)


def minutes_until_next_drop(focus_s):
    """Seconds until the next 5-point step, so the UI can show a live countdown."""
    return int(60 - (max(focus_s, 0) % 60))


# ⛔ BRUTE FORCE, THROTTLED — AND NOTHING HERE COSTS POINTS.
#    An independent battle test filled a blank in SEVEN attempts, 11.4 seconds and no Genie question at
#    all. Two things made that possible: the payload published the answer's LENGTH (fixed in
#    _box_width), and wrong answers were free, unmetered AND unlimited. Removing the length hint raises
#    the cost of a walk but does not bound it — a one-digit answer is still ten guesses.
#
#    The rule this project will not break: NEVER PUNISH ASKING. So there is no points penalty, no strike
#    and no lockout. After BURST wrong answers on ONE blank the next attempt on THAT blank waits
#    COOLDOWN_S, and the message says so. Asking Genie is untouched, every other blank is untouched, and a
#    player who is genuinely stuck loses nothing but a few seconds. A ten-guess walk goes from eleven
#    seconds to over a minute per blank, which is the difference between a shortcut and a chore.
#    These two numbers are yours to change, or to set BURST high enough to disable.
WRONG_BURST = 5
WRONG_COOLDOWN_S = 20


class Attempts:
    """Wrong-answer attempts per (player, blank), in memory only.

    Deliberately NOT persisted: the log table already records every attempt for the operator page, and a
    throttle that survives a restart would punish a player for a redeploy. Losing it on restart makes the
    throttle weaker, which is the harmless direction — it is a speed bump, not a security control.
    """

    def __init__(self):
        self._n = {}
        self._until = {}
        self._lock = threading.Lock()

    def blocked_for(self, user_key, clue_id):
        """-> seconds the player must wait on this blank, or 0."""
        with self._lock:
            until = self._until.get((user_key, clue_id), 0.0)
        left = until - time.time()
        return int(left) + 1 if left > 0 else 0

    def note_wrong(self, user_key, clue_id):
        key = (user_key, clue_id)
        with self._lock:
            n = self._n.get(key, 0) + 1
            self._n[key] = n
            if n % WRONG_BURST == 0:
                self._until[key] = time.time() + WRONG_COOLDOWN_S
            return n

    def note_solved(self, user_key, clue_id):
        key = (user_key, clue_id)
        with self._lock:
            self._n.pop(key, None)
            self._until.pop(key, None)


class Clock:
    """Per-player, per-week accumulated focus time. Memory is the working copy; the log table is truth.

    Seeded from the table on first read of a week (so a container restart cannot wipe a clock) and only
    ever moved FORWARD — `max(memory, table)` — because the two can disagree after a restart and the
    direction that loses time is the one that harms a player.
    """

    def __init__(self, store, queries, table, season_id=""):
        self.store, self.q, self.table, self.season_id = store, queries, table, season_id
        self._acc = {}                      # (user, week) -> {ms, last_tick, persisted_ms}
        self._loaded = set()                # users whose rows have been read back
        self._lock = threading.Lock()

    # -- reading ---------------------------------------------------------------
    def _rows(self, user_key):
        if not self.store:
            return {}
        rows = self.store.query(
            f"weeks:{self.season_id}:{user_key}", self.q.player_weeks(self.table),
            [{"name": "user_key", "type": "STRING", "value": str(user_key)},
             {"name": "season_id", "type": "STRING", "value": str(self.season_id)}], ttl=0)
        out = {}
        for r in rows:
            try:
                wk = int(r.get("week_index"))
            except (TypeError, ValueError):
                continue
            out[wk] = {"unlocked_at": r.get("unlocked_at"),
                       "focus_ms": int(float(r.get("focus_ms") or 0))}
        return out

    def load(self, user_key, force=False):
        """-> {week: {unlocked_at, focus_ms}} merged with anything accumulated in memory since."""
        table = self._rows(user_key) if (force or user_key not in self._loaded) else {}
        with self._lock:
            if table:
                self._loaded.add(user_key)
                for wk, row in table.items():
                    key = (user_key, wk)
                    mem = self._acc.get(key)
                    if mem is None:
                        self._acc[key] = {"ms": row["focus_ms"], "last_tick": None,
                                          "persisted_ms": row["focus_ms"],
                                          "unlocked_at": row["unlocked_at"]}
                    else:
                        # forward only: a restart leaves memory at 0 and the table holding the truth
                        mem["ms"] = max(mem["ms"], row["focus_ms"])
                        mem["unlocked_at"] = mem.get("unlocked_at") or row["unlocked_at"]
            return {wk: {"unlocked_at": v.get("unlocked_at"), "focus_ms": int(v["ms"])}
                    for (u, wk), v in self._acc.items() if u == user_key}

    def focus_s(self, user_key, week):
        with self._lock:
            e = self._acc.get((user_key, int(week)))
            return (e["ms"] / 1000.0) if e else 0.0

    def is_unlocked(self, user_key, week):
        with self._lock:
            e = self._acc.get((user_key, int(week)))
            return bool(e and e.get("unlocked_at"))

    # -- writing ---------------------------------------------------------------
    def unlock(self, viewer, week, case_id=None):
        """Record the player's own unlock click. Idempotent: a second click adds no second clock."""
        week = int(week)
        key = (viewer["user_key"], week)
        with self._lock:
            e = self._acc.get(key)
            if e and e.get("unlocked_at"):
                return False
            self._acc[key] = {"ms": (e or {}).get("ms", 0), "last_tick": time.time(),
                              "persisted_ms": (e or {}).get("persisted_ms", 0),
                              "unlocked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if self.store:
            self.store.log("week_unlocked", season_id=self.season_id, user_key=viewer["user_key"],
                           user_email=viewer["email"], user_display_name=viewer["name"],
                           week_index=week, real_week_index=week, case_id=case_id, latency_ms=0)
            # No inline flush: that would hold the player's unlock CLICK open for a synchronous Delta
            # commit, and the in-memory clock is authoritative from this instant. The background writer
            # lands the row within its flush interval. See the note in app.py's /api/chat.
            self.store.invalidate("weeks")
        return True

    def tick(self, viewer, week, stopped=False):
        """Credit the time since the previous tick, if it is small enough to have been real.

        `stopped` is passed for a week whose five blanks are all filled: the clock is read one last time
        and then never advanced again, so a finished week cannot keep decaying.
        """
        week = int(week)
        key = (viewer["user_key"], week)
        now = time.time()
        persist = None
        with self._lock:
            e = self._acc.get(key)
            if not e or not e.get("unlocked_at"):
                return None                              # never unlocked: nothing to run
            last = e.get("last_tick")
            if not stopped and last is not None:
                gap = now - last
                if 0 < gap <= TICK_GRACE_S:
                    e["ms"] += int(gap * 1000)
                # a larger gap credits NOTHING — see the module docstring
            e["last_tick"] = None if stopped else now
            if e["ms"] - e["persisted_ms"] >= PERSIST_EVERY_MS or (stopped and e["ms"] > e["persisted_ms"]):
                e["persisted_ms"] = e["ms"]
                persist = e["ms"]
            out = int(e["ms"])
        if persist is not None and self.store:
            self.store.log("week_time", season_id=self.season_id, user_key=viewer["user_key"],
                           user_email=viewer["email"], user_display_name=viewer["name"],
                           week_index=week, real_week_index=week, latency_ms=int(persist))
        return out / 1000.0

    def flush_week(self, viewer, week):
        """Persist whatever is accumulated, without crediting a new interval. Called before scoring so
        the figure a score was computed from is on the record."""
        week = int(week)
        with self._lock:
            e = self._acc.get((viewer["user_key"], week))
            if not e or e["ms"] <= e["persisted_ms"]:
                return
            e["persisted_ms"] = e["ms"]
            ms = e["ms"]
        if self.store:
            self.store.log("week_time", season_id=self.season_id, user_key=viewer["user_key"],
                           user_email=viewer["email"], user_display_name=viewer["name"],
                           week_index=week, real_week_index=week, latency_ms=int(ms))


# ─────────────────────────────────────────────────────────────── what the client is sent

def fmt_focus(seconds):
    s = int(max(seconds, 0))
    return f"{s // 60}:{s % 60:02d}"


# ⛔ WIDTH FROM THE KIND, NEVER FROM THE VALUE. The first version sent `len(display) + 2` for UNSOLVED
#    blanks, which published the answer's LENGTH to the browser: a width of 3 said "one character", and a
#    width of 7 on a number said "there is a thousands separator in it". An independent battle test walked
#    that hint and filled a blank in SEVEN attempts, 11.4 seconds, 100 points, WITHOUT ASKING GENIE — which
#    refuted the assumption that the values are not guessable. A solved blank may size to its own value,
#    because the player has already earned it and the memo should read properly.
_KIND_WIDTH = {"number": 11, "string": 18, "date": 13, "set": 18}


def hint_fields(st):
    """The four fields the "Hint (-5)" button renders from.

    ⭐ THE SAME NAMES AND THE SAME DERIVATION ON THE CHEVRON AND ON THE BLANK, which is why this is a
    function and not two literals. A value that appears on a second surface under a second name, or from a
    second derivation, is this project's most expensive recurring defect — it cost a live "Player" where a
    name belonged, and a tag that disagreed with itself on one page. Both read this, from one state row.

    * `hint_cost`    what a FIRST reveal costs. The label comes from here, never from a literal 5.
    * `hint_taken`   this player has already revealed it. A RECORDED fact, so it survives a reload — which
                     is the whole point: a -5 held in the page is wiped by a refresh and the button
                     comes back.
    * `hint_penalty` what has ALREADY been deducted for this blank. This is the number the score
                     arithmetic actually used, not a recomputation of it.
    * `hint_enabled` whether the button accepts a click — FALSE if and only if the blank is solved. His
                     words: "If the answer is correctly answered, the button gets disabled (whether the
                     hint was revealed previously or not)."
      ⛔ So it stays TRUE after a reveal: a second click re-opens the popup and is FREE. That is only safe
         because the penalty is idempotent in the DERIVATION (queries.HINT_PENALTY_SQL takes a MAX, never a
         SUM) rather than in the button — two tabs cannot make it -10.
    """
    st = st or {}
    return {"hint_cost": HINT_PENALTY,
            "hint_taken": int(st.get("hints") or 0) > 0,
            "hint_penalty": int(st.get("hint_penalty") or 0),
            "hint_enabled": not bool(st.get("solved"))}


def _box_width(clue, solved):
    if solved:
        return len(str(clue.get("display") or "")) + 2
    return _KIND_WIDTH.get(clue.get("answer_type"), 14)


def letter_for_client(case, state):
    """The memo, as paragraphs of text and blanks, with each blank carrying only what a player may see.

    A blank that is SOLVED carries its display value — the canonical one from the pack, never what the
    player typed, so the finished memo always reads properly even if they pasted a whole sentence into
    it. An unsolved blank carries no value at all: expected answers never reach the browser, or
    view-source is the winning strategy.
    """
    by_id = {cl["clue_id"]: cl for cl in case["clues"]}
    order = {cl["clue_id"]: i + 1 for i, cl in enumerate(case["clues"])}
    paras = []
    for para in case["letter"]["paras"]:
        parts = []
        for p in para:
            if isinstance(p, str):
                parts.append({"t": p})
                continue
            cl = by_id[p["blank"]]
            st = state.get(cl["clue_id"]) or {}
            solved = bool(st.get("solved"))
            parts.append({"blank": cl["clue_id"], "n": order[cl["clue_id"]],
                          "ask": cl.get("ask"), "kind": cl.get("answer_type"),
                          "solved": solved, "points": int(st.get("points") or 0),
                          "value": cl.get("display") if solved else None,
                          "width": _box_width(cl, solved), **hint_fields(st)})
        paras.append(parts)
    return {"to": case["letter"]["to"], "subject": case["letter"]["subject"], "paras": paras}


def week_payload(season, week, state, clock, user_key, released, focus_s=None):
    """One week, as the tracker and the widget bar need it."""
    case = season.cases.get(int(week))
    if not case:
        return None
    ids = [cl["clue_id"] for cl in case["clues"]]
    solved = [i for i in ids if (state.get(i) or {}).get("solved")]
    unlocked = clock.is_unlocked(user_key, week)
    fs = clock.focus_s(user_key, week) if focus_s is None else focus_s
    done = len(solved) == len(ids)
    return {
        "week": int(week), "case_id": case["case_id"], "title": case["title"],
        "challenge": case.get("challenge"), "scene": case.get("scene"),
        "released": bool(released), "unlocked": unlocked, "done": done,
        # ⛔ RELEASE OUTRANKS UNLOCK. An unlock that predates a de-release used to leave `status: "open"`
        #    reachable, so the memo opened and answers scored while the card said "not released yet".
        #    A week the operator has closed is CLOSED, whatever this player did earlier; their unlock and
        #    their accumulated clock are untouched and come back the moment it is released again.
        "status": ("locked" if not released
                   else "done" if (unlocked and done)
                   else "open" if unlocked
                   else "available"),
        "solved": len(solved), "of": len(ids),
        "points": sum(int((state.get(i) or {}).get("points") or 0) for i in ids),
        "focus_s": int(fs), "focus_label": fmt_focus(fs),
        "worth_now": points_for_focus(fs),
        "next_drop_s": minutes_until_next_drop(fs),
        # ⛔ `points` here is ALREADY NET of any hint penalty — state.py does the one subtraction, so the
        #    chevron's "+95" needs no arithmetic in the client and cannot disagree with the header total.
        "widgets": [{"n": n + 1, "clue_id": i, "label": (case["clues"][n].get("label")),
                     "ask": case["clues"][n].get("ask"),
                     "state": "correct" if (state.get(i) or {}).get("solved")
                              else "wrong" if (state.get(i) or {}).get("tried") else "open",
                     "points": int((state.get(i) or {}).get("points") or 0),
                     **hint_fields(state.get(i))}
                    for n, i in enumerate(ids)],
    }
