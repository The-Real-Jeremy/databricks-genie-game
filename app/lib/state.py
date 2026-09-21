"""Per-player progress, held in memory and rebuilt from the log table on demand.

Why not read the table on every request: each read is a warehouse round trip of a second or more, and
this is on the path of every page load. Why not memory alone: a container restart would wipe every
score. So the table stays the source of truth and this is a cache that is refilled from it — invalidated
the moment the player's own actions change it, so a player never has to wait to see their own points.
"""
import threading, time

from .game import HINT_PENALTY


def _blank():
    return {"solved": False, "earned": 0, "hint_penalty": 0, "hints": 0, "points": 0}


def _net(entry):
    """THE ONE PLACE A HINT PENALTY IS SUBTRACTED on the player's own side.

    ⭐ Why here and not in the SQL: the table is read on a TTL, while the player's own click updates this
    cache immediately. Two separate subtractions would show 100 at the moment of answering and 95 once the
    TTL expired — a number that corrects itself, which is worse than one that is simply wrong, because
    nothing ever looks broken and the player just stops believing the score. `queries.player_state`
    therefore returns `earned` and `hint_penalty` as components and this function is the only arithmetic.
    (The board nets across blanks in SQL because it must; a test asserts the two agree.)
    """
    entry["points"] = int(entry.get("earned") or 0) - int(entry.get("hint_penalty") or 0)
    return entry


class PlayerStates:
    def __init__(self, store, queries, table, ttl=45.0):
        self.store, self.q, self.table, self.ttl = store, queries, table, ttl
        self._cache = {}
        self._asked = {}
        self._lock = threading.Lock()

    def get(self, user_key, season_id="", force=False):
        key = f"{season_id}|{user_key}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and not force and now - hit["at"] < self.ttl:
                return hit["state"]
        state = self._load(user_key, season_id)
        with self._lock:
            self._cache[key] = {"at": now, "state": state}
        return state

    def _load(self, user_key, season_id=""):
        rows = self.store.query(f"pstate:{season_id}:{user_key}", self.q.player_state(self.table),
                                [{"name": "user_key", "type": "STRING", "value": str(user_key)},
                                 {"name": "season_id", "type": "STRING", "value": str(season_id)}],
                                ttl=0)
        state = {}
        for r in rows:
            state[r["clue_id"]] = _net({
                "solved": str(r.get("solved")) == "1",
                "earned": int(r.get("earned") or 0),
                "hint_penalty": int(r.get("hint_penalty") or 0),
                "hints": int(r.get("hints") or 0),
                "tried": str(r.get("tried")) == "1",
            })
        return state

    # -- local updates, so the UI never lags behind the player's own action -------------
    def _touch(self, user_key, season_id=""):
        key = f"{season_id}|{user_key}"
        with self._lock:
            entry = self._cache.setdefault(key, {"at": time.time(), "state": {}})
            return entry["state"]

    def note_hint(self, user_key, clue_id, season_id="", penalty=HINT_PENALTY):
        """A hint revealed. The penalty is SET, never accumulated — the same idempotence the SQL gets from
        taking a MAX, so a second click or a second tab cannot make it -10. Returns the new penalty."""
        c = self._touch(user_key, season_id).setdefault(clue_id, _blank())
        c["hints"] = int(c.get("hints") or 0) + 1
        c["hint_penalty"] = int(penalty)
        _net(c)
        return c["hint_penalty"]

    def note_solved(self, user_key, clue_id, points, season_id=""):
        """`points` is the GROSS figure the clock earned. The hint penalty is a separate component and is
        still subtracted, because their -5 is permanent: it is not refunded by answering correctly."""
        c = self._touch(user_key, season_id).setdefault(clue_id, _blank())
        c["solved"] = True
        c["earned"] = max(int(c.get("earned") or 0), int(points))
        _net(c)

    def note_tried(self, user_key, clue_id, season_id=""):
        """A filled-in blank that was not right. This is what paints the widget red, and it is recorded
        locally as well as in the log so the colour does not wait on a warehouse round trip."""
        c = self._touch(user_key, season_id).setdefault(clue_id, _blank())
        c["tried"] = True

    # -- "have they asked the archive yet" ------------------------------------------------
    # Kept OUTSIDE the TTL cache on purpose: that cache is rebuilt from the log table, which does not
    # carry this flag in a form worth reloading, and a rebuild would silently re-lock the lodge box
    # mid-session. Falls back to the table only when memory has nothing, which is what makes it survive
    # a container restart.
    def note_asked(self, user_key, case_id):
        with self._lock:
            self._asked.setdefault(str(user_key), set()).add(str(case_id))

    def has_asked(self, user_key, case_id):
        with self._lock:
            if str(case_id) in self._asked.get(str(user_key), ()):
                return True
        rows = self.store.query(
            f"asked:{user_key}:{case_id}",
            f"SELECT COUNT(*) AS n FROM {self.table} "
            f"WHERE user_key = :user_key AND case_id = :case_id AND event_type = 'question_asked'",
            [{"name": "user_key", "type": "STRING", "value": str(user_key)},
             {"name": "case_id", "type": "STRING", "value": str(case_id)}], ttl=10)
        found = bool(rows) and int(rows[0].get("n") or 0) > 0
        if found:
            self.note_asked(user_key, case_id)
        return found

    def hints_taken(self, user_key, clue_id, season_id=""):
        return int(((self.get(user_key, season_id) or {}).get(clue_id) or {}).get("hints") or 0)

    def hint_penalty(self, user_key, clue_id, season_id=""):
        """What this blank has ALREADY been charged. Read by /api/hint to decide whether a click is a first
        reveal or a free re-open, and by /api/answer so the points it reports are the net ones."""
        return int(((self.get(user_key, season_id) or {}).get(clue_id) or {}).get("hint_penalty") or 0)
