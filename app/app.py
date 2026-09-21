#!/usr/bin/env python3
"""THE GENIE BAKE-OFF — a Genie game, served by the Python standard library.

Four weeks. Each week is a memo with five blanks in it; the player asks a Genie space in plain English,
reads what comes back, and types the value into the blank. Right answers lock green, wrong ones go red
and stay editable. An admin releases a week; the player clicks to unlock it, which starts their own
clock, and the clock pauses whenever they are not looking at that week.

No third-party packages, no requirements.txt, no CDN, no web font, no outbound call at runtime except
to the Databricks workspace itself. That is not minimalism for its own sake: the target workspace is
assumed air-gapped, and a blocked host HANGS rather than failing fast, which renders nothing at all.

Four components exist and no more: this app, a Genie space reached only as the viewer, the read-only
sample tables behind that space, and one delta table this app writes.
"""
import csv, hmac, io, json, os, secrets, sys, threading, time, traceback, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lib import checker, content, durable, game, genie, queries, tablecheck, week as weeklib  # noqa: E402
from lib.config import Config                                             # noqa: E402
from lib.logstore import LogStore
from lib.localstore import LocalStore
from lib import importer                                         # noqa: E402
from lib.settings import Settings, KEEP as SETTING_KEEP                   # noqa: E402
from lib.state import PlayerStates                                        # noqa: E402
from lib.game import Attempts, Clock                                     # noqa: E402

STATIC = os.path.join(HERE, "static")
THEMES = os.path.join(HERE, "theme")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json",
        ".svg": "image/svg+xml", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp"}

def _server_fingerprint():
    """md5 over the server's own source, as THIS CONTAINER has it on disk.

    The static assets can be fetched and compared, but app.py and lib/ are never served — so without this
    a verifier can prove the browser gets the right CSS while the server runs last week's logic. Computed
    once at import: it describes the code that is actually running, not the code on someone's laptop.
    """
    import hashlib
    h = hashlib.md5()
    for rel in ["app.py"] + sorted("lib/" + f for f in os.listdir(os.path.join(HERE, "lib"))
                                   if f.endswith(".py")):
        try:
            with open(os.path.join(HERE, rel), "rb") as f:
                h.update(rel.encode())
                h.update(f.read())
        except OSError:
            h.update(b"?" + rel.encode())
    return h.hexdigest()


SERVER_MD5 = _server_fingerprint()

CFG = Config()
_ALL_SEASONS = content.discover_seasons()
# Only expose scenarios this deployment built a Genie space for (CFG.seasons). Empty means "whatever is
# shipped", which is the right default for a laptop run.
SEASONS = ({k: v for k, v in _ALL_SEASONS.items() if k in CFG.seasons}
           if CFG.seasons else _ALL_SEASONS) or _ALL_SEASONS
SEASON = SEASONS.get(CFG.season_id) or (list(SEASONS.values())[0] if SEASONS else None)
# Locally there is no injected service-principal secret, so DEV_SQL_TOKEN lets a laptop run the whole
# game against the real workspace. It is read only when DATABRICKS_APP_NAME is absent, i.e. never in a
# deployed container.
_DEV_TOKEN = None if CFG.in_databricks else (os.environ.get("DEV_SQL_TOKEN") or None)
# ── WHERE THE ROWS GO ───────────────────────────────────────────────────────────
# Two modes, one interface. Everything below this block — PlayerStates, Settings, Clock, every operator
# aggregate, the importer — talks to `STORE` through the same five methods and does not know or care which
# one it got. That is why local mode needed no changes anywhere else, and why Delta mode is untouched.
#
# `LOG_TABLE` is the name the SQL is built against: the fully-qualified Delta name in delta mode, the
# bare SQLite table name in local mode. Held separately from CFG.log_table so nothing downstream has to
# branch on the mode to build a query.
#
# ⭐ THE MODE IS NOW RESOLVED BEFORE THE STORE IS BUILT, because on the no-CLI path the operator sets
#    the table from inside the app and that setting lives in the WORKSPACE, not in app.yaml (lib/durable.py
#    says why it cannot live in the log store). `DURABLE.read()` is one HTTP round trip at boot, it can
#    never raise, and on a laptop with no service-principal credentials it does not even make the call.
DURABLE = durable.DurableSettings(CFG.host, CFG.client_id, CFG.client_secret,
                                  CFG.app_name or "genie-bake-off", path=CFG.state_path,
                                  token=_DEV_TOKEN)
_DOC, _DOC_STATE, _DOC_DETAIL = DURABLE.read(force=True)
_APPLIED = CFG.apply_overrides(_DOC)
if _APPLIED:
    print(f"[storage] operator settings from {DURABLE.path}: {_APPLIED}", file=sys.stderr, flush=True)
elif _DOC_STATE not in ("present", "absent"):
    print(f"[storage] durable settings {_DOC_STATE}: {_DOC_DETAIL}", file=sys.stderr, flush=True)


def build_store():
    """-> (store, log_table) for the mode now in force. The one place a store is constructed, so the
    runtime switch and the boot take the same path rather than two that can drift."""
    if CFG.storage_mode == "local":
        print(f"[storage] LOCAL mode: sqlite at {CFG.local_db_path} table={CFG.local_db_table} "
              f"(ephemeral — a restart loses it; export/import is the durability story)",
              file=sys.stderr, flush=True)
        return LocalStore(CFG.local_db_path, table=CFG.local_db_table, build=CFG.build), \
            CFG.local_db_table
    if CFG.log_table and CFG.warehouse_id and (CFG.client_id or _DEV_TOKEN):
        print(f"[storage] DELTA mode: {CFG.log_table} via warehouse {CFG.warehouse_id} "
              f"(source: {CFG.sources.get('log_table')})", file=sys.stderr, flush=True)
        return LogStore(CFG.host, CFG.log_table, CFG.warehouse_id, CFG.client_id, CFG.client_secret,
                        build=CFG.build, dev_token=_DEV_TOKEN), CFG.log_table
    return None, CFG.log_table


STORE, LOG_TABLE = build_store()
STATES = PlayerStates(STORE, queries, LOG_TABLE) if STORE else None
SETTINGS = Settings(STORE, LOG_TABLE) if STORE else None
BOOTED = time.time()
_SPACE = {"id": CFG.genie_space_id or None, "title": None, "error": None,
          "by_title": ({CFG.genie_space_title: CFG.genie_space_id}
                       if CFG.genie_space_id else {})}

# The clock is per SEASON: a scenario switch must not carry one scenario's accumulated time into another.
CLOCKS = {}
# One per process: the throttle is a speed bump on brute force, not persisted state. See game.Attempts.
ATTEMPTS = Attempts()


def clock_for(season_id):
    if season_id not in CLOCKS:
        CLOCKS[season_id] = Clock(STORE, queries, LOG_TABLE, season_id)
    return CLOCKS[season_id]


# ── SWITCHING WHERE THE ROWS GO, WITHOUT A REDEPLOY ───────────────────────────────────────────
# THE REQUIREMENT: the operator can name the catalog.schema.table to use, from inside the app. A person
# with no CLI cannot restart it, so the switch has to happen in the process that is already running.
#
# ⛔ EVERY OBJECT BUILT ON THE STORE HAS TO BE REBUILT, and that list is the whole risk here. `PlayerStates`
#    caches per-blank progress, each `Clock` holds a forward-only accumulator seeded from the old table, and
#    `Settings` caches the releases — all keyed to the store they were made with. Rebinding `STORE` alone
#    would leave three objects still reading SQLite while new rows went to Delta: the board would show the
#    old numbers, and the clock would keep the old total because `max(memory, table)` never moves DOWN.
#    So this reuses `clear_in_memory_state()`'s inventory rather than keeping a second list of caches.
_SWITCHES = []                      # what this process has done, for the operator page and /api/health


def rebind_store(reason="operator", carry_rows=True):
    """Rebuild the store and everything hanging off it for the mode now in CFG. -> a report dict.

    `carry_rows` copies whatever the ephemeral SQLite already holds into the new Delta table, deduplicated
    by `event_id`. Without it, a player who answered before the operator pointed at a table would appear
    to have their work deleted by an admin action — the rows are not lost, they are simply in a file
    nothing reads any more, which is the same thing from where the player is sitting.
    """
    global STORE, LOG_TABLE, STATES, SETTINGS
    old, old_table = STORE, LOG_TABLE
    rows = []
    if carry_rows and old is not None and isinstance(old, LocalStore):
        try:
            # No flush: LocalStore commits inside `log()` (its own flush is a no-op), so the rows are
            # already there. A commit on a request thread is forbidden in this file for a good reason —
            # see test_no_request_path_performs_a_synchronous_log_commit — and draining the OTHER kind of
            # store happens inside LogStore.stop(), where it belongs.
            rows = old.query("switch:export", queries.export_all(old_table),
                             [{"name": "lim", "type": "INT", "value": "200000"}], ttl=0)
        except Exception as e:
            rows = []
            _SWITCHES.append({"at": time.time(), "warn": f"could not read the local rows: {e}"})
    new, new_table = build_store()
    if new is None:
        return {"ok": False, "error": "nothing to switch to: no table, warehouse or credentials"}
    carried = 0
    if rows:
        try:
            ids = [r.get("event_id") for r in rows if r.get("event_id")]
            already = new.existing_event_ids(ids) if hasattr(new, "existing_event_ids") else set()
            fresh = [r for r in rows if r.get("event_id") and r["event_id"] not in already]
            carried = new.insert_rows(fresh) if fresh else 0
        except Exception as e:
            # ⭐ NOT fatal, and NOT silent. The new store is still the right place for new rows, and the
            #    SQLite file is still on disk until this container dies — so the honest outcome is "switched,
            #    and these rows did not come across", which the operator page prints.
            _SWITCHES.append({"at": time.time(), "warn": f"the existing local rows were not copied: {e}"})
    STORE, LOG_TABLE = new, new_table
    STATES = PlayerStates(STORE, queries, LOG_TABLE)
    SETTINGS = Settings(STORE, LOG_TABLE)
    CLOCKS.clear()
    if old is not None and hasattr(old, "stop"):
        try:
            old.stop()
        except Exception:
            pass
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reason": reason,
           "from": {"mode": ("local" if isinstance(old, LocalStore) else
                             "delta" if old else "none"), "table": old_table},
           "to": {"mode": CFG.storage_mode, "table": new_table}, "rows_carried": carried,
           "rows_found_locally": len(rows), "ok": True}
    _SWITCHES.append(rec)
    print(f"[storage] switched: {rec}", file=sys.stderr, flush=True)
    return rec


# ── THE RESET, AND WHY CLEARING MEMORY IS THE HARD HALF ─────────────────────────
# THE REQUIREMENT: a reset button in the operator dashboard that resets the game and everyone's
# stats. Deleting the rows is the easy part and it is NOT a reset:
#   * `PlayerStates._cache` holds each player's per-blank progress for 45 seconds;
#   * ⛔ `Clock._acc` is seeded from the table and only ever moves FORWARD — `max(memory, table)` — so a
#     ZEROED TABLE CANNOT CLEAR A RUNNING CLOCK. That is deliberate (the direction that loses a player's
#     time is the one that harms them) and it is exactly what makes a plain DELETE look broken;
#   * `PlayerStates._asked` outlives a table read on purpose.
# A button INSIDE the app cannot restart the app — the restart would kill the request asking for it — so
# the endpoint clears all of it in process, explicitly, rather than waiting for anything to expire.
_RESET_SEEN = {"at": None}            # the marker this process has already acted on


def clear_in_memory_state():
    """Wipe every in-process cache the game's numbers come from. -> the list of what was cleared, for the
    response, because an operator who presses reset is owed a statement of what it actually did."""
    cleared = []
    if STATES:
        with STATES._lock:
            n = len(STATES._cache)
            STATES._cache.clear()
            STATES._asked.clear()
        cleared.append(f"player score cache ({n} entr{'y' if n == 1 else 'ies'})")
        cleared.append("has-asked flags")
    for sid, clk in list(CLOCKS.items()):
        with clk._lock:
            n = len(clk._acc)
            clk._acc.clear()
            clk._loaded.clear()
        cleared.append(f"week clocks for {sid or '(default)'} ({n})")
    ATTEMPTS.__init__()                       # wrong-answer throttle: per process, not persisted
    cleared.append("wrong-answer throttles")
    if STORE:
        STORE.invalidate("")
        cleared.append("query cache")
    if SETTINGS:
        SETTINGS._at = 0.0                    # re-read the releases rather than serve a cached list
        cleared.append("settings cache")
    return cleared


def honour_remote_reset():
    """If a reset happened in ANOTHER container, clear this one's memory too.

    ⭐ THIS IS WHAT MAKES THE BUTTON CORRECT WITHOUT KNOWING HOW MANY CONTAINERS THERE ARE. I could not
    establish that from outside the platform — the Apps API exposes no replica or scale control, only
    `compute_size`, and process identity was consistent across 14 rapid requests — but "I could not find a
    second one" is not "there is only one". So instead of assuming, every container checks the marker the
    reset wrote and clears itself when it changes. Called on the page-load path, so a stale container
    corrects itself the first time anybody opens the game rather than after a TTL nobody can see.
    """
    if not STORE:
        return None
    try:
        rows = STORE.query("reset:last", queries.last_reset(LOG_TABLE), ttl=10.0)
    except Exception:
        return None                           # a reset check must never take the game down
    at = (rows[0].get("reset_at") if rows else None) or None
    if at and _RESET_SEEN["at"] != at:
        first = _RESET_SEEN["at"] is None
        _RESET_SEEN["at"] = at
        # ⛔ NOT on the first sighting: a container starting up reads a marker from a reset that happened
        #    before it existed, and its caches are already empty. Clearing there would be harmless but it
        #    would also make the log line a lie, and this line is how the behaviour is observed.
        if not first:
            print(f"[reset] another container reset the game at {at} — clearing this one's memory",
                  flush=True)
            return clear_in_memory_state()
    return None


# ⛔ WHICH WEEKS ARE RELEASED is an ADMIN decision with no schedule: weeks are made unlockable
#    from the admin screen, and nothing runs on a calendar. Until an admin saves a setting there is
#    no stored list, and a deployment with nothing playable looks broken rather than unconfigured, so
#    week 1 is released by default. Everything after it waits for a human.
DEFAULT_RELEASED = [1]

# Asked FRESH on every app open, never cached, so the animations run the moment the app opens and a
# broken Genie is visible immediately. The space's instructions carry a paragraph answering it.
GREETING_Q = "What can you do?"


def genie_space_health():
    """`genie_space` for /api/health, with a STATUS that cannot be read as a fault.

    ⛔ THE SHAPE THIS REPLACES COLLAPSED THREE STATES INTO TWO. Space resolution is lazy and BY TITLE, so on
    a container that has not yet been asked a question `_SPACE` is `{"id": None, "title": None,
    "error": None}` — identical in every readable field to a container whose resolution FAILED. Any
    reasonable defensive probe (`if not health["genie_space"]["id"]`) therefore reports a Genie outage on a
    workspace where Genie answers perfectly, and a customer's own monitoring would raise an alert for a
    deployment that is completely healthy. That is the FALSE RED direction, which costs more than a false
    green because somebody goes and fixes a working thing.

    So the three states are named: `resolved`, `not_resolved_yet`, `unresolved_error`. ⭐ And the health
    check still does NOT ask Genie anything — it must not spend a question, because a question is the
    adoption metric this whole product exists to move. `not_resolved_yet` says how to find out instead.
    """
    info = dict(_SPACE)
    if info.get("error"):
        info["status"] = "unresolved_error"
        info["note"] = ("the space could not be resolved; the message in `error` is the reason. Players "
                        "will see the genie fail on their first question.")
    elif info.get("id"):
        info["status"] = "resolved"
        info["note"] = "resolved and in use"
    else:
        info["status"] = "not_resolved_yet"
        info["note"] = ("NOT A FAULT. The space is resolved by TITLE, lazily, on the first question anyone "
                        "asks — so `id` and `title` are null on a container nobody has asked yet. This "
                        "endpoint deliberately does not ask one. To know that Genie works, ask a real "
                        "question through /api/chat; no value in this payload can tell you.")
    return info


def released_weeks(total):
    cur = (SETTINGS.current() if SETTINGS else {}) or {}
    raw = cur.get("unlockable")
    if raw in (None, "", []):
        return list(DEFAULT_RELEASED)
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    out = []
    for x in raw:
        try:
            w = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= w <= total and w not in out:
            out.append(w)
    return sorted(out)


# ⭐ the operator's own line on the home page, for "Welcome Acme team!".
#    It is a SETTING, so it lives where every other setting lives: an append-only `admin_setting` row,
#    which is what makes it survive a redeploy. Nothing else in the app would.
#    Capped here rather than only at the input, because the cap is a property of the setting and an
#    input's maxlength is a property of one browser's form.
CLIENT_MESSAGE_MAX = 120


def client_message():
    """The operator's custom greeting line, or "" when there is none.

    Returns "" for every not-set shape — absent, None, blank, whitespace — so the caller has one
    falsy answer to render nothing from, rather than four spellings of empty that each need testing."""
    cur = (SETTINGS.current() if SETTINGS else {}) or {}
    raw = cur.get("client_message")
    if not isinstance(raw, str):
        return ""
    return raw.strip()[:CLIENT_MESSAGE_MAX]


# ------------------------------------------------------------------ viewer identity
# Laptop-only identity fallback. Read ONLY when DATABRICKS_APP_NAME is absent, i.e. never in a deployed
# container, where the platform supplies these headers and overwrites anything a caller sends.
_DEV_VIEWER = None if CFG.in_databricks else {
    "token": os.environ.get("DEV_VIEWER_TOKEN"), "user": os.environ.get("DEV_VIEWER_USER"),
    "email": os.environ.get("DEV_VIEWER_EMAIL"), "name": os.environ.get("DEV_VIEWER_NAME"),
}


_NAME_CACHE = {}


def _pretty_from_email(email):
    """`first.last@example.com` -> `First Last`. Last resort, but a decent one."""
    local = (email or "").split("@")[0]
    parts = [p for p in local.replace("_", ".").replace("-", ".").split(".") if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts) or "Analyst"


def _scim_display_name(token, user_key):
    """Ask the workspace who this is. Needs no extra scope — `iam.current-user:read` is a default.

    Why this exists: `x-forwarded-preferred-username` is NOT reliably a display name. On the
    bearer-token path it arrived as a display name, but a REAL BROWSER SESSION sent
    `first.last@example.com` — so the greeting read "Afternoon, first.last." to the first
    human who opened it. Measured; only a real person's session revealed it, because the
    local dev identity supplies the header by hand.
    """
    if not token:
        return None
    hit = _NAME_CACHE.get(user_key)
    if hit is not None:
        return hit or None
    try:
        req = urllib.request.Request(host() + "/api/2.0/preview/scim/v2/Me",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=8) as r:
            me = json.loads(r.read().decode() or "{}")
        name = (me.get("displayName") or "").strip()
        given = ((me.get("name") or {}).get("givenName") or "").strip()
        best = name or given or None
    except Exception:
        best = None
    _NAME_CACHE[user_key] = best or ""
    return best


def viewer(handler):
    h = handler.headers
    tok = h.get("x-forwarded-access-token")
    if not tok and _DEV_VIEWER and _DEV_VIEWER.get("token"):
        class _H(dict):
            def get(self, k, d=None):
                return dict.get(self, k.lower(), d)
        h = _H({"x-forwarded-access-token": _DEV_VIEWER["token"],
                "x-forwarded-user": _DEV_VIEWER.get("user") or "",
                "x-forwarded-email": _DEV_VIEWER.get("email") or "",
                "x-forwarded-preferred-username": _DEV_VIEWER.get("name") or ""})
        tok = _DEV_VIEWER["token"]
    raw_user = h.get("x-forwarded-user") or ""
    user_key = raw_user.split("@")[0] if raw_user else ""
    email = h.get("x-forwarded-email") or ""
    if not user_key and email:
        user_key = email
    forwarded = (h.get("x-forwarded-preferred-username") or "").strip()
    # A forwarded name containing "@" is an email address, not a display name — see _scim_display_name.
    name = forwarded if (forwarded and "@" not in forwarded) else None
    name = name or _scim_display_name(tok, user_key) or _pretty_from_email(email or forwarded)
    return {"token": tok, "user_key": user_key, "email": email, "name": name,
            "first_name": (name.split(" ")[0] if name else "Analyst")}


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE OPERATOR UNLOCK SESSION.
#
# THE REQUIREMENT: once a user enters the password, that browser stays unlocked for as long as the
# app is kept alive. So this is a SERVER-SIDE session for the life of the
# CONTAINER — in memory, no table, no fifth component.
#
# ⛔ WHAT A REDEPLOY DOES, stated because it will happen and should not surprise anybody: the container
#    restarts, this dict is empty, and every operator re-enters the password once. That is correct rather
#    than a bug — "as long as the app is kept alive" is exactly the life of the process — and it fails to
#    LOCKED, which is the safe direction.
#
# ⛔ AND WHAT THIS IS NOT. The password reaches the container through app.yaml, so anyone who can read the
#    app's source in the workspace can read it. This is a gate against PLAYERS wandering into the operator
#    page, not a secret store and not protection from someone with workspace access. Saying otherwise
#    would be overselling it, and the README says the same.
_OP_SESSIONS = {}                    # token -> {"user_key": str, "at": float}

# The reset's backup token: minted only by a VERIFIED backup, spent once, and bound to the operator who
# took it. In memory on purpose — a token that survived a restart would let someone reset on the strength
# of a backup taken before whatever happened in between.
_RESET_TOKENS = {}
RESET_TOKEN_TTL_S = 900
RESET_CONFIRM = "RESET EVERYTHING"
_OP_LOCK = threading.Lock()
OP_COOKIE = "nd_op"

# ⛔ THERE IS NO LOCKOUT, AND THAT IS DELIBERATE: there is no cooling-off after a wrong password,
#    because for a game the simplicity is worth more. An earlier 5-attempt cooling-off keyed on
#    `user_key` is gone, and deleting it also removed a hazard that version had found rather than
#    merely documented.
#
# ⭐ THE HAZARD, so nobody helpfully reinstates it: attempts were counted PER IDENTITY, and an agent
#    verifying the gate authenticates AS the operator — so three test attempts spent three of the real
#    operator's five and left them two mistakes from being locked out of their own deployment, caused
#    by the testing rather than by them. A throttle keyed on identity means a TESTER SPENDS THE REAL USER'S BUDGET. If a rate limit is ever
#    genuinely needed, key it on something that is not the person, and give the test path its own bucket.
#
#    What remains against brute force: the address allowlist in front of it (an attacker needs a workspace
#    account that is already on the operator list), and the fact that nothing about the operator page is a
#    secret worth mounting an online attack for — it releases weeks and shows a log, both recoverable.


def _cookies(handler):
    """Parse the request's Cookie header. Stdlib only; no dependency for four lines of parsing."""
    raw = handler.headers.get("Cookie") or ""
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, _, val = part.partition("=")
            out[k.strip()] = val.strip()
    return out


def op_session_valid(v, handler):
    """Does this request carry a live unlock session belonging to THIS viewer?

    Bound to `user_key` on purpose: a session is "unlocked for them", so a token that leaked to another
    identity is not enough on its own. A viewer with no key cannot hold a session at all.
    """
    tok = _cookies(handler).get(OP_COOKIE)
    if not tok or not v.get("user_key"):
        return False
    with _OP_LOCK:
        sess = _OP_SESSIONS.get(tok)
        return bool(sess and sess["user_key"] == v["user_key"])


def op_unlock(v, supplied):
    """-> (ok, token_or_None, message). Never logs or returns the password, right or wrong."""
    now = time.time()
    if not CFG.operator_password_set:
        return False, None, CFG.unlock_hint()
    # hmac.compare_digest: a plain == leaks the length of the matching prefix through timing. The exposure
    # here is small, the fix is one call, and the habit is the point.
    ok = hmac.compare_digest(str(supplied or ""), CFG.operator_password)
    with _OP_LOCK:
        if not ok:
            # No counter, no cooldown, no state kept about the attempt. Just say it was wrong.
            return False, None, "that is not the operator password"
        tok = secrets.token_urlsafe(32)
        _OP_SESSIONS[tok] = {"user_key": v["user_key"], "at": now}
        return True, tok, "unlocked"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE ONE GATE.  Operator, Settings, Raw log and Export all call this and nothing else.
#
# ⚠️ ONE LOCK NOW, NOT TWO. The address allowlist is gone — anyone with the password can act as
#    operator, which is proportionate for a game — so what remains is the password and the
#    per-browser unlock session it mints.
#
#    ⛔ THE HISTORY IS WORTH KEEPING because it says which protection is actually load-bearing. An
#    open gate once let a second identity fetch /api/export.csv and read another player's
#    `expected_value` — the answer key; 5 of 13 live rows carried one. TWO things were changed then, and
#    only one of them was the gate:
#      1. `expected_value` is no longer projected by ANY query (queries.export_all). Even an operator
#         cannot download the answers, and no future gate mistake can re-expose them.
#      2. the allowlist — which is what  has now removed.
#    So the leak is still fixed, at its root, INDEPENDENTLY of who can reach the page. That is precisely
#    what makes removing the allowlist an acceptable simplification rather than a reopening of the hole.
#
#    What the gate still protects: the Settings page, which RELEASES WEEKS FOR EVERY PLAYER; the activity
#    log and its export; and the  RESET, which deletes everyone's progress. The reset carries its own
#    protections (a verified backup, an exact confirmation word, and a logged row naming who pressed it).
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def operator_eligible(v):
    """Is this viewer ALLOWED to reach the operator door at all, before any password is entered?

    ⭐ SINCE  THE ANSWER IS ALWAYS YES, and this function stays rather than being inlined as `True`
    for two reasons.

    First, THE PAYLOAD FIELD MUST NOT DISAPPEAR. `app/static/app.js` hides the Operator nav button when
    `is_operator` is false, and the unlock form lives BEHIND that button. Delete the field and the door is
    inside the room: nobody could ever enter the password. So `is_operator` keeps its meaning — "you may
    knock" — and simply becomes true for everyone, which needs no change from the surface lane.

    Second, it is the ONE place an allowlist would come back if anyone ever wants one, with the reason it
    was removed recorded beside it: operator access does not need to be strict for a game.
    A `True` scattered across five call sites would not carry that.

    ⚠️ Consequence, stated rather than discovered: this used to be checked BEFORE the password, which kept
       the unlock endpoint from being a password ORACLE for anybody with a workspace account. It no longer
       is, and  removed the attempt limiter, so guesses are unlimited. That is the accepted trade.
    """
    return True


def operator_gate(v, handler=None):
    """The gate: ONE lock since an unlock session proving THIS browser entered the password.

    ⛔ `handler=None` MEANS "no request to read a cookie from", and it must keep returning False. A caller
       that forgets to pass the request gets a CLOSED gate, not an open one — that direction is what makes
       a mistake in a new call site fail safe rather than silently exposing the Settings page and the reset.
    """
    if not CFG.operator_password_set:
        # Since  a password ALWAYS deploys (Config.DEFAULT_OPERATOR_PASSWORD), so this branch is only
        # reachable if someone deliberately blanks it. It stays, and it stays CLOSED, because the failure
        # direction of a gate with no secret must not be "open".
        return False
    return bool(handler is not None and op_session_valid(v, handler))


def theme_payload(name):
    path = os.path.join(THEMES, f"{os.path.basename(name)}.json")
    if not os.path.exists(path):
        path = os.path.join(THEMES, "neutral.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def active_season(qs=None):
    """The content pack in play: an admin setting wins, then config, then whatever is shipped."""
    want = None
    if SETTINGS:
        want = (SETTINGS.current() or {}).get("season_id")
    if qs and qs.get("season"):
        want = qs["season"][0]
    return SEASONS.get(want) or SEASON


def space_id(v, season=None):
    """Resolve the ACTIVE scenario's Genie space by title, using the viewer's own token.

    Cached per title rather than globally, because switching scenario switches space — a single cached id
    would keep answering from the previous scenario's data, which is exactly the kind of wrongness that
    raises no error.
    """
    season = season or active_season()
    # ⛔ AN OPERATOR-SET SPACE BEATS THE CONTENT PACK'S OWN TITLE, and it has to. The pack ships
    #    `space_title: "The Data Desk"`, which is the right default and the wrong answer for a manual
    #    install whose space is called something else — and that is the one setting  exists to let
    #    somebody change without a CLI. The ordering is therefore: what an operator saved, then the pack,
    #    then config's default. `CFG.sources` is what says which of those happened.
    operator_set = CFG.sources.get("genie_space") == "operator"
    title = (CFG.genie_space_title if operator_set
             else ((season.space_title if season else None) or CFG.genie_space_title)) \
            + CFG.genie_space_suffix
    hit = _SPACE["by_title"].get(title)
    if hit:
        return hit, None
    sid, err = genie.resolve_space(CFG.host, v["token"], title, CFG.genie_space_id)
    if sid:
        _SPACE["by_title"][title] = sid
    _SPACE["id"], _SPACE["title"], _SPACE["error"] = sid, title, err
    return sid, err


# ── WHAT THE OPERATOR PAGE NEEDS TO SHOW ABOUT THIS DEPLOYMENT ────────────────────────────────
def _sp_token():
    """A bearer token for the app's OWN service principal — the identity that does the writing.

    ⛔ IT MUST BE THIS IDENTITY AND NOT THE OPERATOR'S. An operator with MANAGE on the catalog would pass
       every accessibility check while the app still cannot write a row, which is the exact silent failure
       the check exists to catch. Reuses the log store's token when there is one (it caches and refreshes);
       falls back to minting one, and on a laptop to DEV_SQL_TOKEN.
    """
    if STORE is not None and hasattr(STORE, "_token"):
        return STORE._token()
    if _DEV_TOKEN:
        return _DEV_TOKEN
    return durable.DurableSettings(CFG.host, CFG.client_id, CFG.client_secret,
                                   CFG.app_name)._token()


def sp_principal():
    """The name to use in a `GRANT … TO` statement for this app's own service principal.

    ⭐ THE CLIENT ID, NOT THE DISPLAY NAME. Measured: the grants the platform itself makes when
    you add a Unity Catalog table as an app resource are recorded against the SP's **client id** (a uuid),
    and `GRANT … TO \\`<uuid>\\`` is accepted. The display name (`app-xxxxxx <app-name>`) contains a space
    and is not what UC records, so a statement built from it is the kind of instruction that fails for the
    person following it and works for nobody.
    """
    return CFG.client_id or "<the app's service principal>"


def storage_instructions(fqn=None):
    """The SQL and the UI steps the Operator page prints, with this app's own identity substituted.

    ⛔ THE TWO ROUTES ARE NOT EQUIVALENT AND THE PAGE MUST NOT IMPLY THEY ARE. Adding the table as an app
       RESOURCE removes the SQL STEP — measured: the platform then issues `SELECT`+`MODIFY` on the table,
       `USE SCHEMA`, and `USE CATALOG`, which is more than the deploy hook can do. It does NOT lower the
       PRIVILEGE BAR: the grantor recorded on every one of those rows is the person who added the resource,
       so somebody without `MANAGE` (or ownership) on the catalog cannot do it through the UI either. They
       need the catalog's owner, whichever route they take.
    """
    target = fqn or CFG.log_table or "<catalog>.<schema>.activity_log"
    return {
        "principal": sp_principal(),
        "create_sql": tablecheck.create_sql(target),
        "grant_sql": tablecheck.grant_sql(target, sp_principal()),
        "resource_route": [
            "Compute → Apps → this app → Edit → App resources → Add resource → Unity Catalog table.",
            f"Pick the table ({target}) and tick BOTH permissions: SELECT and MODIFY (add the table twice "
            f"if the form takes one permission at a time).",
            "Add a second resource of type SQL warehouse with permission CAN USE, if the app has none.",
            "Save. Databricks then grants this app's service principal SELECT and MODIFY on the table, "
            "USE SCHEMA on its schema and USE CATALOG on its catalog — no SQL at all.",
        ],
        "resource_route_caveat": (
            "This removes the SQL step, not the permission requirement: Databricks makes those grants AS "
            "YOU, so whoever adds the resource still needs to be the catalog's owner or hold MANAGE on it. "
            "If you are not, the person who is has to add the resource (or run the GRANTs below) — the "
            "table's own owner is not enough, because USE CATALOG is a catalog-level privilege."),
        "sql_route_note": (
            "The other route, if you would rather not touch the app's resources: run the CREATE TABLE and "
            "then these three grants in a SQL editor. The first one needs the catalog's owner or MANAGE."),
    }


def operator_config():
    """One payload for the Genie-space and storage cards. No secrets: paths, states and provenance only."""
    store_health = STORE.healthy() if STORE else None
    doc, doc_state, doc_detail = DURABLE.read(force=True)
    pub = DURABLE.public()
    season = active_season()
    return {
        "ok": True,
        "app_name": CFG.app_name or None,
        "build": CFG.build,
        "storage": CFG.storage_public(),
        "log_writer": store_health or "not configured",
        "warehouse_id": CFG.warehouse_id or None,
        "warehouse_source": CFG.sources.get("warehouse_id"),
        "rows_now": (STORE.count() if (STORE and hasattr(STORE, "count")) else None),
        "switches": _SWITCHES[-5:],
        # Where an operator setting is kept, and whether it can be kept at all. `state` is one of
        # present / absent / denied / unavailable / error — three of which mean "do not believe an empty
        # answer", so the page renders them differently.
        "durable": pub,
        "genie": {
            "title_in_force": (CFG.genie_space_title if CFG.sources.get("genie_space") == "operator"
                               else ((season.space_title if season else None)
                                     or CFG.genie_space_title)) + CFG.genie_space_suffix,
            "space_id_in_force": CFG.genie_space_id or None,
            "source": CFG.sources.get("genie_space"),
            "pack_title": (season.space_title if season else None),
            "env_title": (os.environ.get("GENIE_SPACE_TITLE") or None),
            "resolved": genie_space_health(),
        },
        "instructions": storage_instructions(),
        "settings_doc": {k: doc.get(k) for k in ("log_table_fqn", "warehouse_id", "genie_space_title",
                                                 "genie_space_id", "set_by", "set_at")},
        "settings_state": doc_state,
        "settings_detail": doc_detail,
    }


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "night-desk"

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    # -- plumbing ---------------------------------------------------------------
    def _send(self, body, code=200, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, msg, code=400, **kw):
        self._send({"ok": False, "error": msg, **kw}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._err("not found", 404)
        with open(full, "rb") as f:
            data = f.read()
        self._send(data, 200, MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

    # -- routes -----------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path.startswith("/api/"):
                return self._api_get(u.path, qs)
            if u.path in ("/", "/index.html") or not u.path.startswith("/api"):
                return self._static(u.path)
        except Exception:
            traceback.print_exc()
            return self._err("the desk hit an internal error", 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            # ⭐ The importer takes the CSV as the RAW request body, routed before `_body()` can touch it:
            #    `_body()` JSON-parses and returns {} on failure, so a CSV posted through it would arrive
            #    as an empty dict and the import would report "empty file" for a perfectly good export.
            #    Raw also means an operator can restore with plain curl if the UI is unavailable:
            #      curl -X POST --data-binary @export.csv "$APP/api/import?filename=export.csv"
            if u.path == "/api/import":
                return self._api_import(parse_qs(u.query))
            if u.path == "/api/operator/unlock":
                return self._api_unlock()
            return self._api_post(u.path, self._body(), parse_qs(u.query))
        except Exception:
            traceback.print_exc()
            return self._err("the desk hit an internal error", 500)

    # -- operator unlock -------------------------------------
    def _api_unlock(self):
        """Exchange the operator password for a per-browser session cookie.

        ⛔ NOTHING ABOUT THE PASSWORD IS EVER ECHOED — not on success, not on failure, not into the
           activity log. An `admin_setting`-style row here would put it in the one table this app exports
           to CSV, which is the artefact an operator downloads and could forward. So this endpoint writes
           NO log row at all: the thing worth recording is "someone unlocked", and that is not worth the
           risk of a password-shaped value near a logging call.

        ⚠️ AND SINCE  THIS *IS* A PASSWORD ORACLE for anybody with a workspace account: the allowlist
           that used to be checked first is gone, and  removed the attempt limiter, so guesses are
           unlimited. Recorded here because it is a consequence of a deliberate simplification — "its just
           a game" — and not something to be surprised by later. What it reaches is the Settings page, the
           activity log, and the reset; the answer key is NOT behind it (no query projects expected_value).
        """
        v = viewer(self)
        if not v["token"]:
            return self._err("no viewer token on this request — open the app from the workspace", 401)
        if not operator_eligible(v):
            # Same 403 an ineligible viewer gets from every operator surface. Deliberately says nothing
            # about whether a password exists or was right.
            return self._err("not for you", 403)
        body = self._body()
        ok, tok, msg = op_unlock(v, (body or {}).get("password"))
        if not ok:
            # 401, not 400: the request was well formed and the credential was refused.
            return self._err(msg, 401, password_required=CFG.operator_password_set)
        # HttpOnly so page scripts cannot read it; SameSite=Lax so it is not sent cross-site. Secure only
        # in the container, because a laptop run is plain http and a Secure cookie would silently never
        # come back — which would look like the password being rejected.
        parts = [f"{OP_COOKIE}={tok}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if CFG.in_databricks:
            parts.append("Secure")
        return self._send({"ok": True, "unlocked": True}, 200,
                          extra={"Set-Cookie": "; ".join(parts)})

    # -- log import -----------------------------------------
    def _api_import(self, qs):
        """Restore scores / progress / leaderboard from a previously exported CSV.

        ⛔ FAILS CLOSED. `importer.parse` validates the WHOLE file and raises before anything is written,
           so a truncated or foreign CSV leaves the store untouched. That is the opposite choice from the
           log WRITER, which fails open so a warehouse wobble never takes the game down — the difference
           is that a wrong write here poisons the scores that are the entire point, while a missed log row
           costs one row of telemetry.

        `?dry=1` returns exactly what a real import would do, having written nothing. It is the honest
        version of a confirmation prompt: the operator sees the row count, the event-type breakdown and
        whether the ordering is recoverable BEFORE committing.
        """
        v = viewer(self)
        if not operator_gate(v, self):
            return self._err("not for you", 403)
        if not STORE:
            return self._err("no store configured — nothing to import into", 503)
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return self._err("no file in that request — POST the CSV as the request body.", 400)
        if n > importer.MAX_BYTES:
            return self._err(f"that file is {n // 1048576} MB; the limit is "
                             f"{importer.MAX_BYTES // 1048576} MB.", 413)
        raw = self.rfile.read(n)
        name = (qs.get("filename") or ["upload.csv"])[0][:200]
        dry = (qs.get("dry") or ["0"])[0] not in ("0", "", "false")
        try:
            plan = importer.parse(raw, name)
        except importer.ImportRefused as e:
            # 422, not 500: the request was understood and deliberately refused. The message is written to
            # be read by a person and is shown verbatim, because "invalid file" tells them nothing about
            # which file to go and fetch instead.
            return self._err(str(e), 422)
        if dry:
            preview = {k: vv for k, vv in plan.items() if k != "rows"}
            preview["ok"] = True
            preview["dry_run"] = True
            already = STORE.existing_event_ids([r["event_id"] for r in plan["rows"]])
            preview["already_present"] = len(already)
            preview["would_write"] = plan["row_count"] - len(already)
            # ⛔ SAME KEY NAME AS THE REAL IMPORT. The preview and the result are rendered by ONE function,
            #    so a key the real path calls `rows_in_file` and the preview called only `row_count` printed
            #    a literal "undefined row(s) in the file" in the dry run — found by reading the served page,
            #    not by reading this code, because both sides individually looked right.
            preview["rows_in_file"] = plan["row_count"]
            return self._send(preview)
        result = importer.apply_plan(STORE, plan, operator=(v["email"] or v["user_key"]))
        # Every cache downstream of the table is now stale: scores, board, weeks, settings, stats.
        if STATES:
            STATES._cache.clear()
        CLOCKS.clear()
        if SETTINGS:
            SETTINGS._at = 0.0
        return self._send(result)

    # -- GET api ----------------------------------------------------------------
    def _api_get(self, path, qs):
        v = viewer(self)

        if path == "/api/health":
            return self._send({
                "ok": not CFG.missing(), "build": CFG.build, "uptime_s": round(time.time() - BOOTED, 1),
                "missing_config": CFG.missing(), "season": CFG.season_id,
                "seasons_available": sorted(SEASONS), "seasons_shipped": sorted(_ALL_SEASONS),
                "blanks": sum(len(c["clues"]) for c in (active_season().pack["cases"]
                                                       if active_season() else [])),
                "weeks": (active_season().total_weeks if active_season() else 0),
                "released_weeks": released_weeks(active_season().total_weeks if active_season() else 4),
                "genie_space": genie_space_health(), "log_writer": STORE.healthy() if STORE else "not configured",
                # Which mode this container is actually in, so a deploy verifier can assert it rather
                # than infer it from whether a warehouse id happens to be set.
                "storage": CFG.storage_public(),
                # The STATE of the gate, never the value. A deploy verifier can assert that a password is
                # configured without knowing it, and `operator_sessions` shows a redeploy really did clear
                # them — which is the claim the README makes about "as long as the app is kept alive".
                "operator_password_required": CFG.operator_password_set,
                "operator_sessions": len(_OP_SESSIONS),
                "greeting_question": GREETING_Q,
                # The container's own answer to "which server code am I running". See _server_fingerprint.
                "server_md5": SERVER_MD5,
                "tz_mode": weeklib.tz_mode(CFG.timezone), "viewer_present": bool(v["token"]),
            })

        if path == "/api/game":
            season = active_season(qs)
            if not season:
                return self._err("no content pack is installed", 503)
            sid = season.pack["season_id"]
            # ⭐ BEFORE READING ANY CACHE: if the game was reset in another container (or by the CLI tool
            #    writing the same marker), clear this process's memory first. `Clock._acc` is forward-only,
            #    so a zeroed table cannot correct it — nothing else in the request path would ever notice.
            honour_remote_reset()
            clock = clock_for(sid)
            # ⛔ THREE STATES, NOT TWO. A failed progress read returns {} — which is indistinguishable
            #    from a brand-new player who has solved nothing. Read it, and if the warehouse refuses,
            #    say SO rather than rendering "0 of 20" over somebody's finished week. Measured: an
            #    expired token surfaced as an opaque 500 on the main page, which is the other bad
            #    direction — a dead screen where a named warning would do.
            state, state_ok, state_why = {}, True, None
            # ⛔ ABSENCE IS A THIRD STATE, AND THIS FLAG COULD NOT SEE IT. `state_ok` was only ever set
            #    False inside an `except` around the progress read — so when there was NO STORE AT ALL the
            #    try block never ran, and the payload said `state_ok: true, total_solved: 0`. That is
            #    exactly the instrument that should have shouted when both deployed apps ran for hours with
            #    no environment, and instead it agreed with them. A missing store is now the loudest case.
            if not (STORE and STATES):
                state_ok = False
                state_why = "no log table is configured, so nothing can be read or recorded"
            if STATES and v["user_key"]:
                try:
                    state = STATES.get(v["user_key"], sid)
                except Exception as e:
                    state, state_ok = {}, False
                    state_why = f"{type(e).__name__}: {e}"[:200]
                    print("progress read failed:", state_why, file=sys.stderr)
            if STORE and v["user_key"] and state_ok:
                try:
                    clock.load(v["user_key"])
                except Exception as e:
                    state_ok = False
                    state_why = f"{type(e).__name__}: {e}"[:200]
                    print("clock read failed:", state_why, file=sys.stderr)
            total = season.total_weeks
            rel = released_weeks(total)
            weeks = [game.week_payload(season, w, state, clock, v["user_key"], w in rel)
                     for w in sorted(season.cases)]
            # Which week the player lands on: the one they asked for, else the furthest one they have
            # opened and not finished, else the last one they finished, else the first released week.
            want = qs.get("week")
            active = None
            if want:
                try:
                    active = int(want[0])
                except (TypeError, ValueError):
                    active = None
            # ⛔ VALIDATE THE REQUESTED WEEK AGAINST THE RELEASED LIST, NOT AGAINST THE PACK. It used to be
            #    checked only for existence, so `?week=4` served an unreleased week's entire memo — story,
            #    every blank, every hint — while the same payload dutifully reported `status: "locked"`.
            #    The admin's staged release was therefore cosmetic: anything released later was already
            #    readable. A player asking for a week they may not have gets the week they should be on.
            if active is not None and active not in rel:
                active = None
            if active not in [w["week"] for w in weeks]:
                openish = [w["week"] for w in weeks if w["status"] == "open"]
                donish = [w["week"] for w in weeks if w["status"] == "done"]
                avail = [w["week"] for w in weeks if w["status"] == "available"]
                active = (openish[0] if openish else avail[0] if avail
                          else donish[-1] if donish else weeks[0]["week"])
            case = season.cases.get(active)
            payload = {
                "ok": True,
                "viewer": {k: v[k] for k in ("name", "first_name", "email", "user_key")},
                "season": {**season.meta(), "total_weeks": total, "released": rel},
                "config": CFG.public(), "theme": theme_payload(CFG.theme),
                "alt_theme_name": CFG.alt_theme,
                "weeks": weeks, "active_week": active,
                "tick_every_s": game.TICK_EVERY_S,
                "greeting_question": GREETING_Q,
                # `hint_cost` joins the other three here for the Instructions section states
                # the hint price in words, and a number typed into copy is a number that can disagree
                # with what is charged. Same rule the Hint BUTTON already follows — see game.HINT_PENALTY.
                "scoring": {"base": game.POINTS_BASE, "per_min": game.DECAY_PER_MIN,
                            "floor": game.POINTS_FLOOR, "hint_cost": game.HINT_PENALTY},
                # "" when unset, and the client renders nothing at all rather than a gap.
                "client_message": client_message(),
                # `is_operator` is ELIGIBILITY, unchanged from before , so the nav button still
                # appears for an operator who has not unlocked yet — see operator_eligible.
                "is_operator": operator_eligible(v),
                "operator_unlocked": operator_gate(v, self),
                "operator_password_required": CFG.operator_password_set,
                "operator_locked_reason": (None if operator_gate(v, self)
                                           else (CFG.unlock_hint() or "password required")),
                "total_points": sum(w["points"] for w in weeks),
                "total_solved": sum(w["solved"] for w in weeks),
                "total_blanks": sum(w["of"] for w in weeks),
                # `state_ok: false` means the numbers above are NOT this player's progress — they are what
                # is left when the read failed. The UI must say that rather than show them as a score.
                "state_ok": state_ok,
                "state_detail": state_why,
                "state_warning": None if state_ok else (
                    "This deployment has no activity table configured, so nothing you do here is being "
                    "saved — no points, no progress, no clock. Tell whoever deployed it."
                    if not (STORE and STATES) else
                    "Your progress could not be read just now, so what is shown below may be incomplete. "
                    "Nothing has been lost — reload in a moment."),
            }
            if case:
                payload["case"] = {
                    "case_id": case["case_id"], "week": case["week"], "title": case["title"],
                    "challenge": case.get("challenge"), "story": case.get("story"),
                    "scene": case.get("scene"),
                    "letter": game.letter_for_client(case, state),
                }
            return self._send(payload)

        if path == "/api/theme":
            return self._send(theme_payload((qs.get("name") or [CFG.alt_theme])[0]))

        if path == "/api/leaderboard":
            if not STORE:
                return self._err("the roster is not configured", 503)
            season_now = active_season(qs)
            sid = (season_now.pack["season_id"] if season_now else "") or ""
            total = season_now.total_weeks if season_now else 4
            # There is no calendar week to be "this week", so the board is scoped to a week the CALLER
            # names — the one they are looking at — and defaults to the furthest released week.
            try:
                week = int((qs.get("week") or [0])[0])
            except (TypeError, ValueError):
                week = 0
            rel = released_weeks(total)
            if not 1 <= week <= total:
                week = max(rel) if rel else 1
            sp = {"name": "season_id", "type": "STRING", "value": sid}
            wp = {"name": "week_index", "type": "INT", "value": str(week)}
            allt = STORE.query(f"lb:all:{sid}", queries.leaderboard(LOG_TABLE, "all"), [sp])
            wk = STORE.query(f"lb:w{week}:{sid}", queries.leaderboard(LOG_TABLE, "week"), [wp, sp])
            firsts = STORE.query(f"first:{week}:{sid}", queries.first_blood(LOG_TABLE), [wp, sp])
            # ── TOTAL PLAYERS PER FILTER ────────────────────────────────────────
            # Both scopes on one request, because the client shows one board and needs the count for
            # whichever tab is active without a second round trip per tab.
            cnt_all = STORE.query(f"pc:all:{sid}", queries.filter_player_counts(LOG_TABLE, "all"),
                                  [sp], ttl=10)
            cnt_wk = STORE.query(f"pc:w{week}:{sid}", queries.filter_player_counts(LOG_TABLE, "week"),
                                 [wp, sp], ttl=10)

            def _counts(rows):
                r = (rows[0] if rows else {}) or {}
                out = {}
                for k in ("players_opened", "players_attempted", "players_scored", "players_asked"):
                    try:
                        out[k] = int(r.get(k) or 0)
                    except (TypeError, ValueError):
                        out[k] = 0
                return out
            labels = {cl["clue_id"]: cl.get("label")
                      for c in (season_now.pack["cases"] if season_now else []) for cl in c["clues"]}
            for f in firsts:
                f["label"] = labels.get(f.get("clue_id"), f.get("clue_id"))
            return self._send({"ok": True, "week": week, "all_time": allt, "this_week": wk,
                               "weeks": [{"week": c["week"], "title": c["title"]}
                                         for c in (season_now.pack["cases"] if season_now else [])],
                               "first_blood": firsts, "you": v["user_key"],
                               # ⭐ THREE COUNTS PER FILTER, NOT ONE, with the denominator in the NAME.
                               #    "participated" is ambiguous and whichever single number shipped would
                               #    be read as one of the others. `players_attempted` is the one I would
                               #    put on the board; see queries.filter_player_counts for why.
                               "player_counts": {"all_time": _counts(cnt_all),
                                                 "this_week": _counts(cnt_wk)},
                               "player_counts_denominators": {
                                   "players_opened": "distinct players with ANY row in scope (for a week: "
                                                     "unlocked it, even if they typed nothing)",
                                   "players_attempted": "distinct players who SUBMITTED an answer in scope, "
                                                        "right or wrong — the closest match to "
                                                        "'participated'",
                                   "players_scored": "distinct players with >=1 CORRECT answer in scope",
                                   "players_asked": "distinct players who asked Genie >=1 question in scope",
                                   "identity": "email, falling back to the workspace user id when absent",
                               }})

        # ── The five figures the client renders above the leaderboard.
        #    Open to every player, not operator-gated: these are five aggregates with no answer key and no
        #    per-person detail in them, and the board they sit above is already public. Gating them would
        #    have made the strip render empty for exactly the people it is for.
        #
        #    ⭐ EVERY AVERAGE IS RETURNED WITH ITS DENOMINATOR NAMED, in the payload itself rather than in
        #    a comment only this file can see. `denominators` is what a reader of the JSON uses to tell
        #    `avg_score` (over everyone) from `avg_score_active` (over people who actually scored) — the
        #    two differ by a lot on a young deployment and whichever ships alone gets quoted as the other.
        if path == "/api/leaderboard/stats":
            if not STORE:
                return self._err("the roster is not configured", 503)
            rows = STORE.query("lb:stats", queries.leaderboard_stats(LOG_TABLE), ttl=10)
            r = (rows[0] if rows else {}) or {}

            def _i(name):
                try:
                    return int(r.get(name) or 0)
                except (TypeError, ValueError):
                    return 0

            players, scorers, askers = _i("players"), _i("scorers"), _i("askers")
            total_q, total_pts, total_ok = _i("total_questions"), _i("total_points"), _i("total_correct")

            def _avg(num, den):
                # Nobody has played yet is NOT an average of zero — zero is a real value a reader would
                # take as "they play and score nothing". None renders as the empty state the client was told
                # to draw, which is the honest shape for "no denominator".
                return round(num / den, 1) if den else None

            return self._send({
                "ok": True,
                # The five that were asked for, in that order.
                "players": players,
                "total_questions": total_q,
                "avg_score": _avg(total_pts, players),
                "avg_questions_per_player": _avg(total_q, players),
                "avg_correct_per_player": _avg(total_ok, players),
                # The same three over active players only, so neither can be mistaken for the other.
                "avg_score_active": _avg(total_pts, scorers),
                "avg_questions_per_asker": _avg(total_q, askers),
                "avg_correct_per_scorer": _avg(total_ok, scorers),
                # Raw numerators, so the strip can be re-derived and checked without a second query.
                "total_points": total_pts, "total_correct": total_ok,
                "scorers": scorers, "askers": askers,
                "denominators": {
                    "avg_score": "players", "avg_questions_per_player": "players",
                    "avg_correct_per_player": "players",
                    "avg_score_active": "scorers (players with >=1 correct answer)",
                    "avg_questions_per_asker": "askers (players with >=1 question)",
                    "avg_correct_per_scorer": "scorers (players with >=1 correct answer)",
                    "players": "DISTINCT user_key over all non-admin rows",
                },
            })

        if path == "/api/stats":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            if not STORE:
                return self._err("no log table configured", 503)
            head = STORE.query("stats:head", queries.operator_stats(LOG_TABLE), ttl=10)
            byweek = STORE.query("stats:week", queries.operator_by_week(LOG_TABLE), ttl=10)
            byseason = STORE.query("stats:season", queries.operator_by_season(LOG_TABLE), ttl=10)
            split = STORE.query("stats:split", queries.operator_demo_split(LOG_TABLE), ttl=10)
            daily = STORE.query("stats:daily", queries.operator_daily(LOG_TABLE), ttl=10)
            repeats = STORE.query("stats:repeats", queries.repeat_wrong_values(LOG_TABLE), ttl=30)
            # The reset's own marker, which survives the wipe it records — so "where did the scores go?"
            # has an answer on the page rather than in a container log nobody can read.
            reset = STORE.query("stats:reset", queries.last_reset(LOG_TABLE), ttl=5)
            h = head[0] if head else {}
            def num(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return 0.0
            users, asked = num(h.get("unique_users")), num(h.get("activated_users"))
            derived = {
                "activation_rate_pct": round(asked / users * 100, 1) if users else 0.0,
                "questions_per_user": round(num(h.get("total_questions")) / users, 2) if users else 0.0,
                "questions_per_activated_user": round(num(h.get("total_questions")) / asked, 2) if asked else 0.0,
                "sessions_per_user": round(num(h.get("total_sessions")) / users, 2) if users else 0.0,
                "questions_per_session": round(num(h.get("total_questions")) / num(h.get("total_sessions")), 2)
                                          if num(h.get("total_sessions")) else 0.0,
            }
            return self._send({"ok": True, "headline": h, "derived": derived, "by_week": byweek,
                               "by_season": byseason, "calendar_split": split,
                               "daily": daily, "repeat_rejected_values": repeats,
                               # `table` is what the SQL actually runs against, so it is LOG_TABLE and
                               # not CFG.log_table — in local mode the latter is empty and the operator
                               # page would have shown a blank where the target belongs.
                               "log_writer": STORE.healthy(), "table": LOG_TABLE,
                               # What the reset button needs to render honestly: when it last happened, who
                               # pressed it, and the confirm word the endpoint will demand — so the page
                               # cannot hard-code a phrase the server might change.
                               "last_reset": (reset[0] if reset else {}),
                               "reset_confirm_word": RESET_CONFIRM,
                               "reset_keeps": list(queries.META_EVENT_TYPES),
                               "storage": CFG.storage_public(),
                               "settings": (SETTINGS.current() if SETTINGS else {}),
                               "settings_history": (SETTINGS.history() if SETTINGS else [])})

        if path == "/api/logs":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            if not STORE:
                return self._err("no log table configured", 503)
            page = max(int((qs.get("page") or ["1"])[0]), 1)
            size = min(max(int((qs.get("size") or ["50"])[0]), 1), 500)
            total = STORE.query("logs:count", queries.raw_count(LOG_TABLE), ttl=10)
            rows = STORE.query(f"logs:{page}:{size}", queries.raw_page(LOG_TABLE),
                               [{"name": "lim", "type": "INT", "value": str(size)},
                                {"name": "off", "type": "INT", "value": str((page - 1) * size)}], ttl=5)
            n = int((total[0]["n"] if total else 0) or 0)
            return self._send({"ok": True, "page": page, "size": size, "total": n,
                               "pages": max((n + size - 1) // size, 1), "rows": rows})

        if path == "/api/export.csv":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            if not STORE:
                return self._err("no log table configured", 503)
            lim = min(max(int((qs.get("limit") or ["50000"])[0]), 1), 200000)
            rows = STORE.query(f"export:{lim}", queries.export_all(LOG_TABLE),
                               [{"name": "lim", "type": "INT", "value": str(lim)}], ttl=0)
            buf = io.StringIO()
            cols = list(rows[0].keys()) if rows else ["event_ts"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            return self._send(buf.getvalue().encode(), 200, "text/csv; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="night-desk-log-{stamp}.csv"'})

        # ── everything the two new Operator cards need to render, in one call ────────────────────
        if path == "/api/operator/config":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            return self._send(operator_config())

        if path == "/api/settings":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            season = active_season(qs)
            total = season.total_weeks if season else 4
            return self._send({
                "ok": True, "current": (SETTINGS.current(force=True) if SETTINGS else {}),
                "history": (SETTINGS.history() if SETTINGS else []),
                "released": released_weeks(total),
                "default_released": list(DEFAULT_RELEASED),
                "seasons": [{"season_id": k, "title": s2.pack.get("title"),
                             "subtitle": s2.pack.get("subtitle"), "weeks": s2.total_weeks,
                             "requires_tables": s2.requires_tables}
                            for k, s2 in sorted(SEASONS.items())],
                "season_id": season.pack["season_id"] if season else None,
                "weeks": [{"week": c["week"], "title": c["title"],
                           "challenge": c.get("challenge")}
                          for c in (season.pack["cases"] if season else [])],
            })

        if path == "/api/obo":
            tok = v["token"] or ""
            claims = {}
            if tok:
                import base64
                try:
                    p = tok.split(".")[1]
                    p += "=" * (-len(p) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(p))
                except Exception:
                    claims = {}
            return self._send({"present": bool(tok), "scope": claims.get("scope"),
                               "expires_in_s": (claims.get("exp", 0) - int(time.time()))
                                                if claims.get("exp") else None,
                               "subject": claims.get("sub"), "viewer": v["email"]})

        return self._err("not found", 404)

    # -- POST api ---------------------------------------------------------------
    def _api_post(self, path, body, qs):
        v = viewer(self)
        if not v["token"]:
            return self._err("no viewer token on this request — open the app from the workspace", 401)
        season = active_season(qs)
        sid = (season.pack["season_id"] if season else "") or ""
        # There is no calendar rotation any more — an admin releases weeks and a player unlocks them —
        # so `real_week_index` records the week the request is actually about rather than a date's idea
        # of it. Keeping the column filled means the operator queries that read it still work.
        try:
            real_wk = int(body.get("week")) if body.get("week") else None
        except (TypeError, ValueError):
            real_wk = None
        session_id = (body.get("session_id") or "").strip() or str(uuid.uuid4())

        # ── POINT THE APP AT A GENIE SPACE, AND PROVE IT WORKS ───────────────────────────
        # THE REQUIREMENT: after a manual install the operator points at the right Genie space from this
        # page, and entering it quickly tests that it is working.
        #
        # ⭐ THE TEST ASKS A REAL QUESTION, and it has to. Space resolution is lazy and BY TITLE, so
        #    everything short of a question — the space existing, /api/health being green, an id resolving —
        #    is satisfied by a space that answers nothing useful. So this asks week 1's first blank, as the
        #    OPERATOR (the viewer path players use), and compares the answer with the authored value
        #    server-side. It reports whether it matched; it does NOT send the expected value to the browser,
        #    because that is the answer key and no payload in this app carries one.
        if path == "/api/operator/genie-space":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            clear = bool(body.get("clear"))
            raw = (body.get("value") or "").strip()
            if not clear and not raw:
                return self._err("type the title of your Genie space, or paste its id or its URL")
            want_id, want_title = "", ""
            if not clear:
                want_id, want_title = genie.parse_space_ref(raw)
            out = {"ok": True, "cleared": clear, "space_id": want_id or None,
                   "space_title": want_title or None}
            if not clear:
                # Resolve first: a title nobody created must not be saved, or the next restart comes up
                # pointing at nothing and the failure lands on a player instead of on the person typing.
                sid_res, err = (want_id, None) if want_id else \
                    genie.resolve_space(CFG.host, v["token"], want_title)
                if not sid_res:
                    return self._send({"ok": False, "stage": "resolve", "error": err,
                                       "spaces": genie.list_space_titles(CFG.host, v["token"])}, 200)
                out["space_id"] = sid_res
                if body.get("test", True):
                    probe = season.cases.get(1, {}).get("clues", [{}])[0] if season else {}
                    q = probe.get("ask") or GREETING_Q
                    ans = genie.ask(CFG.host, v["token"], sid_res, q, viewer_user_id=v["user_key"],
                                    budget=150)
                    texts = [checker.drop_repeated_sentences(checker.strip_markdown(a["text"]))
                             for a in ans.get("attachments", []) if a.get("text")]
                    reply = (texts[0] if texts else "")[:600]
                    out["test"] = {
                        "asked": q, "ok": bool(ans.get("ok")), "answer": reply,
                        "elapsed_s": ans.get("elapsed_s"), "error": ans.get("error"),
                        "attributed_to_viewer": ans.get("attributed_to_viewer"),
                        "rows": (ans.get("attachments") or [{}])[0].get("rows", [])[:3],
                    }
                    if ans.get("ok") and probe.get("expected") is not None:
                        # The same two calls every offline validator in tools/ makes, in the same order:
                        # candidates out of the ROWS first, prose last. `matches_expected` is a boolean —
                        # the expected value itself never leaves the server.
                        cands = checker.extract_candidates(
                            {"attachments": ans.get("attachments", [])})
                        hit = next((c for c in cands
                                    if checker.check(probe, c).get("verdict") == "correct"), None)
                        out["test"]["matches_expected"] = hit is not None
                        out["test"]["candidates_seen"] = len(cands)
                    if not ans.get("ok"):
                        out["ok"] = False
                        out["error"] = ("The space resolved but it did not answer: "
                                        + (ans.get("error") or "no answer"))
                        return self._send(out)
            if body.get("save", True):
                ok, why = DURABLE.write({"genie_space_title": (None if clear else (want_title or None)),
                                         "genie_space_id": (None if clear else (want_id or None))}, v)
                out["saved"] = ok
                out["save_error"] = why
                if not ok:
                    out["ok"] = False
                    # ⛔ APPLIED ANYWAY, and said so. Refusing to use a working space because the note
                    #    could not be filed would trade a durable problem for an immediate one; the page
                    #    tells the operator it will not survive a restart and names the path that failed.
                    out["warning"] = ("This space is now in use, but the setting could NOT be stored, so "
                                      "it will be lost when the app restarts: " + (why or ""))
                doc, _, _ = DURABLE.read(force=True)
                CFG.sources["genie_space"] = "env"          # re-derive from scratch
                CFG.genie_space_id, CFG.genie_space_title = "", os.environ.get(
                    "GENIE_SPACE_TITLE") or CFG.genie_space_title
                if clear:
                    CFG.apply_overrides({})
                else:
                    CFG.apply_overrides({"genie_space_title": want_title or None,
                                         "genie_space_id": want_id or None})
                _SPACE["by_title"].clear()                  # the cache is keyed by title; the title moved
                _SPACE["id"], _SPACE["title"], _SPACE["error"] = (out["space_id"], want_title or None,
                                                                  None)
            out["config"] = operator_config()
            return self._send(out)

        # ── POINT THE APP AT A UNITY CATALOG TABLE (persistent storage) ──────────────────
        # THE REQUIREMENT: for persistent storage the operator enters a catalog.schema.table here; entering
        # it checks that the table is accessible, and a table that already holds data is picked up as it
        # stands rather than replaced.
        if path == "/api/operator/storage":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            action = (body.get("action") or "check").strip().lower()
            if action not in ("check", "save", "clear"):
                return self._err("action must be check, save or clear")
            if action == "clear":
                ok, why = DURABLE.write({"log_table_fqn": None}, v)
                CFG.log_table = CFG.log_table_env
                CFG.sources["log_table"] = "env" if CFG.log_table_env else "unset"
                CFG.storage_mode = CFG.storage_mode_declared
                rec = rebind_store(reason="operator cleared the table", carry_rows=False)
                return self._send({"ok": bool(ok), "saved": ok, "save_error": why, "switch": rec,
                                   "config": operator_config()})
            if CFG.log_table_env:
                # The environment pinned it; see Config.apply_overrides for why that wins. Refusing loudly
                # is the point — a box that accepts a value the app will not use is worse than a disabled one.
                return self._err(
                    f"This deployment's table comes from its own configuration "
                    f"(LOG_TABLE_FQN={CFG.log_table_env}), so it cannot be changed from here — that is "
                    f"either a value in app.yaml or the `log-table` app resource app.yaml reads it from. "
                    f"Change it wherever it is set and redeploy, or remove it to hand the choice to this "
                    f"page.", 409)
            fqn = (body.get("table") or "").strip()
            cat, sch, tbl = tablecheck.split_fqn(fqn)
            if cat is None:
                return self._err(tbl)
            fqn = f"{cat}.{sch}.{tbl}"
            warehouse = (body.get("warehouse_id") or CFG.warehouse_id or "").strip()
            chk = tablecheck.Checker(CFG.host, warehouse, _sp_token, build=CFG.build).check(fqn)
            out = {"ok": bool(chk.get("ok")), "check": chk, "table": fqn,
                   "instructions": storage_instructions(fqn)}
            if action == "save":
                if not chk.get("ok"):
                    out["ok"] = False
                    out["error"] = ("Not saved. " + (chk.get("message") or "the table is not usable") +
                                    " Fix that and check again — saving a table the app cannot write to "
                                    "would give you a game that scores nothing and says nothing.")
                    out["config"] = operator_config()
                    return self._send(out)
                ok, why = DURABLE.write({"log_table_fqn": fqn,
                                         "warehouse_id": (warehouse or None)
                                         if not CFG.warehouse_id_env else None}, v)
                out["saved"] = ok
                out["save_error"] = why
                if not ok:
                    # ⛔ REFUSED, unlike the Genie space, and the asymmetry is deliberate: a space that is
                    #    forgotten on restart costs a failed question, while a TABLE that is forgotten on
                    #    restart silently sends everyone back to an empty ephemeral game while their rows
                    #    sit in Delta. That is the worst failure this product has, so it is not entered
                    #    into on a promise that cannot be kept.
                    out["ok"] = False
                    out["error"] = ("NOT switched. The table is fine, but the pointer to it could not be "
                                    "stored, so a restart would leave this app back on its ephemeral local "
                                    "database with your rows stranded in Delta. " + (why or ""))
                    out["config"] = operator_config()
                    return self._send(out)
                CFG.log_table, CFG.sources["log_table"] = fqn, "operator"
                if warehouse and not CFG.warehouse_id:
                    CFG.warehouse_id, CFG.sources["warehouse_id"] = warehouse, "operator"
                CFG.storage_mode = "delta"
                out["switch"] = rebind_store(reason="operator set a table")
            out["config"] = operator_config()
            return self._send(out)

        if path == "/api/settings":
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            if not SETTINGS:
                return self._err("no log table configured, so there is nowhere to keep a setting", 503)
            total = season.total_weeks if season else 4
            raw = body.get("unlockable")
            # ⛔ TWO CONTROLS NOW WRITE THIS ONE ROW (release weeks, and 's client message), so
            #    an ABSENT field has to mean "leave it alone" rather than "clear it". It is not a
            #    convenience: every setting shares one row and the latest row wins, so a client-message
            #    save that sent no `unlockable` would have written it as empty — and released_weeks()
            #    reads empty as DEFAULT_RELEASED, silently CLOSING weeks 2-4 for everybody mid-game while
            #    returning ok:true. `Settings.set` carries omitted keys forward (see settings.KEEP); this
            #    branch only has to avoid inventing a value for one it was not given.
            if raw is None:
                if "client_message" not in body:
                    return self._err("send `unlockable` — the list of weeks players may unlock")
                weeks = None                      # not this request's business; KEEP it
            else:
                if isinstance(raw, str):
                    raw = [x for x in raw.replace(",", " ").split() if x]
                weeks = []
                for x in raw:
                    try:
                        w = int(x)
                    except (TypeError, ValueError):
                        return self._err(f"not a week number: {x!r}")
                    if not 1 <= w <= total:
                        return self._err(f"weeks run 1 to {total}; got {w}")
                    if w not in weeks:
                        weeks.append(w)
            want_season = (body.get("season_id") or "").strip() or None
            if want_season and want_season not in SEASONS:
                return self._err(f"no such scenario: {want_season}")
            # An explicitly empty string CLEARS the line (that is how an operator removes it); a missing
            # field leaves it as it was. That is why this is a membership test and not `or ""`.
            if "client_message" in body:
                msg = body.get("client_message")
                msg = (msg if isinstance(msg, str) else "").strip()[:CLIENT_MESSAGE_MAX] or None
            else:
                msg = SETTING_KEEP
            cur = SETTINGS.set(v, season_id=want_season, client_message=msg,
                               unlockable=(sorted(weeks) if weeks is not None else SETTING_KEEP),
                               note=((body.get("note") or "").strip()[:200] or None
                                     if "note" in body else SETTING_KEEP),
                               real_week=(max(weeks) if weeks else None),
                               row_season_id=want_season or sid)
            STORE.invalidate("")
            return self._send({"ok": True, "current": cur,
                               "released": released_weeks(total),
                               "history": SETTINGS.history()})

        if path in ("/api/operator/reset/prepare", "/api/operator/reset"):
            # ⛔ THE MOST DESTRUCTIVE THING IN THE APP, reachable by anyone holding the operator password.
            #    Four protections, each for a stated reason:
            #      1. a VERIFIED BACKUP FIRST — prepare builds the CSV, checks it with the importer's own
            #         parser, and only then mints a token; reset refuses without that token;
            #      2. an explicit CONFIRM string, because "everyone's stats" is not an undoable misclick;
            #      3. the WEEK RELEASES SURVIVE — only non-META event types are deleted, so pressing a
            #         button labelled "reset stats" cannot cost them weeks 1-4;
            #      4. it LOGS ITS OWN USE as `admin_reset`, which is a META type and therefore survives the
            #         wipe it records. A reset with no trace is indistinguishable from data loss.
            if not operator_gate(v, self):
                return self._err("not for you", 403)
            if not (STORE and STATES):
                return self._err("no activity table is configured, so there is nothing to reset", 503)

            if path == "/api/operator/reset/prepare":
                # The backup, built and VERIFIED here rather than trusted. `export_all` is the projection the
                # importer reads, so what comes back is restorable by /api/import — the same round trip
                rows = STORE.query("reset:backup", queries.export_all(LOG_TABLE),
                                   [{"name": "lim", "type": "INT", "value": "200000"}], ttl=0)
                buf = io.StringIO()
                cols = list(rows[0].keys()) if rows else []
                if cols:
                    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    for r in rows:
                        w.writerow(r)
                text = buf.getvalue()
                plan, why = None, None
                if not rows:
                    why = "the table is already empty — there is nothing to back up or reset"
                else:
                    try:
                        plan = importer.parse(text.encode(), "reset-backup.csv")
                    except Exception as e:
                        why = f"the backup did not parse ({type(e).__name__}) — refusing to reset"
                    else:
                        if len(plan["rows"]) != len(rows):
                            why = (f"the backup parsed {len(plan['rows'])} of {len(rows)} rows — refusing "
                                   f"to reset on a partial backup")
                if why:
                    # ⛔ NO TOKEN ON A DOUBTFUL BACKUP. The failure direction is "you cannot reset", never
                    #    "reset without a backup".
                    return self._send({"ok": False, "error": why, "rows": len(rows),
                                       "can_reset": False}, 200)
                token = secrets.token_urlsafe(24)
                _RESET_TOKENS[token] = {"user_key": v["user_key"], "at": time.time(),
                                        "rows": len(rows)}
                counts = {}
                for r in rows:
                    counts[r.get("event_type")] = counts.get(r.get("event_type"), 0) + 1
                keep = sum(n for t, n in counts.items() if t in queries.META_EVENT_TYPES)
                return self._send({
                    "ok": True, "can_reset": True, "token": token,
                    "rows": len(rows), "bytes": len(text.encode()),
                    "will_delete": len(rows) - keep, "will_keep": keep,
                    "kept_types": list(queries.META_EVENT_TYPES),
                    "by_event_type": counts,
                    "ordering": plan["ordering"],
                    "released_now": released_weeks(season.total_weeks if season else 4),
                    "backup_csv": text,
                    "confirm_word": RESET_CONFIRM,
                    "note": ("Download the backup before confirming. The week releases are kept; every "
                             "score, answer, question, clock and hint is deleted for everyone.")})

            # ── the reset itself
            token = (body.get("token") or "").strip()
            held = _RESET_TOKENS.get(token)
            if not held or held["user_key"] != v["user_key"]:
                return self._err("take a verified backup first (POST /api/operator/reset/prepare)", 409,
                                 needs_backup=True)
            if time.time() - held["at"] > RESET_TOKEN_TTL_S:
                _RESET_TOKENS.pop(token, None)
                return self._err("that backup is stale — take a fresh one", 409, needs_backup=True)
            if (body.get("confirm") or "") != RESET_CONFIRM:
                return self._err(f"send confirm=\"{RESET_CONFIRM}\" — this deletes everyone's progress",
                                 400, needs_confirm=True)
            _RESET_TOKENS.pop(token, None)
            # The marker goes in BEFORE the delete and is FLUSHED, so it is already a row when the purge
            # runs — and it survives, because META types are what the purge keeps.
            STORE.log("admin_reset", season_id=sid, user_key=v["user_key"], user_email=v["email"],
                      user_display_name=v["name"], real_week_index=real_wk,
                      extra={"rows_before": held["rows"], "via": "operator page"})
            STORE.flush()
            deleted, kept = STORE.purge(queries.META_EVENT_TYPES)
            cleared = clear_in_memory_state()
            _RESET_SEEN["at"] = None          # re-read the marker; do not clear twice for our own reset
            honour_remote_reset()
            rel = released_weeks(season.total_weeks if season else 4)
            print(f"[reset] {v['email']} reset the game: {deleted} rows deleted, {kept} kept, "
                  f"releases {rel}", flush=True)
            return self._send({"ok": True, "deleted": deleted, "kept": kept,
                               "kept_types": list(queries.META_EVENT_TYPES),
                               "released": rel, "cleared": cleared,
                               "message": (f"Reset. {deleted} rows deleted, {kept} kept. Weeks {rel} are "
                                           f"still released. Every score, clock and hint is gone for "
                                           f"everyone.")})

        if path == "/api/session":
            if STORE:
                STORE.log("session_start", season_id=sid, user_key=v["user_key"],
                          user_email=v["email"], user_display_name=v["name"],
                          session_id=session_id, week_index=real_wk, real_week_index=real_wk)
            return self._send({"ok": True, "session_id": session_id})

        if path == "/api/chat":
            # ⛔ THE GREETING IS NOT A QUESTION THE PLAYER ASKED, AND IT MUST NEVER BE COUNTED AS ONE.
            #    A question is fired on every app open, uncached, so the animations run and so
            #    we can see Genie is alive. Every activation query in queries.py keys on
            #    `event_type = 'question_asked'` — ten of them — so logging this under that type would mark
            #    EVERY visitor as activated and turn "unique users who asked at least one question" into
            #    "unique users". That is the one number this whole product exists to move.
            #    So a greeting logs as `question_greeting`: the latency and the outcome are on the record
            #    as a health signal, and the adoption metric does not move.
            greeting = bool(body.get("greeting"))
            ask_event = "question_greeting" if greeting else "question_asked"
            fail_event = "greeting_failed" if greeting else "question_failed"
            # A real conversation, not a one-shot: `conversation_id` is threaded back so follow-ups land
            # in the same Genie conversation, and "New chat" is simply the client dropping it. That is
            # what makes this feel like talking to a Genie space rather than to a search box.
            question = (body.get("question") or "").strip()
            if not (3 <= len(question) <= 500):
                return self._err("ask something between 3 and 500 characters")
            wk = int(body.get("week") or 1)
            case = season.cases.get(wk) if season else None
            space, err = space_id(v, season)
            if not space:
                return self._err(err or "the genie is unreachable", 503, archive=True)
            t0 = time.time()
            # A shorter budget for the greeting: it is a health check on the page's first second, and a
            # lamp that thinks forever is the worst outcome — it cannot be told apart from "slow", which
            # silently converts the health check into nothing. 60s, then a visible failure.
            # MEASURED, and the obvious explanation was wrong. On the deployed app the greeting round trip
            # is ~8.7s while Genie's own share is ~4.5s. I assumed the 4s gap was our 2s poll granularity
            # and tried a 0.8s poll: median went 8.1s -> 8.7s, i.e. no improvement at all, so the gap is
            # transport (the Apps proxy plus a fresh TLS connection per call), not our polling. The faster
            # poll is therefore NOT kept — it only multiplies API calls at scale for nothing. What IS kept
            # is the shorter budget, because that is what bounds the visible failure.
            ans = genie.ask(CFG.host, v["token"], space, question, viewer_user_id=v["user_key"],
                            conversation_id=(body.get("conversation_id") or None),
                            budget=(60 if greeting else 210))
            latency = int((time.time() - t0) * 1000)
            if not ans.get("ok"):
                if STORE:
                    STORE.log(fail_event, season_id=sid, real_week_index=real_wk,
                              user_key=v["user_key"], user_email=v["email"],
                              user_display_name=v["name"], session_id=session_id, week_index=wk,
                              case_id=(case or {}).get("case_id"), question_text=question,
                              genie_space_id=space, genie_status=ans.get("status") or ans.get("stage"),
                              latency_ms=latency, extra={"error": ans.get("error")})
                return self._send({"ok": False, "error": ans.get("error") or "the genie went quiet",
                                   "expired": bool(ans.get("expired")),
                                   "elapsed_s": ans.get("elapsed_s")}, 200)
            texts = [checker.drop_repeated_sentences(checker.strip_markdown(a["text"]))
                     for a in ans["attachments"] if a.get("text")]
            sqls = [a["sql"] for a in ans["attachments"] if a.get("sql")]
            # The attribution guard, unchanged and load-bearing: a row is credited to a human only when
            # Genie's own record of the message names that human. Nothing in this app can ask Genie as
            # the service principal, and this is the check that would catch it if that ever changed.
            if STORE and ans.get("attributed_to_viewer"):
                STORE.log(ask_event, season_id=sid, real_week_index=real_wk,
                          user_key=v["user_key"], user_email=v["email"],
                          user_display_name=v["name"], session_id=session_id, week_index=wk,
                          case_id=(case or {}).get("case_id"), question_text=question,
                          genie_space_id=space, genie_conversation_id=ans.get("conversation_id"),
                          genie_message_id=ans.get("message_id"), genie_user_id=ans.get("genie_user_id"),
                          genie_status=ans.get("status"), genie_answer_text=(texts[0] if texts else None),
                          genie_sql=(sqls[0] if sqls else None), latency_ms=latency)
                # ⛔ NO INLINE FLUSH. `flush()` runs the INSERT on THIS thread, so every app open performed
                #    a synchronous single-row Delta commit — and an independent load test found the visible
                #    duration of a GENIE health check was dominated by it: 8.2s -> 11.6s -> 15.3s -> 26.0s
                #    at 5/20/40/120 simultaneous opens, while the app's view of Genie never left 4.5-4.8s
                #    and Genie's own clock said a flat 3.85s throughout. The control that located it: a
                #    /api/health with 200 concurrent was flat at 1.62s, so neither the HTTP layer nor the
                #    proxy was the limit. The lamp was slow because the log table was busy.
                #    The background writer flushes on its own 2s timer and batches up to 40 rows, which also
                #    turns 113 separate commits into a handful. The player's own view never waited on this
                #    anyway: note_asked and note_solved update memory synchronously.
                if STATES and case and not greeting:
                    STATES.note_asked(v["user_key"], case["case_id"])
                    STORE.invalidate("stats")
            elif STORE:
                STORE.log("question_unattributed", season_id=sid, real_week_index=real_wk,
                          user_key=v["user_key"], user_email=v["email"],
                          user_display_name=v["name"], session_id=session_id, week_index=wk,
                          case_id=(case or {}).get("case_id"), question_text=question,
                          genie_space_id=space, genie_user_id=ans.get("genie_user_id"),
                          latency_ms=latency,
                          extra={"why": "genie message user_id did not match the forwarded viewer"})
            return self._send({"ok": True, "answer": {
                "text": texts[0] if texts else None, "sql": sqls[0] if sqls else None,
                "tables": [{"schema": a.get("schema"), "rows": a.get("rows"),
                            "row_count": a.get("row_count")}
                           for a in ans["attachments"] if a.get("rows") is not None],
                "elapsed_s": ans.get("elapsed_s"),
                "conversation_id": ans.get("conversation_id"),
                "greeting": greeting,
                "attributed": bool(ans.get("attributed_to_viewer"))}})

        if path == "/api/unlock":
            # An admin RELEASES a week for everybody; each player then unlocks it for themselves, and
            # that click is what starts their own clock. Two different acts, deliberately.
            try:
                wk = int(body.get("week"))
            except (TypeError, ValueError):
                return self._err("which week?")
            if not season or wk not in season.cases:
                return self._err("no such week", 404)
            if wk not in released_weeks(season.total_weeks):
                return self._err("that week has not been released yet", 403, not_released=True)
            clock = clock_for(sid)
            clock.load(v["user_key"])
            fresh = clock.unlock(v, wk, case_id=season.cases[wk]["case_id"])
            state = STATES.get(v["user_key"], sid) if STATES else {}
            return self._send({"ok": True, "opened": fresh, "week": wk,
                               "weeks": [game.week_payload(season, w, state, clock, v["user_key"],
                                                           w in released_weeks(season.total_weeks))
                                         for w in sorted(season.cases)]})

        if path == "/api/tick":
            # The pausing clock. The browser sends this only while the tab is VISIBLE and this week is
            # the one on screen — so a closed laptop simply stops ticking and banks nothing.
            try:
                wk = int(body.get("week"))
            except (TypeError, ValueError):
                return self._err("which week?")
            clock = clock_for(sid)
            state = STATES.get(v["user_key"], sid) if STATES else {}
            case = season.cases.get(wk) if season else None
            # A de-released week's clock must not run either, or a player keeps burning their own score on
            # a week they cannot answer.
            if season and wk not in released_weeks(season.total_weeks):
                return self._send({"ok": True, "running": False, "not_released": True})
            done = bool(case) and all((state.get(cl["clue_id"]) or {}).get("solved")
                                      for cl in case["clues"])
            focus = clock.tick(v, wk, stopped=done)
            if focus is None:
                return self._send({"ok": True, "running": False})
            return self._send({"ok": True, "running": not done, "week": wk, "focus_s": int(focus),
                               "focus_label": game.fmt_focus(focus),
                               "worth_now": game.points_for_focus(focus),
                               "next_drop_s": game.minutes_until_next_drop(focus)})

        if path == "/api/hint":
            # ⭐ THE -5 IS AN EVENT THAT IS RECORDED, NOT A NUMBER HELD IN THE PAGE. THE REQUIREMENT:
            #    the hint button for a blank opens a pop-up revealing the hint and permanently adds -5 to
            #    that blank; once the blank is answered correctly the button is disabled, whether or not the
            #    hint was revealed first. A -5 kept in the browser is
            #    wiped by a refresh and the button comes back — so a `hint_used` row is written and every
            #    score is DERIVED from it (queries.HINT_PENALTY_SQL).
            # The revealed content is the clue's own QUESTION (`ask`), option A: "the question is
            # already very obvious and acts as a hint". So this endpoint authors no hint text and discloses
            # nothing the payload did not already carry — what the -5 prices is the reveal, not secrecy.
            clue_id = body.get("clue_id")
            case, clue = season.clue(clue_id) if season else (None, None)
            if not clue:
                return self._err("no such blank", 404)
            wk = case["week"]
            clock = clock_for(sid)
            clock.load(v["user_key"])
            # Release first, then unlock — the same order and the same reason as /api/answer: a week an
            # operator has closed is closed, whatever this player did earlier.
            if wk not in released_weeks(season.total_weeks):
                return self._err("that week is not open at the moment — an operator has it closed.", 403,
                                 not_released=True)
            if not clock.is_unlocked(v["user_key"], wk):
                return self._err("unlock that week first", 403, needs_unlock=True)
            st_before = (STATES.get(v["user_key"], sid) if STATES else {}).get(clue_id) or {}
            if st_before.get("solved"):
                # ⛔ DISABLED HAS TO BE TRUE ON THE SERVER, NOT ONLY RENDERED. A disabled button is a
                #    drawing; a second tab, a stale page or a curl can still post this. Refusing here is
                #    what makes "no penalty once it is right" a fact rather than an appearance.
                return self._err("that blank is already answered — there is no hint to take.", 409,
                                 clue_id=clue_id, already_solved=True)
            # ⭐ IDEMPOTENT PER BLANK. A second click (or a second tab) reveals the same hint and charges
            #    NOTHING. This guard reads a per-container cache, so it is the courtesy; the guarantee is
            #    that the penalty is derived with a MAX rather than a SUM, which no duplicate row can fool.
            charged = not int(st_before.get("hint_penalty") or 0)
            if charged and STORE:
                # The row carries its own price as a NEGATIVE `points`, so a hint keeps the price it was
                # revealed at. `verdict` stays NULL: a hint is not an attempt, and every attempt-counting
                # aggregate keys on verdict. No inline flush — see the note in /api/chat; the player's own
                # score moves immediately from STATES below, and the board can be one writer-tick behind.
                STORE.log("hint_used", season_id=sid, real_week_index=real_wk, user_key=v["user_key"],
                          user_email=v["email"], user_display_name=v["name"], session_id=session_id,
                          week_index=wk, case_id=case["case_id"], clue_id=clue_id,
                          points=-game.HINT_PENALTY, latency_ms=0)
                STORE.invalidate("lb")
                STORE.invalidate("stats")
            if charged and STATES:
                STATES.note_hint(v["user_key"], clue_id, sid)
            state = STATES.get(v["user_key"], sid) if STATES else {}
            rel = released_weeks(season.total_weeks)
            weeks = [game.week_payload(season, w, state, clock, v["user_key"], w in rel)
                     for w in sorted(season.cases)]
            st_now = state.get(clue_id) or {}
            # ⛔ THE SAME SHAPE ON BOTH BRANCHES, first click and repeat, and it carries `weeks` + `letter`
            #    so one round trip re-renders every point display. A response that changes shape on a branch
            #    a real player reaches is what put "Cannot read properties of undefined" in the wrong-answer
            #    red once already.
            return self._send({
                "ok": True, "clue_id": clue_id, "hint": clue.get("ask"),
                "already": not charged, "hint_taken": True,
                "hint_cost": game.HINT_PENALTY,
                "hint_penalty": int(st_now.get("hint_penalty") or 0),
                "weeks": weeks, "letter": game.letter_for_client(case, state),
                "total_points": sum(w["points"] for w in weeks)})

        if path == "/api/answer":
            # One blank in the memo. Wrong costs nothing and stays editable; right locks and cannot be
            # edited again. There is no "ask first" gate: the values are not guessable, so the content
            # enforces asking without a rule that can fire on someone who did the work.
            clue_id = body.get("clue_id")
            submitted = body.get("value")
            case, clue = season.clue(clue_id) if season else (None, None)
            if not clue:
                return self._err("no such blank", 404)
            wk = case["week"]
            clock = clock_for(sid)
            clock.load(v["user_key"])
            # ⛔ RELEASE FIRST, THEN UNLOCK. Checking only the unlock meant an unlock that predated a
            #    de-release left the week wide open and scoring at full value, while the tracker card
            #    honestly said "not released yet". An operator could not take a week back. Release is the
            #    operator's gate and it has to be checked everywhere play happens, not only where a week
            #    is opened.
            if wk not in released_weeks(season.total_weeks):
                return self._err("that week is not open at the moment — an operator has it closed.", 403,
                                 not_released=True)
            if not clock.is_unlocked(v["user_key"], wk):
                return self._err("unlock that week first", 403, needs_unlock=True)
            wait = ATTEMPTS.blocked_for(v["user_key"], clue_id)
            if wait:
                # Not a penalty: no points are lost and every other blank is still open. It only bounds a
                # brute-force walk on THIS blank. Asking the genie is never throttled.
                return self._err(f"Take {wait}s on this one — that is {game.WRONG_BURST} wrong answers in a "
                                 f"row here. Nothing is lost and no points are deducted; ask the genie and "
                                 f"come back to it.", 429, cooldown_s=wait, clue_id=clue_id)
            already = (STATES.get(v["user_key"], sid) if STATES else {}).get(clue_id) or {}
            if already.get("solved"):
                # ⛔ THE SAME SHAPE AS EVERY OTHER ANSWER, because this is a NORMAL path a real player
                #    reaches — two tabs, or a phone and a laptop, and one of them is stale about one blank.
                #    It used to omit `weeks`, `letter` and `total_points`; the client dereferenced
                #    `r.weeks.reduce`, threw, and rendered "Cannot read properties of undefined" IN THE
                #    WRONG-ANSWER RED on an answer the server had just called correct — and the throw also
                #    froze that player's own running total. A response that is a different shape on a
                #    branch somebody actually reaches is a defect, not an optimisation.
                st_now = STATES.get(v["user_key"], sid) if STATES else {}
                rel_now = released_weeks(season.total_weeks)
                weeks_now = [game.week_payload(season, w2, st_now, clock, v["user_key"], w2 in rel_now)
                             for w2 in sorted(season.cases)]
                this_now = next((w2 for w2 in weeks_now if w2["week"] == wk), None)
                return self._send({
                    "ok": True, "verdict": "correct", "already": True,
                    "points": already.get("points", 0), "clue_id": clue_id,
                    "value": clue.get("display"), "message": "Already filled in.",
                    "trim": False, "weeks": weeks_now,
                    "letter": game.letter_for_client(case, st_now),
                    "week_done": bool(this_now and this_now["done"]),
                    "total_points": sum(w2["points"] for w2 in weeks_now)})
            result = checker.check(clue, submitted)
            focus_s = clock.focus_s(v["user_key"], wk)
            points = game.points_for_focus(focus_s, clue.get("points", game.POINTS_BASE)) \
                if result["verdict"] == "correct" else 0
            if result["verdict"] == "correct":
                clock.flush_week(v, wk)          # the figure the score came from goes on the record
            if STORE:
                STORE.log("answer_" + result["verdict"], season_id=sid, real_week_index=real_wk,
                          user_key=v["user_key"], user_email=v["email"],
                          user_display_name=v["name"], session_id=session_id, week_index=wk,
                          case_id=case["case_id"], clue_id=clue_id,
                          extracted_value=str(submitted)[:400],
                          expected_value=(str(clue.get("expected")) if result["verdict"] == "correct"
                                          else None),
                          verdict=result["verdict"], points=points, latency_ms=int(focus_s * 1000))
                if result["verdict"] == "correct":
                    # Not flushed inline — see the note in /api/chat. The player's own points come from
                    # STATES.note_solved below, which is immediate; the leaderboard is a shared aggregate
                    # and being one writer-tick behind costs nothing.
                    STORE.invalidate("lb")
                    STORE.invalidate("stats")
            if result["verdict"] == "correct":
                ATTEMPTS.note_solved(v["user_key"], clue_id)
            else:
                ATTEMPTS.note_wrong(v["user_key"], clue_id)
            if STATES:
                if result["verdict"] == "correct":
                    STATES.note_solved(v["user_key"], clue_id, points, sid)
                else:
                    STATES.note_tried(v["user_key"], clue_id, sid)
            state = STATES.get(v["user_key"], sid) if STATES else {}
            rel = released_weeks(season.total_weeks)
            weeks = [game.week_payload(season, w, state, clock, v["user_key"], w in rel)
                     for w in sorted(season.cases)]
            this = next((w for w in weeks if w["week"] == wk), None)
            # ⛔ WHAT THIS REPORTS IS THE NET FIGURE; THE ROW LOGGED THE GROSS ONE. `points` above is what
            #    the clock earned, and a hint penalty is a separate recorded event, so the two rows sum to
            #    what the player sees. Reporting the gross here would have flashed 100 at the moment of
            #    answering and settled to 95 when the state cache's TTL expired — a SELF-CORRECTING number,
            #    which is the worst kind: nothing ever looks broken and the score simply stops being
            #    believed. It is read from the SAME state entry the chevron renders, so the two cannot
            #    disagree, and test_r9_hint_penalty asserts net == logged_gross - logged_penalty.
            net_points = (int((state.get(clue_id) or {}).get("points") or 0)
                          if result["verdict"] == "correct" else 0)
            return self._send({
                "ok": True, "verdict": result["verdict"], "message": result["message"],
                "trim": bool(result.get("trim")), "points": net_points, "clue_id": clue_id,
                "value": clue.get("display") if result["verdict"] == "correct" else None,
                "weeks": weeks, "letter": game.letter_for_client(case, state),
                "week_done": bool(this and this["done"]),
                "total_points": sum(w["points"] for w in weeks)})

        return self._err("not found", 404)


def main():
    missing = CFG.missing()
    print(f"[night-desk] build={CFG.build} season={CFG.season_id} port={CFG.port} "
          f"host={CFG.host or '(unset)'} tz={weeklib.tz_mode(CFG.timezone)}", flush=True)
    if missing:
        print(f"[night-desk] WARNING missing config: {', '.join(missing)} — "
              f"the game will run but cannot log or score", flush=True)
    if not SEASON:
        print("[night-desk] FATAL no content pack found in app/content", flush=True)
    ThreadingHTTPServer(("0.0.0.0", CFG.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
