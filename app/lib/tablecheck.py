"""IS THIS TABLE USABLE? — the check that runs before the app is allowed to write the game's scores to it.

THE REQUIREMENT: entering a table checks that it is accessible, and a table that already has data in it is
picked up from where it stands.

⭐ WHY A CHECK RATHER THAN A TRY. The failure this prevents is not a crash — it is an app that starts,
serves every page, scores nothing and shows an empty leaderboard, which reads as a broken deploy and is
really a missing grant. The whole point is to refuse the setting at the moment somebody is standing there
able to fix it, and to say which of the four possible things is wrong.

⛔ AND IT TESTS THE TABLE, NOT THE CONFIGURATION. Adding a Unity Catalog table as an app RESOURCE is what
grants the service principal its privileges (measured: `SELECT`/`MODIFY` on the table,
`USE SCHEMA`, and `USE CATALOG` — issued by the platform as the person who added the resource). But
`apps update` accepted a resource naming `samples.bakehouse.sales_customers` and returned 200 while grants
on sample tables are *not supported at all* — so the API's success proves nothing about the privilege. Only
a real read and a real write do, and those are what this module does.

## THE FIVE ANSWERS, in the order a person can act on them
1. `bad_name`        — not `catalog.schema.table`. Nothing was attempted.
2. `no_warehouse`    — the app has no SQL warehouse, so it cannot reach Unity Catalog at all. Names the fix.
3. `denied`          — Unity Catalog refused. The message is UC's own, which names the missing privilege.
4. `missing`         — the catalog and schema are reachable and the table is not there. Offers the DDL.
5. `wrong_shape`     — the table exists and does not have the columns this app writes. Lists which.
…and `ok`, which carries the row count so "pick up from there" is visible before anything is switched.

The write test is an INSERT of one real audit row (`admin_storage` — a META event type, so it is excluded
from every player aggregate and survives a reset, because it is configuration history) plus a DELETE that
matches nothing. A zero-row DELETE proves `MODIFY` covers deletes — which the reset button needs — without
touching a single row of anybody's data.
"""
import json, re, time, urllib.error, urllib.request, uuid

from .logstore import COLUMNS

# What the app writes. `event_ts` is added by the DDL and stamped by the warehouse, so it is required in the
# table and never in an INSERT. Derived from the writer's own column list so this cannot drift from it.
REQUIRED_COLUMNS = tuple(["event_ts"] + list(COLUMNS))
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def split_fqn(raw):
    """-> (catalog, schema, table) or (None, None, reason). Accepts backticks and stray whitespace."""
    s = (raw or "").strip().strip(";")
    if not s:
        return None, None, "type a table name as catalog.schema.table"
    parts = [p.strip().strip("`") for p in s.split(".")]
    if len(parts) != 3 or not all(parts):
        return None, None, (f"{s!r} is not a three-part name. It must be "
                            f"catalog.schema.table — for example main.bake_off.activity_log")
    for p in parts:
        if not IDENT.match(p):
            return None, None, (f"{p!r} is not a plain identifier. Letters, digits and underscores only, "
                                f"starting with a letter or underscore — this app builds SQL around the "
                                f"name, so it will not accept anything that needs quoting.")
    return parts[0], parts[1], parts[2]


def create_sql(fqn):
    """The DDL for a blank log table, as one statement an operator can paste into a SQL editor.

    ⭐ READ FROM bootstrap/bootstrap.py, never re-typed here. Two copies of this schema is two schemas, and
    the one nobody runs is the one that rots — the operator page would hand out a DDL that the writer no
    longer matches. Falls back to a derived-from-COLUMNS version only if the bootstrap is not shipped
    beside the app (the manual upload takes `app/` alone).
    """
    import os
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for cand in (os.path.join(here, "bootstrap", "bootstrap.py"),
                 os.path.join(os.path.dirname(here), "bootstrap", "bootstrap.py")):
        try:
            with open(cand, encoding="utf-8") as f:
                src = f.read()
            i = src.index('DDL = """') + len('DDL = """')
            j = src.index('"""', i)
            return src[i:j].replace("{fq}", fqn)
        except Exception:
            continue
    types = {"event_ts": "TIMESTAMP", "received_ts": "TIMESTAMP", "week_index": "INT",
             "real_week_index": "INT", "points": "INT", "latency_ms": "BIGINT"}
    cols = ",\n  ".join(f"{c:<22}{types.get(c, 'STRING')}" for c in REQUIRED_COLUMNS)
    return (f"CREATE TABLE IF NOT EXISTS {fqn} (\n  {cols}\n)\nUSING DELTA\n"
            f"COMMENT 'The Genie Bake-Off activity log — the single table this app writes.'")


def grant_sql(fqn, principal):
    cat, sch, _ = fqn.split(".")
    p = f"`{principal}`"
    return "\n".join([f"GRANT USE CATALOG ON CATALOG {cat} TO {p};",
                      f"GRANT USE SCHEMA ON SCHEMA {cat}.{sch} TO {p};",
                      f"GRANT SELECT, MODIFY ON TABLE {fqn} TO {p};"])


class Checker:
    """Runs the checks as the APP's service principal — the identity that will do the writing.

    It has to be that identity and not the operator's: an operator with `MANAGE` on the catalog would pass
    every check while the app still cannot write a row, which is precisely the silent failure this exists
    to catch.
    """

    def __init__(self, host, warehouse_id, token_fn, build="dev"):
        self.host, self.warehouse_id, self.token_fn, self.build = host, warehouse_id, token_fn, build

    def _sql(self, statement, parameters=None, timeout=70):
        body = {"statement": statement, "warehouse_id": self.warehouse_id, "wait_timeout": "50s"}
        if parameters:
            body["parameters"] = parameters
        req = urllib.request.Request(
            self.host + "/api/2.0/sql/statements", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.token_fn(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return {"_http": e.code, "_error": e.read().decode()[:400]}
        except Exception as e:
            return {"_http": -1, "_error": f"{type(e).__name__}: {e}"}
        state = (out.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            err = (out.get("status") or {}).get("error") or {}
            return {"_http": 200, "_state": state, "_error": err.get("message") or json.dumps(err)[:300],
                    "_code": err.get("error_code")}
        return {"rows": ((out.get("result") or {}).get("data_array") or []),
                "cols": [c.get("name") for c in
                         ((out.get("manifest") or {}).get("schema") or {}).get("columns", [])]}

    # ------------------------------------------------------------------ the check
    def check(self, fqn, write=True):
        t0 = time.time()
        cat, sch, tbl = split_fqn(fqn)
        if cat is None:
            return {"status": "bad_name", "ok": False, "message": tbl}
        fqn = f"{cat}.{sch}.{tbl}"
        out = {"table": fqn, "ok": False, "status": None, "message": None, "steps": []}
        if not self.warehouse_id:
            out.update(status="no_warehouse", message=(
                "This app has no SQL warehouse, so it cannot reach Unity Catalog at all. Add one as an "
                "app resource (Apps → this app → Edit → App resources → SQL warehouse, permission CAN USE) "
                "and the name below can be checked."))
            return out

        def step(name, res):
            out["steps"].append({"step": name, "ok": "_error" not in res,
                                 "error": res.get("_error"), "code": res.get("_code")})
            return res

        # 1. Is the table there, and may we look? `information_schema` is permission-filtered, so an empty
        #    answer means "not there OR not visible to the app" — which is why the error text from the
        #    DESCRIBE below is what decides, rather than this count.
        cols = step("read columns", self._sql(
            f"SELECT column_name FROM {cat}.information_schema.columns "
            f"WHERE table_schema = :sch AND table_name = :tbl",
            [{"name": "sch", "type": "STRING", "value": sch},
             {"name": "tbl", "type": "STRING", "value": tbl}]))
        if "_error" in cols:
            msg = cols["_error"]
            denied = any(k in msg for k in ("INSUFFICIENT_PERMISSIONS", "PERMISSION_DENIED",
                                            "does not have", "Insufficient privileges"))
            out.update(status=("denied" if denied else "error"), message=msg)
            return out
        have = {r[0].lower() for r in cols["rows"]}
        if not have:
            probe = step("describe", self._sql(f"DESCRIBE TABLE {fqn}"))
            msg = probe.get("_error") or ""
            if any(k in msg for k in ("INSUFFICIENT_PERMISSIONS", "PERMISSION_DENIED", "does not have",
                                      "Insufficient privileges")):
                out.update(status="denied", message=msg)
            else:
                out.update(status="missing", message=(
                    f"No table {fqn} is visible to this app. Either it does not exist — create it with the "
                    f"SQL below — or the app has not been granted anything on it."))
            return out
        missing = [c for c in REQUIRED_COLUMNS if c.lower() not in have]
        if missing:
            out.update(status="wrong_shape", columns_missing=missing, message=(
                f"{fqn} exists but is missing {len(missing)} column(s) this app writes: "
                f"{', '.join(missing)}. Point at a different table, or add them."))
            return out

        # 2. Can we READ it, and how much is already in there? This is the "pick up from there" number.
        cnt = step("count rows", self._sql(f"SELECT COUNT(*) AS n FROM {fqn}"))
        if "_error" in cnt:
            out.update(status="denied", message=cnt["_error"])
            return out
        out["rows_existing"] = int((cnt["rows"] or [["0"]])[0][0] or 0)
        players = step("count players", self._sql(
            f"SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(user_email), ''), user_key)) AS p FROM {fqn} "
            f"WHERE event_type NOT IN ('admin_setting', 'import_receipt', 'admin_reset', 'admin_storage')"))
        if "_error" not in players:
            out["players_existing"] = int((players["rows"] or [["0"]])[0][0] or 0)

        # 3. Can we WRITE it? One real audit row, then a DELETE that matches nothing.
        if write:
            eid = "storage-check-" + uuid.uuid4().hex[:12]
            ins = step("insert an audit row", self._sql(
                f"INSERT INTO {fqn} (event_id, event_ts, received_ts, event_type, app_build, extra) "
                f"VALUES (:eid, current_timestamp(), current_timestamp(), 'admin_storage', :build, :extra)",
                [{"name": "eid", "type": "STRING", "value": eid},
                 {"name": "build", "type": "STRING", "value": self.build},
                 {"name": "extra", "type": "STRING",
                  "value": json.dumps({"check": "storage_target", "table": fqn})}]))
            if "_error" in ins:
                out.update(status="denied", message=(
                    "The app can READ that table but not write to it: " + ins["_error"]))
                return out
            # Matches nothing on purpose: it proves MODIFY covers deletes, which the reset button needs,
            # without touching a row of anybody's data.
            dele = step("prove delete", self._sql(
                f"DELETE FROM {fqn} WHERE event_id = :eid",
                [{"name": "eid", "type": "STRING", "value": "storage-check-never-" + uuid.uuid4().hex}]))
            if "_error" in dele:
                out["delete_warning"] = ("The app can write rows but not delete them, so the Reset button "
                                         "will fail: " + dele["_error"])
        out.update(ok=True, status="ok", elapsed_s=round(time.time() - t0, 1), message=(
            f"{fqn} is readable and writable by this app"
            + (f", and already holds {out['rows_existing']} row(s)" if out.get("rows_existing") else
               " and is empty") + "."))
        return out
