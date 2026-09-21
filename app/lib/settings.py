"""Admin settings — stored as events in the one delta table, latest wins.

There is no fifth component to spend on a settings store, and that constraint produced a better design
than a settings table would have: every change is an append-only audited row that names who made it and
when, so a forgotten override is discoverable rather than invisible. Reads are cached briefly and
invalidated on write.

WHAT THE SETTING IS. `unlockable` is the list of weeks players are allowed to unlock. It is an admin
decision with no schedule behind it, and it applies to EVERY player at once. It does not unlock anything
by itself: each player still clicks to unlock, and that click is what starts their own clock.

WHAT IT DOES NOT DO: re-score anything. A blank is always logged under `week_index` = its own week,
which is an immutable property of the content, so releasing or un-releasing a week can never move a
score that has already been earned.

⛔ ONE ROW HOLDS EVERY SETTING AND THE LATEST ROW WINS, so a write that OMITS a key does not leave that
key alone — it CLEARS it. That is not a tidiness point, it is the sharpest edge in this module, and 
added a second setting which is what made it reachable: `released_weeks()` in app.py reads a missing
`unlockable` as `DEFAULT_RELEASED = [1]`, so saving a client message with a partial payload would have
CLOSED weeks 2-4 for every player at once, mid-game, with the save reporting success. `KEEP` is
therefore the "not provided" marker, and it is deliberately NOT None — None and "" are real values
meaning *clear this setting*. A caller says which of the three it means.
"""
import json, time

KEEP = object()          # "leave whatever is stored alone" — distinct from None/"" which mean "clear it"


class Settings:
    KEYS = ("unlockable", "season_id", "note", "client_message")

    def __init__(self, store, table, ttl=15.0):
        self.store, self.table, self.ttl = store, table, ttl
        self._at = 0.0
        self._cur = {}

    def _read(self):
        rows = self.store.query(
            "settings:latest",
            f"SELECT event_ts, user_display_name, user_email, week_index, season_id, extra "
            f"FROM {self.table} WHERE event_type = 'admin_setting' ORDER BY event_ts DESC LIMIT 1",
            ttl=0)
        if not rows:
            return {}
        r = rows[0]
        try:
            payload = json.loads(r.get("extra") or "{}")
        except Exception:
            payload = {}
        return {"unlockable": payload.get("unlockable"), "season_id": payload.get("season_id"),
                "note": payload.get("note"), "client_message": payload.get("client_message"),
                "set_by": r.get("user_display_name"),
                "set_by_email": r.get("user_email"), "set_at": r.get("event_ts")}

    def current(self, force=False):
        now = time.time()
        if force or now - self._at > self.ttl:
            try:
                self._cur = self._read()
                self._at = now
            except Exception:
                pass                     # a settings read must never take the game down
        return dict(self._cur)

    def set(self, viewer, unlockable=KEEP, season_id=None, note=KEEP, client_message=KEEP,
            real_week=None, row_season_id=None):
        """`season_id` is the setting being requested (None means "leave the scenario alone");
        `row_season_id` is what this ROW is tagged with, which must always be the season actually in
        force, or clearing a week override would write an untagged row.

        Every other setting defaults to `KEEP`: the stored value is carried into the new row rather than
        being blanked by a caller that simply had nothing to say about it. See the module docstring for
        what a partial write used to cost. `season_id` keeps its old default because its None already
        means something specific to the one caller, and changing that would move the scenario."""
        cur = self.current()
        keep = lambda given, key: cur.get(key) if given is KEEP else given
        unlockable = keep(unlockable, "unlockable")
        payload = {"unlockable": unlockable, "season_id": season_id,
                   "note": keep(note, "note"),
                   "client_message": keep(client_message, "client_message")}
        self.store.log("admin_setting", user_key=viewer["user_key"], user_email=viewer["email"],
                       user_display_name=viewer["name"],
                       week_index=(max(unlockable) if unlockable else None),
                       real_week_index=real_week, season_id=row_season_id or season_id, extra=payload)
        self.store.flush()
        self.store.invalidate("settings")
        self._at = 0.0
        return self.current(force=True)

    def history(self, limit=20):
        return self.store.query(
            "settings:history",
            f"SELECT event_ts, user_display_name, week_index, season_id, extra FROM {self.table} "
            f"WHERE event_type = 'admin_setting' ORDER BY event_ts DESC LIMIT {int(limit)}", ttl=5)
