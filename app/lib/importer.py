"""Restore scores / progress / leaderboard from a previously exported CSV.

His ask: *"I am assuming the logs is our source of truth, can we also have the functionality that we can
'import' a copy of the exported logs, and this restores the scores / user progress to a previous state?"*

**The assumption in that sentence is correct, and it was verified before this file was written** — see
Every figure the game shows (points, solved/tried, leaderboard rank,
week unlocks, the focus clock that pauses, activation) is SQL over the one activity table; `PlayerStates`
and `Clock` are caches that say so in their own docstrings. So import is a REPLAY, not a reconstruction,
and two properties make the replay safe rather than merely possible: points deduplicate per (player,
clue) with MAX, and the focus clock persists as an accumulated MAX rather than a delta. Replaying a row
twice cannot inflate a score or rewind a clock.

## ⛔ WHAT THIS FILE IS MOSTLY ABOUT: NOT REPRODUCING THE DEFECT IT WAS BUILT TO FIX

The precondition work found that `export_all` was omitting `received_ts` — the one column the DDL calls
"the only column safe to order players by", because `event_ts` is the WAREHOUSE clock and was measured
2.639s off for simultaneous answers and p50 17.8s behind under load. A restore from such a file brings
every point back correctly and silently re-ranks the board onto the warehouse clock, because
`COALESCE(received_ts, event_ts)` quietly absorbs the absence. Values restored, ordering corrupted,
nothing complaining.

The export is fixed. **But every CSV that already exists is pre-fix** — the ones that already exist were
checked and not one carries `received_ts`. So this importer is handed exactly that file shape on day one,
and if it were to COALESCE quietly it would reproduce the same defect one layer along.

**So: this importer never synthesizes `received_ts`.** It would be trivial to write `event_ts` into it and
every downstream query would go green — and that is precisely the failure, because it makes an
unrecoverable ordering *look* authoritative and destroys the evidence that it is not. A NULL is the
honest record that the app clock for that row is unknown.

**And the consequence is made VISIBLE rather than left to be inferred, in three places that outlive the
import**, because the person reading the leaderboard next week is not the person who ran the import:
  1. the import result itself reports `ordering` as one of authoritative / partial / unrecoverable,
     with the row counts behind that word;
  2. every imported row carries an `_import` marker in `extra` naming the source file and whether its
     ordering was recoverable, so the provenance travels with the data;
  3. an `import_receipt` row is written to the log, so the operator page can say so long afterwards.

A pre-fix file is ACCEPTED rather than refused, because the points in it are genuinely recoverable and
those files are currently the only backups that exist — refusing them would be a false negative on real
data. What is refused is not the file, it is the *silence*.

## The other decisions, stated rather than left to be discovered

* **MERGE, not REPLACE**, keyed on `event_id`. Replace would make a restore destructive of anything
  written since the export, and the aggregates are already dedup-safe. Merge also makes "restore after a
  restart" and "load last night's export" the same operation with no mode flag.
* **A row with no `event_id`** (the pre-fix exports have none) gets a DETERMINISTIC id hashed from its own
  content, so importing the same file twice is still a no-op. Trade named: two events identical in every
  exported field collapse into one. That loses at most one duplicate row, against the alternative of
  double-counting `total_questions` — a raw SUM, and the one number this product exists to move.
* **FAILS CLOSED.** The file is parsed and validated IN FULL before a single row is written. A truncated
  or malformed CSV is refused with nothing mutated, because a half-restored leaderboard is worse than a
  refused import somebody retries — the opposite direction from the log WRITER, which fails open so a
  warehouse wobble never takes the game down.
"""
import csv, hashlib, io, json, time

from .logstore import COLUMNS

ALL_COLUMNS = ["event_ts"] + list(COLUMNS)

# Without these a row cannot be attributed to anybody, so it cannot restore progress.
REQUIRED = ("event_type", "user_key")

# ⛔ NEVER IMPORTED, even when a file carries it. `expected_value` is the answer key: the export withholds
#    it deliberately (see queries.export_all), but any pre-fix CSV predates that fix and
#    DO contain it. Importing it would put the answers back into a table the app serves from, quietly
#    undoing a security fix by way of a restore. `verdict` already carries whether the player was right.
NEVER_IMPORT = ("expected_value",)

MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 500_000


class ImportRefused(Exception):
    """Raised before anything is written. The message is shown to the operator verbatim."""


def _die(msg):
    raise ImportRefused(msg)


def synth_event_id(row):
    """A deterministic id for a row that has none, so re-importing the same file stays a no-op."""
    basis = "\x1f".join(str(row.get(c) or "") for c in ALL_COLUMNS if c != "event_id")
    return "syn-" + hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:28]


def parse(raw, source_name="upload.csv"):
    """Parse and FULLY validate a CSV export. Returns a plan; writes nothing. Raises ImportRefused.

    The plan is deliberately the thing that gets inspected and reported, so the caller can show an
    operator what WOULD happen before it happens, and so the refusal reasons are testable without a store.
    """
    if not raw:
        _die("that file is empty — nothing to import.")
    if len(raw) > MAX_BYTES:
        _die(f"that file is {len(raw) // 1048576} MB; the limit is {MAX_BYTES // 1048576} MB.")
    if isinstance(raw, bytes):
        # A CSV saved from Excel often arrives as UTF-8 with a BOM; utf-8-sig eats it if present.
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            _die("that file is not valid UTF-8 text — is it really the exported CSV?")
    else:
        text = raw

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        _die("that file has no header row — nothing to import.")
    header = [(h or "").strip() for h in header]
    if not header or header == [""]:
        _die("that file has no header row — nothing to import.")

    known = [h for h in header if h in ALL_COLUMNS]
    unknown = [h for h in header if h and h not in ALL_COLUMNS]
    missing_required = [c for c in REQUIRED if c not in header]
    if missing_required:
        # ⭐ USER-VISIBLE: an operator reads this after picking the wrong file, so it carries the CURRENT
        #    product name. The three `genie_space_title` copies of the old name are WIRING and stay.
        #    ⚠️ BOTH LANES CHANGED THIS LINE IN THE SAME ROUND AND TO THE SAME STRING — the conflict was the
        #    comment, not the text. Kept both halves: their rename and this note about why it is safe.
        _die(f"this does not look like a Genie Bake-Off log export: it has no "
             f"{', '.join(missing_required)} column. Columns found: {', '.join(header[:12])}"
             f"{'…' if len(header) > 12 else ''}")
    if not known:
        _die("none of that file's columns are log columns — is it a different CSV?")

    width = len(header)
    rows, bad = [], []
    for n, rec in enumerate(reader, start=2):        # start=2: line 1 is the header
        if not rec or (len(rec) == 1 and not (rec[0] or "").strip()):
            continue                                  # blank line, e.g. trailing newline
        if len(rec) != width:
            # ⛔ THE TRUNCATION CHECK. csv.DictReader would pad a short row with None and bucket the
            #    extras under a restkey, i.e. absorb exactly the corruption we are looking for. Comparing
            #    the field count against the header is what makes a cut-off file a refusal rather than a
            #    silently thinner restore.
            bad.append((n, len(rec)))
            if len(bad) > 20:
                break
            continue
        rows.append(dict(zip(header, rec)))
        if len(rows) > MAX_ROWS:
            _die(f"that file has more than {MAX_ROWS:,} rows; export a narrower range.")
    if bad:
        shown = ", ".join(f"line {n} has {c}" for n, c in bad[:5])
        _die(f"that file looks truncated or corrupt: {len(bad)} row(s) do not have {width} fields "
             f"({shown}). Nothing was imported — re-export and try again.")
    if not rows:
        _die("that file has a header but no data rows — nothing to import.")

    # ---- normalise, and classify what this file can and cannot restore -------------------
    has_received = "received_ts" in header
    has_event_id = "event_id" in header
    with_received = 0
    out = []
    for r in rows:
        row = {}
        for c in ALL_COLUMNS:
            if c in NEVER_IMPORT:
                continue
            v = r.get(c)
            if v is not None:
                v = v.strip() if isinstance(v, str) else v
            row[c] = (None if v in ("", None) else v)
        for c in ("week_index", "real_week_index", "points", "latency_ms"):
            if row.get(c) is not None:
                try:
                    row[c] = int(float(row[c]))
                except (TypeError, ValueError):
                    row[c] = None
        if row.get("received_ts"):
            with_received += 1
        # ⛔ received_ts is left as it came — NULL if the file had none. Never filled from event_ts. See
        #    the module docstring: a synthesized value makes an unrecoverable ordering look authoritative.
        if not row.get("event_id"):
            row["event_id"] = synth_event_id(row)
            row["_synthetic_id"] = True
        out.append(row)

    n = len(out)
    if not has_received or with_received == 0:
        ordering = "unrecoverable"
    elif with_received < n:
        ordering = "partial"
    else:
        ordering = "authoritative"

    return {
        "source": source_name,
        "rows": out,
        "row_count": n,
        "columns_used": known,
        "columns_ignored": unknown,
        "columns_withheld": [c for c in NEVER_IMPORT if c in header],
        "has_event_id": has_event_id,
        "synthetic_ids": sum(1 for r in out if r.get("_synthetic_id")),
        "ordering": ordering,
        "rows_with_received_ts": with_received,
        "ordering_note": _ordering_note(ordering, with_received, n),
        "event_types": _counts(out, "event_type"),
        "players": len({r.get("user_key") for r in out if r.get("user_key")}),
    }


def _ordering_note(ordering, with_received, n):
    if ordering == "authoritative":
        return ("Full fidelity: every row carries received_ts, the app clock, so the leaderboard order "
                "and 'who got there first' restore exactly as they were.")
    if ordering == "partial":
        return (f"PARTIAL ORDERING: {with_received} of {n} rows carry received_ts. Points restore "
                f"exactly; for the {n - with_received} row(s) without it the tie-break falls back to the "
                f"warehouse clock, which can reorder players who scored within a few seconds of each "
                f"other. Nothing is invented to cover the gap.")
    return ("ORDERING NOT RECOVERABLE: this file has no received_ts (the app clock) — it predates the "
            "export fix. Every POINT and every unlock restores exactly and the totals will "
            "be right; but the leaderboard TIE-BREAK and 'who cracked it first' fall back to the "
            "warehouse write clock, which has been measured 2.6s out for simultaneous answers and p50 "
            "17.8s behind under load. Players separated by seconds may come back in the wrong order. "
            "This is recorded rather than patched over: re-export from a current build "
            "for a restore that is exact.")


def _counts(rows, field):
    out = {}
    for r in rows:
        k = r.get(field) or "(none)"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def apply_plan(store, plan, operator=None):
    """Write a parsed plan into the store, skipping rows already present. -> result dict.

    Called only after `parse` has accepted the whole file, so this does not validate: by here, either
    every row is writable or nothing should have reached this point.
    """
    rows = plan["rows"]
    ids = [r["event_id"] for r in rows]
    already = store.existing_event_ids(ids)

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    marker = {"src": plan["source"], "at": stamp, "ordering": plan["ordering"]}
    if operator:
        marker["by"] = operator
    fresh = []
    seen = set()
    for r in rows:
        if r["event_id"] in already or r["event_id"] in seen:
            continue
        seen.add(r["event_id"])
        row = {c: r.get(c) for c in ALL_COLUMNS}
        # ⭐ The provenance travels WITH the row, so a figure that came out of a lossy restore can still be
        #    traced to the file it came from long after the import result has been closed. The source's own
        #    `extra` is preserved beside it rather than overwritten — it carries the admin_setting payload.
        try:
            base = json.loads(r.get("extra") or "{}")
            if not isinstance(base, dict):
                base = {"_value": base}
        except (ValueError, TypeError):
            base = {"_value": r.get("extra")} if r.get("extra") else {}
        base["_import"] = marker
        row["extra"] = json.dumps(base)[:4000]
        fresh.append(row)

    written = store.insert_rows(fresh) if fresh else 0
    result = {
        "ok": True,
        "source": plan["source"],
        "rows_in_file": plan["row_count"],
        "written": written,
        "skipped_already_present": len(rows) - len(fresh),
        "ordering": plan["ordering"],
        "ordering_note": plan["ordering_note"],
        "rows_with_received_ts": plan["rows_with_received_ts"],
        "synthetic_ids": plan["synthetic_ids"],
        "columns_ignored": plan["columns_ignored"],
        "columns_withheld": plan["columns_withheld"],
        "event_types": plan["event_types"],
        "players_in_file": plan["players"],
        "at": stamp,
    }
    # A receipt in the log itself, so the fact of a lossy restore outlives this response. Its own
    # event_type is excluded from every player aggregate (see queries.NOT_ADMIN) — a meta row must never
    # count as a person playing, which is the mistake the admin_setting rows already taught this project.
    try:
        store.log("import_receipt",
                  user_key=(operator or "import"), user_email=(operator or None),
                  user_display_name="(log import)",
                  extra={k: result[k] for k in
                         ("source", "rows_in_file", "written", "skipped_already_present",
                          "ordering", "rows_with_received_ts", "synthetic_ids")})
        store.flush()
    except Exception as e:                      # a receipt that fails must not undo a good import
        result["receipt_error"] = f"{type(e).__name__}: {e}"[:200]
    store.invalidate("")
    return result
