"""LOCAL DATABASE MODE — the same game, backed by a SQLite file instead of a Delta table.

WHY THIS EXISTS: a deployment must also be possible in a "local database" mode backed by SQLite
rather than a Delta table, so that nothing depends on being able to write to a table.
Some workspaces will not give an app a writable catalog, or not quickly. In those, the
Delta dependency is the thing that stops the game being played at all.

⭐ IT IS A DROP-IN FOR `LogStore`, NOT A FORK OF IT. Everything downstream — `PlayerStates`, `Settings`,
`Clock`, every operator aggregate — talks to a store through exactly five methods (`log`, `flush`,
`query`, `invalidate`, `healthy`) and reads `.table`, `.build` and `.stats`. So this class implements
those and NOTHING else changes. Delta mode keeps working untouched, which is the requirement: both
modes, chosen at deploy time.

⭐ AND IT REUSES `queries.py` VERBATIM rather than keeping a second copy of the SQL. A second copy is two
bodies of SQL that must be kept in agreement forever, and the one that is not exercised in your workspace
is the one that silently rots. Verified against SQLite 3.53: window functions, `COUNT(DISTINCT CASE WHEN
… END)`, named `:params` and backtick identifiers all behave as they do on Databricks, so the whole
dialect gap is TWO constructs, translated in `_translate` below and listed there.

## ⛔ THE ONE THING TO BE HONEST ABOUT: THE FILE IS AS EPHEMERAL AS THE CONTAINER

A Databricks Apps container has no persistent volume. In local mode the SQLite file lives on that
container's own disk, so **a redeploy or a restart loses it**. That is not a defect of this module, it is
the trade being made — no table dependency, in exchange for state that does not outlive the container.

It is also exactly why the CSV import exists. The CSV export is the backup and the importer is the restore, and
in this mode they are not a convenience but the whole durability story. `healthy()` therefore reports
`ephemeral: True` so the operator page can say so where a human reads it, rather than leaving someone to
discover it after a redeploy.

Point `LOCAL_DB_PATH` at a mounted volume if one is ever available and the problem goes away by itself.
"""
import json, os, re, sqlite3, sys, threading, time, uuid

from .logstore import COLUMNS, INT_COLS, BIGINT_COLS, MAX_TEXT

# `event_ts` is the warehouse clock in Delta mode and the app clock here. Kept as a separate column
# anyway, so an export from either mode has the same shape and the importer needs no special case.
ALL_COLUMNS = ["event_ts"] + list(COLUMNS)

DDL = """CREATE TABLE IF NOT EXISTS {t} (
  event_id              TEXT,
  event_ts              TEXT,
  received_ts           TEXT,
  event_type            TEXT,
  season_id             TEXT,
  user_key              TEXT,
  user_email            TEXT,
  user_display_name     TEXT,
  session_id            TEXT,
  week_index            INTEGER,
  real_week_index       INTEGER,
  case_id               TEXT,
  clue_id               TEXT,
  question_text         TEXT,
  genie_space_id        TEXT,
  genie_conversation_id TEXT,
  genie_message_id      TEXT,
  genie_user_id         TEXT,
  genie_status          TEXT,
  genie_answer_text     TEXT,
  genie_sql             TEXT,
  extracted_value       TEXT,
  expected_value        TEXT,
  verdict               TEXT,
  points                INTEGER,
  latency_ms            INTEGER,
  app_build             TEXT,
  extra                 TEXT
)"""

# ⛔ UNIQUE ON event_id IS WHAT MAKES THE IMPORT IDEMPOTENT. Without it, importing the same CSV twice
#    doubles `total_questions` — a raw SUM over question_asked rows, and the one number this product
#    exists to move. With it, a re-import is a no-op per row it has already seen, and the importer can
#    report "skipped" honestly instead of guessing.
INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS {t}_event_id ON {t}(event_id)",
    "CREATE INDEX IF NOT EXISTS {t}_user ON {t}(user_key, clue_id)",
    "CREATE INDEX IF NOT EXISTS {t}_type ON {t}(event_type)",
    "CREATE INDEX IF NOT EXISTS {t}_verdict ON {t}(verdict)",
]

# The entire dialect gap between this and Databricks SQL, for the queries in queries.py.
# Anything added here must be a construct, never a per-query patch: a table of exceptions keyed on
# individual statements is how the two SQL bodies drift apart again.
_SUBS = [
    (re.compile(r"\bto_date\s*\(", re.I), "date("),          # to_date(x)  -> date(x)
    (re.compile(r"\bcurrent_timestamp\s*\(\s*\)", re.I), "CURRENT_TIMESTAMP"),
]


def _translate(sql):
    for pat, rep in _SUBS:
        sql = pat.sub(rep, sql)
    return sql


def _now_ts():
    """UTC, to the millisecond, in a format that sorts lexicographically and parses as a timestamp.

    Matches how `LogStore` stamps `received_ts`, so a CSV from either mode is the same shape. Ordering in
    SQLite is a TEXT comparison, which is only correct because this format is zero-padded and fixed-width.
    """
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}"


class LocalStore:
    """SQLite-backed store with `LogStore`'s interface. See the module docstring for the seam."""

    def __init__(self, path, table="activity_log", build="dev", cache_ttl=20.0):
        self.path = path
        # The table name reaches SQL by interpolation, so it must not be attacker-shaped. It comes from a
        # deploy-time env var, but a bad one should fail loudly here rather than produce odd SQL later.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
            raise ValueError(f"LOCAL_DB_TABLE must be a bare identifier, got {table!r}")
        self.table = table
        self.build = build
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._lock = threading.Lock()          # guards the connection AND the read cache
        self.stats = {"queued": 0, "written": 0, "failed": 0, "flushes": 0, "last_error": None,
                      "last_write_ts": None, "dropped_rows": 0, "last_drop": None}
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False because the HTTP server is threaded; every access is serialised by
        # self._lock instead. WAL so a read during a write does not raise "database is locked".
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(DDL.format(t=self.table))
            for ix in INDEXES:
                self._db.execute(ix.format(t=self.table))
            self._db.commit()

    # ---------------------------------------------------------------- writing
    def log(self, event_type, **kw):
        """Written SYNCHRONOUSLY — no queue, no background thread, unlike Delta mode.

        Delta mode batches because each INSERT is a warehouse round trip of a second or more. A local
        SQLite INSERT is tens of microseconds, so a queue here would add a way to lose rows (a container
        dying with rows still in it) to buy nothing. `flush()` is a no-op for the same reason, and
        `queue_depth` is reported as 0 rather than omitted so the operator page needs no special case.
        """
        row = {c: kw.get(c) for c in COLUMNS}
        row["event_id"] = kw.get("event_id") or str(uuid.uuid4())
        row["received_ts"] = kw.get("received_ts") or _now_ts()
        row["event_ts"] = kw.get("event_ts") or row["received_ts"]
        row["event_type"] = event_type
        row["app_build"] = self.build
        for c in ALL_COLUMNS:
            v = row.get(c)
            if isinstance(v, (dict, list)):
                row[c] = json.dumps(v)[:MAX_TEXT]
            elif isinstance(v, str) and len(v) > MAX_TEXT:
                row[c] = v[:MAX_TEXT]
            elif c in INT_COLS or c in BIGINT_COLS:
                # Parenthesised deliberately: `v is not None and c in INT_COLS or c in BIGINT_COLS`
                # binds as `(… and …) or (…)`, which ran the numeric branch for latency_ms even when the
                # value was None. Harmless here only because of the inner guard — but the kind of
                # precedence bug that reads as correct, so the condition is on the COLUMN alone now and
                # the None case is handled inside.
                try:
                    row[c] = int(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    row[c] = None
        self.stats["queued"] += 1
        try:
            self._insert([row])
            self.stats["written"] += 1
            self.stats["last_write_ts"] = time.time()
        except Exception as e:
            # Same posture as Delta mode: a lost row is LOUD. See the long note in logstore.flush.
            self.stats["failed"] += 1
            self.stats["dropped_rows"] += 1
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:400]
            self.stats["last_drop"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                       "rows": 1, "event_types": {event_type: 1},
                                       "error": f"{type(e).__name__}: {e}"[:200]}
            print(f"[localstore] ⛔ DROPPED 1 ROW — event_type={event_type} db={self.path} "
                  f"error={type(e).__name__}: {e}"[:600], file=sys.stderr, flush=True)
        return row["event_id"]

    def _insert(self, rows, ignore_dupes=False):
        """-> rows actually written. `ignore_dupes` is for the importer: a row whose event_id is already
        present is a row we already have, which is a SKIP and not a failure."""
        if not rows:
            return 0
        cols = ", ".join(f"`{c}`" for c in ALL_COLUMNS)
        marks = ", ".join("?" for _ in ALL_COLUMNS)
        verb = "INSERT OR IGNORE INTO" if ignore_dupes else "INSERT INTO"
        stmt = f"{verb} {self.table} ({cols}) VALUES ({marks})"
        payload = [[r.get(c) for c in ALL_COLUMNS] for r in rows]
        with self._lock:
            cur = self._db.executemany(stmt, payload)
            self._db.commit()
            self._cache.clear()          # any aggregate could have moved
            return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)

    def flush(self, block_timeout=0.0):
        """No-op: `log` already committed. Present because callers flush without knowing the mode."""
        self.stats["flushes"] += 1
        return 0

    def healthy(self):
        size = None
        try:
            size = os.path.getsize(self.path)
        except OSError:
            pass
        return {"queue_depth": 0, "thread_alive": True,
                "degraded": bool(self.stats.get("dropped_rows")),
                "mode": "local-sqlite", "db_path": self.path, "db_bytes": size,
                # ⭐ Surfaced, not implied: this store does not outlive its container. The operator page
                #    renders it, so the trade is visible to whoever is relying on it.
                "ephemeral": True,
                **self.stats}

    # ---------------------------------------------------------------- reading
    def query(self, key, statement, parameters=None, ttl=None):
        """Runs a `queries.py` statement against SQLite, returning the same list-of-dicts as Delta mode."""
        ttl = self.cache_ttl if ttl is None else ttl
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        params = {}
        for p in (parameters or []):
            v = p.get("value")
            if v is not None and p.get("type") in ("INT", "BIGINT"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    pass
            params[p["name"]] = v
        sql = _translate(statement)
        with self._lock:
            cur = self._db.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            self._cache[key] = (now, rows)
        return rows

    def invalidate(self, prefix=""):
        with self._lock:
            for k in [k for k in self._cache if k.startswith(prefix)]:
                self._cache.pop(k, None)

    # ---------------------------------------------------------------- import support
    def existing_event_ids(self, ids):
        """Which of these event_ids are already stored. Used by the importer to report skips honestly
        instead of inferring them from a rowcount."""
        ids = [i for i in ids if i]
        if not ids:
            return set()
        found = set()
        with self._lock:
            for i in range(0, len(ids), 500):          # SQLite caps variables per statement
                chunk = ids[i:i + 500]
                marks = ",".join("?" for _ in chunk)
                cur = self._db.execute(
                    f"SELECT event_id FROM {self.table} WHERE event_id IN ({marks})", chunk)
                found.update(r[0] for r in cur.fetchall())
        return found

    def insert_rows(self, rows):
        """Bulk insert for the importer, skipping event_ids already present. -> count written."""
        return self._insert(rows, ignore_dupes=True)

    def purge(self, keep_event_types):
        """Same contract as LogStore.purge: delete every row whose event_type is not kept -> (deleted, kept).
        Both modes need it because the operator reset button exists in both."""
        keep = [str(k) for k in (keep_event_types or [])]
        if keep:
            marks = ", ".join("?" for _ in keep)
            where = f"(event_type NOT IN ({marks}) OR event_type IS NULL)"
            args = keep
        else:
            where, args = "1 = 1", []
        with self._lock:                      # the one connection is serialised by this, see __init__
            before = self._db.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]
            doomed = self._db.execute(f"SELECT COUNT(*) FROM {self.table} WHERE {where}",
                                      args).fetchone()[0]
            self._db.execute(f"DELETE FROM {self.table} WHERE {where}", args)
            self._db.commit()
            after = self._db.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0]
        if before - doomed != after:
            raise RuntimeError(f"purge did not reconcile: {before} - {doomed} != {after}")
        self.invalidate("")
        return doomed, after

    def count(self):
        with self._lock:
            return int(self._db.execute(f"SELECT COUNT(*) FROM {self.table}").fetchone()[0])
