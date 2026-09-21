#!/usr/bin/env python3
"""Idempotent deploy-time bootstrap. Runs from the DAB postdeploy hook, as the DEPLOYING HUMAN.

Creates (or finds) everything the app needs beyond the app itself:
  1. the ONE delta activity table, INSIDE A SCHEMA YOU ALREADY OWN, granted to the app's service
     principal. ⛔ It does NOT create the schema any more — it checks that the one you named
     exists and says so by name at deploy time if it does not.
  2. a Genie space PER SCENARIO named in SEASONS, built from that scenario's content pack, and granted
     to the audience group
Everything here is find-or-create: running it twice changes nothing.

One space per scenario is a measured decision, not tidiness. A space holding two unrelated sample
domains answered from the wrong one — asked for the top payment method by revenue it returned
`creditcard` from samples.wanderbricks.payments instead of the bakery's own paymentMethod column
(17/18 vs 18/18). A wrong-domain answer raises no error, so the player is
simply told "wrong" for asking a perfectly good question. SEASONS defaults to the active scenario only,
which keeps a pilot at exactly four components.

No pip dependencies — shells out to the Databricks CLI, which the deployer already has.
"""
import glob, json, os, subprocess, sys

PROFILE   = os.environ.get("DB_PROFILE") or ""
APP_NAME  = os.environ.get("APP_NAME", "night-desk")
WAREHOUSE = os.environ.get("WAREHOUSE_ID", "")
# ⭐ ONE INPUT, `catalog.schema`, AND IT MUST ALREADY EXIST. The deployer points at a schema they
#    own and can use, rather than at a catalog in which this would have to create one.
#    So nothing here creates a schema any more — it creates TABLES INSIDE one.
#    LOG_CATALOG is gone; splitting the FQ name here keeps the two halves impossible to pass inconsistently.
LOG_SCHEMA = (os.environ.get("LOG_SCHEMA") or "").strip()
_parts = [x.strip("`") for x in LOG_SCHEMA.split(".") if x.strip("`")]
CATALOG = _parts[0] if len(_parts) == 2 else ""
SCHEMA = _parts[1] if len(_parts) == 2 else ""
TABLE     = os.environ.get("LOG_TABLE", "activity_log")
AUDIENCE  = os.environ.get("AUDIENCE_GROUP", "users")
SEASON_ID = os.environ.get("SEASON_ID", "s1_bakehouse")
SEASONS   = [s.strip() for s in (os.environ.get("SEASONS") or SEASON_ID).split(",") if s.strip()]
CONTENT   = os.environ.get("CONTENT_DIR", "app/content")
# Appended to every scenario's space title so two targets in one workspace get their own spaces rather
# than silently sharing one. The app applies the SAME suffix, from GENIE_SPACE_SUFFIX.
# Not stripped: a leading space in " (dev)" is meaningful, and losing it would create a second space
# under a subtly different title instead of finding the existing one.
_sfx = os.environ.get("SPACE_SUFFIX") or ""
SUFFIX    = "" if _sfx.strip().lower() in ("", "none") else _sfx
# ── STORAGE MODE ───────────────────────────────────────────────────────────────
# In `local` mode the app keeps its activity log in a SQLite file inside its own container, so there is
# no schema to create, no table to migrate and no grant to make. The GENIE SPACE IS STILL CREATED: that
# is the game itself, not the logging, and skipping it would deploy an app with nothing to ask.
STORAGE_MODE = (os.environ.get("STORAGE_MODE") or "delta").strip().lower()
if STORAGE_MODE in ("sqlite", "local-sqlite", "localdb", "local_db"):
    STORAGE_MODE = "local"


def cli(*args, jsonbody=None, check=True):
    cmd = ["databricks", *args] + (["-p", PROFILE] if PROFILE else [])
    if jsonbody is not None:
        cmd += ["--json", json.dumps(jsonbody)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "").strip()
    if r.returncode != 0 and check:
        raise SystemExit(f"FAILED: {' '.join(cmd[:4])}\n{r.stdout}\n{r.stderr}")
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        return {"_raw": out, "_err": r.stderr.strip()}


def sql(stmt, wait="50s"):
    r = cli("api", "post", "/api/2.0/sql/statements",
            jsonbody={"statement": stmt, "warehouse_id": WAREHOUSE, "wait_timeout": wait})
    state = (r.get("status") or {}).get("state")
    if state != "SUCCEEDED":
        raise SystemExit(f"SQL {state}: {json.dumps((r.get('status') or {}).get('error'))}\n  {stmt[:200]}")
    return ((r.get("result") or {}).get("data_array") or [])


DDL = """CREATE TABLE IF NOT EXISTS {fq} (
  event_id              STRING    COMMENT 'uuid4 per event',
  event_ts              TIMESTAMP COMMENT 'WAREHOUSE clock, when the INSERT executed. A queue away from when the event happened - do NOT rank players by it.',
  received_ts           TIMESTAMP COMMENT 'APP clock, when the event happened. The only column safe to order players by: two players answering the same blank simultaneously landed 2.639s apart on event_ts, and under load that write queued at p50 17.8s.',
  event_type            STRING    COMMENT 'session_start | question_greeting | question_asked | question_failed | question_unattributed | hint_used | answer_correct | answer_near | answer_wrong | answer_empty | week_time | and the META types, which no player aggregate counts: admin_setting | admin_reset | admin_storage | import_receipt',
  season_id             STRING    COMMENT 'which content pack this row belongs to - scenario is switchable, so history must say which one it was scored under',
  user_key              STRING    COMMENT 'workspace user id from x-forwarded-user, stable across sessions',
  user_email            STRING,
  user_display_name     STRING,
  session_id            STRING    COMMENT 'one browser visit',
  week_index            INT       COMMENT 'THE WEEK THIS ROW WAS SCORED UNDER. For a clue event it is the clue own case week, which is an immutable property of the content. Never re-derived from the current setting, so an admin changing the active week cannot re-bucket anyone score.',
  real_week_index       INT       COMMENT 'the week the CALENDAR said when the row was written. Differs from week_index when an admin override or a past-week case was in play - that difference is how demo activity is told apart from live activity.',
  case_id               STRING,
  clue_id               STRING    COMMENT 'which of the five targets in the case',
  question_text         STRING    COMMENT 'exactly what the user sent to Genie',
  genie_space_id        STRING,
  genie_conversation_id STRING,
  genie_message_id      STRING,
  genie_user_id         STRING    COMMENT 'user_id Genie itself recorded on the message: the attribution proof',
  genie_status          STRING,
  genie_answer_text     STRING,
  genie_sql             STRING,
  extracted_value       STRING    COMMENT 'the value the player lodged',
  expected_value        STRING,
  verdict               STRING    COMMENT 'correct | near | wrong | empty',
  points                INT,
  latency_ms            BIGINT,
  app_build             STRING,
  extra                 STRING    COMMENT 'json, for anything added later without a schema change'
)
USING DELTA
COMMENT 'The Genie Bake-Off activity log - the single table this app writes.'
TBLPROPERTIES (delta.enableChangeDataFeed = false)"""

# Columns added after the first deployment. Delta has no ADD COLUMN IF NOT EXISTS, so the existing
# columns are read from information_schema and only the genuinely missing ones are added.
MIGRATIONS = [("season_id", "STRING"), ("real_week_index", "INT"), ("received_ts", "TIMESTAMP")]


def migrate(fq, catalog, schema, table):
    have = {r[0] for r in sql(
        f"SELECT lower(column_name) FROM `{catalog}`.information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}'")}
    if not have:
        return []
    missing = [(n, t) for n, t in MIGRATIONS if n.lower() not in have]
    for name, typ in missing:
        sql(f"ALTER TABLE {fq} ADD COLUMNS (`{name}` {typ})")
    return [n for n, _ in missing]


def load_pack(season_id):
    for path in sorted(glob.glob(os.path.join(CONTENT, "*.json"))):
        with open(path, encoding="utf-8") as f:
            pack = json.load(f)
        if pack.get("season_id") == season_id:
            return pack, path
    raise SystemExit(f"no content pack with season_id={season_id!r} under {CONTENT}")


def tables_exist(fqns):
    """Report which of a scenario's tables are actually present, so a workspace without a dataset says
    so instead of getting a Genie space that cannot answer anything."""
    missing = []
    for fq in fqns:
        cat, sch, tbl = fq.split(".")
        rows = sql(f"SELECT 1 FROM `{cat}`.information_schema.tables "
                   f"WHERE table_schema = '{sch}' AND table_name = '{tbl}' LIMIT 1")
        if not rows:
            missing.append(fq)
    return missing


def space_instruction_text(pack):
    """One string, no newlines. Measured, and both failure modes are silent:

      * `content: [a, b, c]`   -> stored as "abc", the three paragraphs GLUED with no separator.
      * three separate text_instructions -> collapsed to the same single glued string.
      * a single string containing "\n\n" -> TRUNCATED at the first newline. Three paragraphs joined
        that way came back as 20 characters, and nothing errored.

    So the only safe shape is one whitespace-normalised string, and it is worth reading back (below)
    rather than trusting the write.
    """
    parts = [" ".join(str(p).split()) for p in (pack.get("space_instructions") or []) if str(p).strip()]
    return " ".join(parts)


def serialized_space(pack):
    # data_sources.tables MUST be sorted by identifier or create fails: "data_sources.tables must be
    # sorted by identifier".
    body = {"version": 2,
            "data_sources": {"tables": [{"identifier": t} for t in sorted(pack["requires_tables"])]}}
    text = space_instruction_text(pack)
    if text:
        body["instructions"] = {"text_instructions": [{"content": [text]}]}
    return body


def verify_space(space_id, pack):
    """Read the space back and check the artefact, not the write. A silently truncated instruction set
    is the difference between Genie disambiguating 'revenue' and guessing at it."""
    got = cli("api", "get", f"/api/2.0/genie/spaces/{space_id}?include_serialized_space=true")
    try:
        body = json.loads(got.get("serialized_space") or "{}")
    except json.JSONDecodeError:
        print("[bootstrap]   WARNING could not read the space back")
        return False
    have_tables = sorted(t["identifier"] for t in
                         (body.get("data_sources") or {}).get("tables") or [])
    want_tables = sorted(pack["requires_tables"])
    ok = have_tables == want_tables
    if not ok:
        print(f"[bootstrap]   WARNING tables differ: wanted {len(want_tables)}, space has "
              f"{len(have_tables)} ({set(want_tables) ^ set(have_tables)})")
    stored = " ".join((body.get("instructions") or {}).get("text_instructions", [{}])[0]
                      .get("content", [""]))
    want = space_instruction_text(pack)
    if want:
        # check a marker from every paragraph survived, not just that something is there
        missing = [" ".join(str(p).split())[:28] for p in pack.get("space_instructions", [])
                   if " ".join(str(p).split())[:28] not in stored]
        if missing:
            ok = False
            print(f"[bootstrap]   WARNING {len(missing)} instruction paragraph(s) did not survive the "
                  f"write: {missing}")
        else:
            print(f"[bootstrap]   instructions verified ({len(stored)} chars, "
                  f"{len(pack.get('space_instructions', []))} paragraphs)")
    return ok


def ensure_space(pack):
    title = (pack.get("space_title") or pack["season_id"]) + SUFFIX
    # A Genie space is registered as a workspace item, so its title inherits workspace-item rules.
    # Measured: a "/" fails with "Workspace items cannot contain the '/' character" AFTER creation.
    if "/" in title:
        raise SystemExit(f"space_title must not contain '/': {title!r}")
    missing = tables_exist(pack["requires_tables"])
    if missing:
        # ⛔ NAME THE CAUSE, NOT ONLY THE SYMPTOM. This message used to say only which tables were absent,
        #    and then the deploy died with "no Genie space could be created". A deployer was told the
        #    symptom twice and the cause never: `samples` is a Databricks-PROVIDED Unity Catalog share, so
        #    it cannot be created, and a workspace without Unity Catalog can never run this game at all.
        #    Without that sentence the obvious next step is to go and build five tables by hand, which
        #    cannot work.
        print(f"[bootstrap] SKIPPING scenario {pack['season_id']}: this workspace has no "
              f"{', '.join(missing)}")
        if any(fq.startswith("samples.") for fq in missing):
            print("[bootstrap]   `samples` is a Databricks-PROVIDED Unity Catalog share, not something you "
                  "create: if it is absent this workspace")
            print("[bootstrap]   almost certainly has no Unity Catalog, and the game cannot run here at "
                  "all. Creating the tables by hand will NOT help —")
            print("[bootstrap]   the content pack's questions are written against that dataset. Use a "
                  "Unity Catalog workspace, or author a new content")
            print("[bootstrap]   pack against data you do have (README: 'Changing the theme, the story, or "
                  "the data').")
        return None
    body = serialized_space(pack)
    spaces = (cli("api", "get", "/api/2.0/genie/spaces").get("spaces") or [])
    match = [s for s in spaces if s.get("title") == title]
    if match:
        space_id = sorted(match, key=lambda s: s.get("create_time", ""))[0]["space_id"]
        origin = "adopted"
        cli("api", "patch", f"/api/2.0/genie/spaces/{space_id}",
            jsonbody={"serialized_space": json.dumps(body), "warehouse_id": WAREHOUSE}, check=False)
        print(f"[bootstrap] Genie space up to date: {title} -> {space_id}")
    else:
        origin = "created"
        r = cli("api", "post", "/api/2.0/genie/spaces",
                jsonbody={"warehouse_id": WAREHOUSE, "title": title,
                          "description": f"Ask questions of the data. {pack.get('title', '')} "
                                         f"— created by the night-desk bundle.",
                          "serialized_space": json.dumps(body)})
        space_id = r["space_id"]
        print(f"[bootstrap] Genie space created: {title} -> {space_id}")
    cli("api", "patch", f"/api/2.0/permissions/genie/{space_id}",
        jsonbody={"access_control_list": [{"group_name": AUDIENCE, "permission_level": "CAN_RUN"}]})
    print(f"[bootstrap]   granted {AUDIENCE} CAN_RUN")
    # ⭐ CREATED-versus-ADOPTED IS NOW A REPORTED VALUE, not something a reader infers from which sentence
    #    was printed. Resolution is BY TITLE, so a space somebody left behind under the same title is
    #    SILENTLY ADOPTED — and a test asserting "a fresh workspace can create its space", which is the one
    #    assertion a fresh workspace has historically failed, then passes WITHOUT EXERCISING CREATION.
    #    Telling the next tester to pick an unused suffix is a discipline and it fails the moment somebody
    #    reuses one. This is the mechanism instead: the value is emitted on a parseable line, and
    #    REQUIRE_NEW_SPACE makes the deploy REFUSE on an adoption, so a leftover space cannot make the
    #    assertion vacuous however careless the next person is.
    print(f"[bootstrap]   space origin: {origin}")
    verify_space(space_id, pack)
    return space_id, origin


def main():
    # The warehouse is required in BOTH modes — the Genie space uses it. The catalog is required only in
    # delta mode, and `none` is the sentinel the bundle passes when it is deliberately unset.
    if not WAREHOUSE:
        raise SystemExit("WAREHOUSE_ID must be set (the Genie space needs a warehouse in both modes)")
    delta = STORAGE_MODE == "delta"
    if delta and (not LOG_SCHEMA or LOG_SCHEMA.lower() == "none"):
        raise SystemExit("STORAGE_MODE=delta needs LOG_SCHEMA; it is unset or still the 'none' sentinel. "
                         "Pass --var log_schema=<catalog>.<schema>, naming a schema you already have.")
    if delta and not (CATALOG and SCHEMA):
        # ⛔ NAME WHAT WAS GIVEN AND WHAT WAS WANTED. `--var log_schema=my_schema` (no catalog) is the
        #    mistake this catches, and it would otherwise surface as SQL against a half-built identifier.
        raise SystemExit(f"LOG_SCHEMA must be <catalog>.<schema>; got {LOG_SCHEMA!r}. "
                         f"Two parts, e.g. main.genie_game — not a catalog and not a bare schema name.")
    print(f"[bootstrap] mode={STORAGE_MODE} warehouse={WAREHOUSE} app={APP_NAME} "
          + (f"schema={CATALOG}.{SCHEMA} " if delta else "(no table — SQLite in the container) ")
          + f"scenarios={','.join(SEASONS)}")

    app = cli("apps", "get", APP_NAME)
    sp = app.get("service_principal_client_id") or app.get("service_principal_id")
    print(f"[bootstrap] app SP: {app.get('service_principal_name')} (client_id {sp})")

    if delta:
        fq = f"`{CATALOG}`.`{SCHEMA}`.`{TABLE}`"
        # ⭐ THE SCHEMA IS CHECKED, NOT CREATED, AND THE CHECK IS BY NAME AT DEPLOY TIME. A missing or
        #    unreadable schema used to become a SQL error on the first write, i.e. in front of a player,
        #    hours after the deploy said it was fine. `information_schema.schemata` is the enumerating
        #    source: it answers "is it there" without attempting a write.
        rows = sql(f"SELECT schema_name FROM `{CATALOG}`.information_schema.schemata "
                   f"WHERE schema_name = '{SCHEMA}'")
        if not rows:
            raise SystemExit(
                f"[bootstrap] the schema {CATALOG}.{SCHEMA} does not exist, or you cannot see it.\n"
                f"  This deploy creates TABLES inside a schema you own — it no longer creates the schema.\n"
                f"  Create it and re-run, e.g.:  CREATE SCHEMA {CATALOG}.{SCHEMA};\n"
                f"  (If it does exist, you are missing USE CATALOG on {CATALOG} or USE SCHEMA on it — the\n"
                f"   same message covers both, because from here they are indistinguishable.)")
        sql(DDL.format(fq=fq))
        added = migrate(fq, CATALOG, SCHEMA, TABLE)
        print(f"[bootstrap] table ready: {CATALOG}.{SCHEMA}.{TABLE}"
              + (f" (added columns: {', '.join(added)})" if added else ""))
        if sp:
            # ⛔ THE APP'S SERVICE PRINCIPAL IS A DIFFERENT PRINCIPAL FROM YOU, so owning the schema is not
            #    enough — the SP needs its own grants or the log writer silently degrades.
            # ⚠️ AND `USE CATALOG` IS THE ONE YOU MAY NOT BE ABLE TO GRANT. A schema owner cannot grant on
            #    the catalog above it; only its owner (or someone with MANAGE) can. So each grant is
            #    attempted separately and a refusal NAMES the statement somebody else has to run, rather
            #    than failing the deploy with a bare PERMISSION_DENIED.
            for stmt, remedy in (
                (f"GRANT USE CATALOG ON CATALOG `{CATALOG}` TO `{sp}`",
                 f"ask the owner of catalog {CATALOG} to run it"),
                (f"GRANT USE SCHEMA ON SCHEMA `{CATALOG}`.`{SCHEMA}` TO `{sp}`",
                 f"ask the owner of schema {CATALOG}.{SCHEMA} to run it"),
                (f"GRANT SELECT, MODIFY ON TABLE {fq} TO `{sp}`",
                 f"ask the owner of {CATALOG}.{SCHEMA} to run it"),
            ):
                try:
                    sql(stmt)
                except SystemExit as e:
                    print(f"[bootstrap] ⛔ COULD NOT GRANT — the app will start and record NOTHING until "
                          f"this is run:\n    {stmt};\n  {remedy}. Underlying error: {e}", file=sys.stderr)
                    raise SystemExit(f"[bootstrap] the app's service principal ({sp}) could not be granted "
                                     f"on {CATALOG}.{SCHEMA}. The statement above is the whole remedy.")
            print("[bootstrap] granted USE/SELECT/MODIFY to the app SP")
    else:
        print("[bootstrap] local mode: no schema, no table, no grant. The activity log lives in "
              f"SQLite inside the container and DOES NOT SURVIVE A RESTART — export it from the "
              f"Operator page to keep it, and import it back to restore.")

    made, origins = {}, {}
    for season in SEASONS:
        pack, path = load_pack(season)
        got = ensure_space(pack)
        if got:
            made[season], origins[season] = got
    if not made:
        raise SystemExit("[bootstrap] no Genie space could be created — the game cannot run")
    print("[bootstrap] DONE " + " ".join(f"{k}={v}" for k, v in made.items()))
    print("[bootstrap] SPACE_ORIGINS " + " ".join(f"{k}={v}" for k, v in origins.items()))
    # ⛔ OPT-IN, AND IT FAILS CLOSED. Unset, nothing changes — adopting a space is the CORRECT behaviour for
    #    a redeploy and must stay silent. Set, it refuses an adoption, which is what makes a first-install
    #    test assert creation rather than assert whatever happened to be there. The refusal names the title,
    #    because the remedy is to delete that space or choose a suffix nobody has used.
    if os.environ.get("REQUIRE_NEW_SPACE", "").strip().lower() in ("1", "true", "yes"):
        adopted = sorted(k for k, v in origins.items() if v != "created")
        if adopted:
            raise SystemExit(
                "[bootstrap] REQUIRE_NEW_SPACE is set and these scenarios ADOPTED an existing Genie space "
                "instead of creating one: " + ", ".join(adopted) + ". Space resolution is BY TITLE, so a "
                "space left behind under the same title is reused silently — and a test that meant to prove "
                "a fresh workspace can create its space would pass without ever creating one. Delete that "
                "space, or deploy with a --var space_suffix nobody has used.")


if __name__ == "__main__":
    main()
