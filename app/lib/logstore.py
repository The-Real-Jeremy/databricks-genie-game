"""The one delta table: a batched writer, plus the aggregate reads the game and the operator page need.

Written as the app's service principal over the SQL Statement Execution API, always with NAMED
PARAMETERS — a player's question text goes into this table verbatim, so string interpolation is not an
option. Verified that parameterised multi-row INSERTs round-trip quotes, semicolons and
non-ASCII intact.
"""
import base64, json, queue, sys, threading, time, urllib.error, urllib.request, uuid

# `received_ts` is stamped by the APP when the event happened. `event_ts` is stamped by the WAREHOUSE when
# the INSERT executes, which is a different clock and a queue away: two players answering the same blank
# simultaneously landed 2.639s apart, and under load that queue reached p50 17.8s. Anything that ranks
# players must use received_ts; event_ts remains useful as the record of when the row actually landed.
COLUMNS = ["event_id", "received_ts", "event_type", "season_id", "user_key", "user_email",
           "user_display_name",
           "session_id", "week_index", "real_week_index", "case_id", "clue_id", "question_text",
           "genie_space_id", "genie_conversation_id", "genie_message_id", "genie_user_id",
           "genie_status", "genie_answer_text", "genie_sql", "extracted_value", "expected_value",
           "verdict", "points", "latency_ms", "app_build", "extra"]
INT_COLS = {"week_index", "real_week_index", "points"}
TS_COLS = {"received_ts"}
BIGINT_COLS = {"latency_ms"}
MAX_TEXT = 4000


class LogStore:
    def __init__(self, host, table, warehouse_id, client_id, client_secret, build="dev",
                 flush_interval=2.0, batch_max=40, cache_ttl=20.0, dev_token=None,
                 start_writer=True):
        self.host, self.table, self.warehouse_id = host, table, warehouse_id
        self.client_id, self.client_secret = client_id, client_secret
        self.dev_token = dev_token       # laptop-only; see the note at the call site in app.py
        self.build = build
        self.flush_interval, self.batch_max = flush_interval, batch_max
        self.cache_ttl = cache_ttl
        self._q = queue.Queue()
        self._tok = (None, 0.0)
        self._cache = {}
        self._lock = threading.Lock()
        self.stats = {"queued": 0, "written": 0, "failed": 0, "flushes": 0, "last_error": None,
                      "last_write_ts": None, "dropped_rows": 0, "last_drop": None}
        self._stop = threading.Event()
        # `start_writer=False` leaves the background flusher UNSTARTED. This exists for tests that need to
        # be the only flusher: see `stop()` for why setting the flag is not enough, and why "never started"
        # is the only state in which a second flusher is structurally impossible rather than merely unlikely.
        self._thread = threading.Thread(target=self._loop, daemon=True, name="logwriter")
        if start_writer:
            self._thread.start()

    # ---------------------------------------------------------------- auth (app SP)
    def _token(self):
        if self.dev_token:
            return self.dev_token
        tok, exp = self._tok
        if tok and time.time() < exp - 60:
            return tok
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            self.host + "/oidc/v1/token", data=b"grant_type=client_credentials&scope=all-apis",
            method="POST", headers={"Authorization": "Basic " + basic,
                                    "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
        self._tok = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._tok[0]

    def _sql(self, statement, parameters=None, wait="50s", timeout=90):
        body = {"statement": statement, "warehouse_id": self.warehouse_id, "wait_timeout": wait}
        if parameters:
            body["parameters"] = parameters
        req = urllib.request.Request(
            self.host + "/api/2.0/sql/statements", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + self._token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")
        state = (out.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            raise RuntimeError(f"{state}: {json.dumps((out.get('status') or {}).get('error'))[:300]}")
        return out

    # ---------------------------------------------------------------- writing
    def log(self, event_type, **kw):
        row = {c: kw.get(c) for c in COLUMNS}
        row["event_id"] = kw.get("event_id") or str(uuid.uuid4())
        # Stamped HERE, on the app's own clock, at the moment the event happened.
        row["received_ts"] = kw.get("received_ts") or time.strftime("%Y-%m-%d %H:%M:%S",
                                                                    time.gmtime()) + \
            f".{int((time.time() % 1) * 1000):03d}"
        row["event_type"] = event_type
        row["app_build"] = self.build
        for c in COLUMNS:
            if isinstance(row[c], str) and len(row[c]) > MAX_TEXT:
                row[c] = row[c][:MAX_TEXT]
            if isinstance(row[c], (dict, list)):
                row[c] = json.dumps(row[c])[:MAX_TEXT]
        self._q.put(row)
        self.stats["queued"] += 1
        return row["event_id"]

    def _rows_to_stmt(self, rows):
        cols = ", ".join(f"`{c}`" for c in COLUMNS)
        tuples, params = [], []
        for i, row in enumerate(rows):
            names = []
            for c in COLUMNS:
                p = f"{c}_{i}"
                names.append(f":{p}")
                v = row.get(c)
                typ = ("INT" if c in INT_COLS else "BIGINT" if c in BIGINT_COLS
                       else "TIMESTAMP" if c in TS_COLS else "STRING")
                params.append({"name": p, "type": typ,
                               "value": None if v is None else str(v)})
            tuples.append(f"({', '.join(names)}, current_timestamp())")
        stmt = (f"INSERT INTO {self.table} ({cols}, `event_ts`) VALUES " + ", ".join(tuples))
        return stmt, params

    def flush(self, block_timeout=0.0):
        rows = []
        while len(rows) < self.batch_max:
            try:
                rows.append(self._q.get(timeout=block_timeout) if block_timeout else self._q.get_nowait())
            except queue.Empty:
                break
        if not rows:
            return 0
        stmt, params = self._rows_to_stmt(rows)
        try:
            self._sql(stmt, params)
            self.stats["written"] += len(rows)
            self.stats["flushes"] += 1
            self.stats["last_write_ts"] = time.time()
            return len(rows)
        except Exception as e:
            # ⛔ A DROPPED BATCH IS LOUD, NOT SILENT. The rows are gone — there is no retry here, on purpose:
            #    an INSERT that failed after partially committing would duplicate on retry, and a duplicated
            #    `question_asked` is a worse lie about adoption than a missing one. So the batch is lost and
            #    the loss is made impossible to overlook instead.
            #
            #    Why this matters more than its odds: the batch is now up to 40 rows where it used to be 1,
            #    and the thing being lost is THE ONE NUMBER THIS PRODUCT EXISTS TO MOVE. `stats['failed']`
            #    used to be the only place it ever surfaced — which is the same shape as every silent-success
            #    defect this build has already paid for. It now surfaces in three:
            #      * a marked line in the container log, naming the count and the event types;
            #      * `dropped_rows` / `last_drop` on /api/health, so a monitor or the deploy verifier sees it;
            #      * `degraded` on the writer's health, which the Operator page renders.
            #    Measured context (an independent load test, ~540 greetings): it did not happen once, and the
            #    40-row cap is structurally out of reach because the ingress admits ~100 slow requests so the
            #    app cannot generate more than ~25 rows per 2s tick. That makes it a note, not a finding —
            #    but a quiet note about the adoption number is exactly what this file must not have.
            kinds = {}
            for r in rows:
                kinds[r.get("event_type") or "?"] = kinds.get(r.get("event_type") or "?", 0) + 1
            self.stats["failed"] += len(rows)
            self.stats["dropped_rows"] = self.stats.get("dropped_rows", 0) + len(rows)
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:400]
            self.stats["last_drop"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                       "rows": len(rows), "event_types": kinds,
                                       "error": f"{type(e).__name__}: {e}"[:200]}
            print(f"[logstore] ⛔ DROPPED {len(rows)} ROWS — NOT RETRIED, THEY ARE GONE. "
                  f"event_types={kinds} table={self.table} error={type(e).__name__}: {e}"[:600],
                  file=sys.stderr, flush=True)
            return 0

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.flush(block_timeout=self.flush_interval)
            except Exception as e:                                   # never let the writer die silently
                self.stats["last_error"] = f"writer loop: {type(e).__name__}: {e}"[:400]
                time.sleep(1.0)

    def stop(self, timeout=None):
        """Stop the background writer and WAIT for it. -> True if it is really stopped.

        ⛔ `self._stop.set()` ON ITS OWN IS NOT A STOP, and believing otherwise produced a real flake.
           The writer spends nearly all its life blocked inside `self._q.get(timeout=flush_interval)`, and
           an Event does not interrupt a blocking get — it is only noticed when the get returns. So after
           `set()` the thread stays ALIVE for up to `flush_interval` (2.0s by default): measured alive at
           1.75s and gone only by 2.0s, which is LONGER THAN A TEST BODY.

           The consequence is not a hung thread, it is a SECOND FLUSHER. Two flushers sharing one queue
           split a 2-row batch into 1+1, so a counter asserted immediately after reads half of what the
           caller put in — intermittently, and only when the suite is slow enough for the timing to line up
           (measured 2 of 60 full-suite runs, 0 of 120 runs of that file alone, i.e. cross-file timing).

           So: set the flag, then JOIN. `flush_interval` is the natural default timeout because that is the
           longest the writer can be inside its get.
        """
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(self.flush_interval + 0.5 if timeout is None else timeout)
        return not self._thread.is_alive()

    def healthy(self):
        """`degraded` is the one field a monitor should key on: it means rows that were meant to be
        recorded are gone, which for this product means the adoption number is understated by an unknown
        amount. It stays true for the life of the container on purpose — a transient loss is still a loss,
        and something that clears itself is something nobody ever sees."""
        return {"queue_depth": self._q.qsize(), "thread_alive": self._thread.is_alive(),
                "degraded": bool(self.stats.get("dropped_rows")), **self.stats}


    # ---------------------------------------------------------------- import support
    # These two exist so `importer.py` is mode-agnostic: it talks to a store, and the store knows whether
    # it is Delta or SQLite. `LocalStore` implements the same pair against a UNIQUE index; here, where
    # Delta has no uniqueness constraint to lean on, the dedup is an explicit read-then-write.
    def existing_event_ids(self, ids):
        """Which of these event_ids the table already holds."""
        ids = [str(i) for i in ids if i]
        if not ids:
            return set()
        found = set()
        # Chunked because the statement carries one named parameter per id, and a 50k-row import would
        # otherwise build a single statement no warehouse will accept.
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            names = [f"e{j}" for j in range(len(chunk))]
            marks = ", ".join(f":{n}" for n in names)
            params = [{"name": n, "type": "STRING", "value": v} for n, v in zip(names, chunk)]
            out = self._sql(
                f"SELECT event_id FROM {self.table} WHERE event_id IN ({marks})", params)
            for r in ((out.get("result") or {}).get("data_array") or []):
                found.add(r[0])
        return found

    def purge(self, keep_event_types):
        """Delete every row whose event_type is NOT in `keep_event_types`. -> (deleted, kept).

        ⛔ A NAMED OPERATION, NOT A SQL EXECUTOR. The operator RESET button reaches this from an HTTP
        request, so the store must not expose "run this statement" to anything a request can influence.
        The only variable part is a list of event-type names, each bound as a named parameter.

        ⭐ IT COUNTS BOTH SIDES AND RETURNS THEM, because the caller has to reconcile: before - deleted ==
        kept. A DELETE's own success says nothing about how many rows went, and this project has already
        been burned by a cleanup that removed somebody else's rows while reporting success.
        """
        keep = [str(k) for k in (keep_event_types or [])]
        names = [f"k{i}" for i in range(len(keep))]
        params = [{"name": n, "type": "STRING", "value": v} for n, v in zip(names, keep)]
        where = (f"event_type NOT IN ({', '.join(':' + n for n in names)}) OR event_type IS NULL"
                 if keep else "1 = 1")
        before = self._one(f"SELECT COUNT(*) FROM {self.table}")
        doomed = self._one(f"SELECT COUNT(*) FROM {self.table} WHERE {where}", params)
        self._sql(f"DELETE FROM {self.table} WHERE {where}", params)
        after = self._one(f"SELECT COUNT(*) FROM {self.table}")
        if before - doomed != after:
            raise RuntimeError(f"purge did not reconcile: {before} - {doomed} != {after}")
        return doomed, after

    def _one(self, statement, parameters=None):
        out = self._sql(statement, parameters)
        rows = (out.get("result") or {}).get("data_array") or []
        return int(rows[0][0]) if rows else 0

    def insert_rows(self, rows):
        """Bulk insert already-deduplicated rows, preserving their ORIGINAL timestamps. -> count written.

        ⛔ NOT the same statement as `_rows_to_stmt`, and the difference is the point: that one writes
        `current_timestamp()` into `event_ts` because it is recording a live event. A restore must carry
        the timestamps the rows already had, or the import itself becomes the moment everything happened
        and every by-day and by-week aggregate collapses onto today.
        """
        if not rows:
            return 0
        written = 0
        cols = ", ".join(f"`{c}`" for c in (["event_ts"] + list(COLUMNS)))
        for i in range(0, len(rows), self.batch_max):
            batch = rows[i:i + self.batch_max]
            tuples, params = [], []
            for n, row in enumerate(batch):
                names = []
                for c in ["event_ts"] + list(COLUMNS):
                    pn = f"{c}_{n}"
                    names.append(f":{pn}")
                    v = row.get(c)
                    typ = ("INT" if c in INT_COLS else "BIGINT" if c in BIGINT_COLS
                           else "TIMESTAMP" if (c in TS_COLS or c == "event_ts") else "STRING")
                    params.append({"name": pn, "type": typ,
                                   "value": None if v is None else str(v)})
                tuples.append(f"({', '.join(names)})")
            self._sql(f"INSERT INTO {self.table} ({cols}) VALUES " + ", ".join(tuples), params)
            written += len(batch)
        self.invalidate("")
        return written

    # ---------------------------------------------------------------- reading
    def query(self, key, statement, parameters=None, ttl=None):
        ttl = self.cache_ttl if ttl is None else ttl
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        out = self._sql(statement, parameters)
        cols = [c["name"] for c in ((out.get("manifest") or {}).get("schema") or {}).get("columns", [])]
        rows = [dict(zip(cols, r)) for r in ((out.get("result") or {}).get("data_array") or [])]
        with self._lock:
            self._cache[key] = (now, rows)
        return rows

    def invalidate(self, prefix=""):
        with self._lock:
            for k in [k for k in self._cache if k.startswith(prefix)]:
                self._cache.pop(k, None)
