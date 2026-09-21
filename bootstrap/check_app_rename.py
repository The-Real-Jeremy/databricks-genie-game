#!/usr/bin/env python3
"""Refuse a deploy that would RENAME the app — because a rename silently destroys it.

    python3 bootstrap/check_app_rename.py <workspace-root-path> <app-name>

⛔ WHAT THIS PREVENTS, IN THE TERMS A CUSTOMER FEELS. `name:` on `databricks_app` is the app's IDENTITY,
not a label. So if this target's bundle state already tracks an app under one name and you deploy with
another, terraform DESTROYS the old app and creates a new one. The deploy is green and says nothing. The
new app gets a NEW SERVICE PRINCIPAL, so the three grants the old one held — USE CATALOG, USE SCHEMA,
SELECT/MODIFY — belong to a principal that no longer exists, and the replacement app then **starts, serves
every page, and records nothing**: no scores, no leaderboard, no clock. That is the same silent outcome the
README calls the worst case for a missing USE CATALOG grant, arriving off a green deploy.

Measured the hard way: two apps deleted on a shared test workspace by an `--var app_name=`
chosen specifically to AVOID touching them.

⭐ THE WHOLE RISK IS A THREE-STATE DISCRIMINATION, AND THE TWO FAILURE DIRECTIONS ARE OPPOSITE:
  * collapse "cannot read the state" toward ALLOW and the guard disarms itself exactly when it cannot see;
  * collapse "no state at all" toward REFUSE and it blocks every clean first install, which is the one
    property that must never break.
So: **no state → ALLOW. Different name → REFUSE. Cannot see → REFUSE.**

⛔ AND ABSENCE IS ONLY EVER CONCLUDED FROM A SUCCESSFUL LISTING OF THE CONTAINER. The CLI reports a missing
path and an unusable profile with the same shape — `exit 1` plus English prose ("Path (…) doesn't exist."
vs "resolve: … has no … profile configured"), with no structured error code in either — and this project has
already been burned by reading a failure's body as data. So the script never decides "absent" by pattern-
matching an error: it walks DOWN from a directory it could actually list, and a listing that fails at any
level is `cannot_see`, not `absent`.
"""
import json
import os
import subprocess
import sys

PROFILE = os.environ.get("DB_PROFILE") or ""


def cli(*args, check=True):
    """-> (returncode, stdout, stderr). Never raises on a non-zero exit; the caller decides."""
    cmd = ["databricks", *args] + (["-p", PROFILE] if PROFILE else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def listing(path):
    """-> (ok, set_of_child_basenames). ok is False when the listing itself could not be done."""
    rc, out, _ = cli("workspace", "list", path, "-o", "json")
    if rc != 0:
        return False, set()
    try:
        d = json.loads(out or "[]")
    except Exception:
        return False, set()
    objs = d if isinstance(d, list) else (d.get("objects") or [])
    return True, {(o.get("path") or "").rstrip("/").rsplit("/", 1)[-1] for o in objs}


def find_state(root):
    """Is there a terraform state for this target?

    -> ("absent", why) | ("present", path) | ("cannot_see", why)

    Walks down from `/Workspace/Users/<me>` so that every ABSENT verdict is backed by a listing that
    SUCCEEDED. A truly fresh workspace has no `.bundle` at all, and that is absent-not-unreadable — which is
    only knowable by having listed the level above it.
    """
    parts = root.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "Workspace":
        return "cannot_see", f"unexpected workspace root {root!r}"
    # start at /Workspace/Users/<user>, the deepest ancestor that must exist if auth works at all
    cur = "/" + "/".join(parts[:3])
    ok, children = listing(cur)
    if not ok:
        return "cannot_see", f"could not list {cur} — auth or permissions, not an empty workspace"
    for nxt in parts[3:] + ["state"]:
        if nxt not in children:
            return "absent", f"{cur}/{nxt} is not there (listing of {cur} succeeded and did not contain it)"
        cur = f"{cur}/{nxt}"
        ok, children = listing(cur)
        if not ok:
            return "cannot_see", f"could not list {cur}"
    if "terraform.tfstate" not in children:
        return "absent", f"no terraform.tfstate in {cur} (listing succeeded)"
    return "present", f"{cur}/terraform.tfstate"


def recorded_app_names(state_path):
    """-> (names, why). `names` is None when the state could not be read or parsed."""
    rc, out, err = cli("workspace", "export", state_path)
    if rc != 0:
        return None, f"could not export {state_path}: {(err or '').strip()[:160]}"
    try:
        d = json.loads(out)
    except Exception as e:
        return None, f"{state_path} is not parseable JSON: {e}"
    names = []
    for res in d.get("resources") or []:
        if res.get("type") != "databricks_app":
            continue
        for inst in res.get("instances") or []:
            nm = (inst.get("attributes") or {}).get("name")
            if nm:
                names.append(nm)
    return names, f"serial={d.get('serial')}"


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    root, want = sys.argv[1], sys.argv[2].strip()
    if not want:
        sys.exit("[rename-guard] no app name given — refusing rather than guessing")

    state, detail = find_state(root)
    if state == "absent":
        print(f"[rename-guard] first deploy of this target ({detail}) — nothing to rename, proceeding")
        return 0
    if state == "cannot_see":
        print(f"[rename-guard] ⛔ REFUSING: I cannot read this target's deployment state, so I cannot tell "
              f"whether this deploy would RENAME and therefore DESTROY an existing app.\n"
              f"  {detail}\n"
              f"  This is deliberately not treated as 'no state': a guard that waves through what it cannot "
              f"see is worse than no guard.\n"
              f"  Fix the access (DATABRICKS_CONFIG_PROFILE / permissions on the workspace path) and retry. "
              f"To proceed without this check, set NIGHT_DESK_ALLOW_APP_RENAME=1 and understand what it "
              f"costs — see below.", file=sys.stderr)
        return 1

    names, why = recorded_app_names(detail)
    if names is None:
        print(f"[rename-guard] ⛔ REFUSING: this target HAS deployment state but I could not read it, so I "
              f"cannot tell whether this deploy would destroy the app it tracks.\n  {why}", file=sys.stderr)
        return 1
    if not names:
        print(f"[rename-guard] state exists but tracks no app yet ({why}) — proceeding")
        return 0
    if want in names:
        print(f"[rename-guard] app name unchanged ({want}) — proceeding")
        return 0

    print(f"[rename-guard] ⛔ REFUSING: this would RENAME the app, which DESTROYS it.\n"
          f"  this target's state tracks: {', '.join(names)}\n"
          f"  this deploy asks for:       {want}\n"
          f"\n"
          f"  WHAT THAT COSTS YOU, and it is not just the app object: the replacement app gets a NEW service\n"
          f"  principal, so the grants the old one held (USE CATALOG, USE SCHEMA, SELECT/MODIFY) belong to a\n"
          f"  principal that no longer exists. The new app then STARTS, SERVES EVERY PAGE, AND RECORDS\n"
          f"  NOTHING — no scores, no leaderboard, no clock — and the deploy that did it reports success.\n"
          f"  Players see a working game that forgets everything.\n"
          f"\n"
          f"  WHAT TO DO INSTEAD, pick one:\n"
          f"    * deploy at the existing name:  --var app_name={names[0]}\n"
          f"    * or use a DIFFERENT TARGET, which has its own state, for a second deployment;\n"
          f"    * or, if you really do intend to replace it, set NIGHT_DESK_ALLOW_APP_RENAME=1 and plan to\n"
          f"      re-issue those three grants to the new service principal afterwards.\n",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    if os.environ.get("NIGHT_DESK_ALLOW_APP_RENAME", "").strip().lower() in ("1", "true", "yes"):
        print("[rename-guard] NIGHT_DESK_ALLOW_APP_RENAME is set — skipping the check. If this deploy "
              "renames the app, the replacement's service principal will need the three grants re-issued "
              "or the app will record nothing.")
        sys.exit(0)
    sys.exit(main())
