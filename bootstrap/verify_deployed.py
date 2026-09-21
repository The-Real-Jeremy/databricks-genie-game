#!/usr/bin/env python3
"""Ask the RUNNING app whether it actually got its configuration, and fail the deploy if it did not.

⛔ WHY THIS EXISTS, and it is the same lesson this project keeps paying for. Both deployed
apps ran for hours with no environment at all. Every signal available said otherwise:
  * `bundle validate` — passed, all twelve variables resolved;
  * `bundle deploy` — exit 0, "Deployment complete!";
  * `bundle run` — "App started successfully";
  * `databricks apps get` — app_status RUNNING, compute ACTIVE, active_deployment SUCCEEDED;
  * every page and every static asset — served correctly, md5-identical to disk.
And the log tables sat at zero rows, which was read as proof of a clean reset rather than as the symptom
it was. One observable, two causes, and the reassuring one got picked.

The only witness that cannot lie about the container's environment is the container. So the deploy is not
finished until the app's own /api/health says so, and a non-zero exit here fails the deploy.
"""
import hashlib, json, os, subprocess, sys, time, urllib.error, urllib.request

PROFILE = os.environ.get("DB_PROFILE") or ""
SYNCED_DIR = os.environ.get("SYNCED_SOURCE_DIR") or ""


def token():
    cmd = ["databricks", "auth", "token"] + (["-p", PROFILE] if PROFILE else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"[verify] could not mint a token: {r.stderr[:300]}")
    return json.loads(r.stdout)["access_token"]


def app_source_path(name):
    """Where the ACTIVE DEPLOYMENT was made from. This, not a guess, is what "synced" means."""
    cmd = ["databricks", "apps", "get", name, "-o", "json"] + (["-p", PROFILE] if PROFILE else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    d = json.loads(r.stdout or "{}")
    return ((d.get("active_deployment") or {}).get("source_code_path") or "").rstrip("/")


def app_url(name):
    cmd = ["databricks", "apps", "get", name, "-o", "json"] + (["-p", PROFILE] if PROFILE else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"[verify] could not read the app: {r.stderr[:300]}")
    d = json.loads(r.stdout)
    url = d.get("url")
    if not url:
        raise SystemExit("[verify] the app has no url")
    return url.rstrip("/")


def health(url, tok):
    req = urllib.request.Request(url + "/api/health", headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"_body": e.read().decode()[:200]}
    except Exception as e:
        return -1, {"_error": f"{type(e).__name__}: {e}"}


# ⛔ A LIMIT ONLY LIMITS THE AXIS IT MEASURES, and this file learned that the hard way TWICE IN ONE DAY.
#    It was written to stop a deploy shipping a blank CONFIG — which it does. It was then reported as
#    passing on a deploy that had shipped stale CODE, because the config was fine and nothing here looked
#    at the code at all. That is the same shape as the green deploy it was built to catch, one layer along:
#    the gate built to stop a deploy lying was itself blind to another way of lying.
#    So it now asserts CODE IDENTITY as well: the bytes the deployment serves must be the bytes in the tree
#    that was just deployed. Byte equality is the claim; a build id would only prove a restart.
# ⭐ THE STRONGEST REFERENCE IS THE SYNCED ARTEFACT, NOT HEAD — BT-B's method note, and it is sharper than
#    comparing against the local tree, which is what the first version did:
#      * `bundle deploy` ships the WORKING TREE, so a deployment can be legitimately current while
#        DIFFERING from HEAD — and a verifier comparing served against the local tree raises a FALSE ALARM
#        the moment the next round of edits begins. That false alarm cost twenty minutes tonight.
#      * and HEAD can match while a later sync shipped something else entirely.
#    They are two separate questions, so they are asked separately:
#      SERVED == SYNCED   → "is the container running what was deployed"   (the gate; failure blocks)
#      SYNCED == TREE     → "which code got deployed"                      (reported, and named)
#    The FILE COUNT is asserted rather than an exit code, because `workspace export` exits 0 on nothing.
#    app.py and lib/ are never served, so the container reports its own `server_md5` and that is compared
#    against the synced source — otherwise a verifier proves the browser has the right CSS while the server
#    runs last week's logic.
STATIC = (("/", "app/static/index.html"), ("/app.js", "app/static/app.js"),
          ("/app.css", "app/static/app.css"), ("/sprites.js", "app/static/sprites.js"))
SERVER_SRC = ("app.py",)          # plus everything under app/lib, discovered below


def _md5(b):
    return hashlib.md5(b).hexdigest()


def synced_bytes(remote_dir, rel):
    """Read one file back out of the workspace path the deployment was made from."""
    cmd = ["databricks", "workspace", "export", f"{remote_dir}/{rel}"] + (["-p", PROFILE] if PROFILE else [])
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        return None
    return r.stdout


def code_identity(url, tok, remote_dir, root):
    """-> (ok, lines). SERVED == SYNCED for the assets, plus the container's server_md5 == SYNCED source."""
    lines, ok = [], True
    checked = 0
    for path, local in STATIC:
        rel = local[len("app/"):]                       # e.g. static/app.js inside the app source dir
        want = synced_bytes(remote_dir, rel)
        if want is None:
            lines.append(f"{path}: could NOT read the synced copy at {remote_dir}/{rel}")
            ok = False
            continue
        req = urllib.request.Request(url + path, headers={"Authorization": "Bearer " + tok})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                got, status = r.read(), r.status
        except Exception as e:
            lines.append(f"{path}: could not fetch ({type(e).__name__})")
            ok = False
            continue
        checked += 1
        if status != 200:
            lines.append(f"{path}: HTTP {status}")
            ok = False
        elif _md5(got) != _md5(want):
            lines.append(f"{path}: SERVED {_md5(got)[:8]} != SYNCED {_md5(want)[:8]}")
            ok = False
        else:
            lines.append(f"{path}: served == synced ({_md5(got)[:8]})")
        # and say whether the synced copy is the local tree, without making it a gate
        try:
            with open(os.path.join(root, local), "rb") as f:
                tree = f.read()
            if _md5(tree) != _md5(want):
                lines.append(f"{path}: note — the synced copy differs from the LOCAL TREE "
                             f"(tree {_md5(tree)[:8]}); expected while a later round is in progress")
        except OSError:
            pass
    # ⛔ COUNT, do not trust: `workspace export` exits 0 on a missing file in some versions.
    if checked != len(STATIC):
        lines.append(f"only {checked} of {len(STATIC)} assets were actually compared")
        ok = False
    return ok, lines


def server_identity(url, tok, remote_dir, body):
    """The half that is never served: does the container's own server fingerprint match the synced source?"""
    reported = body.get("server_md5")
    if not reported:
        return False, ["the app does not report a server_md5, so its server code cannot be identified"]
    h = hashlib.md5()
    rels = list(SERVER_SRC)
    out = subprocess.run(["databricks", "workspace", "list", f"{remote_dir}/lib", "-o", "json"]
                         + (["-p", PROFILE] if PROFILE else []), capture_output=True, text=True)
    try:
        libs = sorted(os.path.basename(o["path"]) for o in json.loads(out.stdout or "[]")
                      if o.get("path", "").endswith(".py"))
    except Exception:
        libs = []
    if not libs:
        return False, [f"could not list {remote_dir}/lib, so the server source cannot be hashed"]
    rels += ["lib/" + n for n in libs]
    for rel in rels:
        b = synced_bytes(remote_dir, rel)
        if b is None:
            return False, [f"could not read the synced {rel}"]
        h.update(rel.encode())
        h.update(b)
    synced = h.hexdigest()
    if synced == reported:
        return True, [f"server code: container {reported[:8]} == synced ({len(rels)} files)"]
    return False, [f"server code: the container reports {reported[:8]} but the SYNCED source hashes to "
                   f"{synced[:8]} over {len(rels)} files — the container is running different server code "
                   f"from the one that was just deployed"]


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: verify_deployed.py <app_name> <expected_build_id> [synced_source_dir]")
    name, want_build = sys.argv[1], sys.argv[2]
    global SYNCED_DIR
    if len(sys.argv) > 3:
        SYNCED_DIR = sys.argv[3].rstrip("/")
    url, tok = app_url(name), token()
    if not SYNCED_DIR:
        SYNCED_DIR_LOCAL = app_source_path(name)
        if SYNCED_DIR_LOCAL:
            globals()["SYNCED_DIR"] = SYNCED_DIR_LOCAL
    print(f"[verify] asking {url}/api/health whether the container really got its config")

    # A container that has just been (re)deployed may take a few seconds to answer. Distinguish
    # NOT-YET-UP from UP-BUT-WRONG: only the second one is a deploy failure worth reporting as one.
    last = None
    for attempt in range(12):
        st, body = health(url, tok)
        last = (st, body)
        if st == 200:
            break
        time.sleep(5)
    st, body = last
    if st != 200:
        raise SystemExit(f"[verify] FAILED — /api/health did not answer 200 after 60s: {st} "
                         f"{json.dumps(body)[:250]}")

    problems = []
    missing = body.get("missing_config") or []
    if missing:
        problems.append(f"the container is MISSING {', '.join(missing)} — app.yaml did not reach it")
    lw = body.get("log_writer")
    if lw == "not configured" or not isinstance(lw, dict):
        problems.append(f"the log writer is not configured ({lw!r}) — nothing will be recorded, so there "
                        f"is no activation metric, no scoring and no clock")
    elif lw.get("thread_alive") is not True:
        problems.append(f"the log writer thread is not alive: {json.dumps(lw)[:120]}")
    if body.get("build") != want_build:
        problems.append(f"BUILD_ID is {body.get('build')!r}, expected {want_build!r} — so the env this "
                        f"deploy generated is not the env the container is running")
    if body.get("ok") is not True:
        problems.append(f"the app reports ok={body.get('ok')!r}")
    if not body.get("weeks"):
        problems.append("the app reports no weeks, so no content pack loaded")

    # ── ⭐ THE STORAGE MODE THE CONTAINER IS ACTUALLY IN, not the one the bundle asked for.
    #    The two can differ without anything erroring: STORAGE_MODE reaches the container only through the
    #    generated app.yaml, which is the exact file that has already gone missing twice here (the sync
    #    honours .gitignore). A delta-mode deploy that silently came up in local mode would look perfect —
    #    it serves, it scores, it has a leaderboard — and would be writing every row to a container disk
    #    that the next redeploy erases. That is the most expensive failure this app has available, and
    #    nothing else in this gate would catch it, because a working SQLite store IS a working store.
    want_mode = (os.environ.get("STORAGE_MODE") or "delta").strip().lower()
    if want_mode in ("sqlite", "local-sqlite", "localdb", "local_db"):
        want_mode = "local"
    got = body.get("storage") or {}
    got_mode = (got.get("mode") or "").strip().lower()
    if not got_mode:
        problems.append("the app's /api/health reports no storage mode at all — it is running code older "
                        "than the two-mode change, so the bundle and the container disagree by definition")
    elif got_mode != want_mode:
        problems.append(f"STORAGE MODE MISMATCH: this deploy asked for {want_mode!r} and the container is "
                        f"running {got_mode!r} (target {got.get('target')!r}). In local mode every row "
                        f"goes to a container disk the next redeploy erases.")
    else:
        print(f"[verify]    storage · mode={got_mode} target={got.get('target')!r} "
              f"ephemeral={got.get('ephemeral')}")
        if got_mode == "local":
            print("[verify]    storage · ⚠️  LOCAL MODE IS EPHEMERAL — a restart or redeploy loses the "
                  "activity log. Export from the Operator page to keep it.")

    # ── CODE IDENTITY, against the SYNCED ARTEFACT. Retried, because a container that has just restarted
    #    can serve the previous deployment's assets for a few seconds — a TIMING difference, not a stale
    #    deploy, and reporting it as one would make this gate cry wolf.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    remote = (body.get("source") or {}).get("path") or SYNCED_DIR
    code_ok, lines, srv_ok, srv_lines = False, [], False, []
    for attempt in range(10):
        code_ok, lines = code_identity(url, tok, remote, root)
        srv_ok, srv_lines = server_identity(url, tok, remote, body)
        if code_ok and srv_ok:
            break
        time.sleep(6)
        st2, b2 = health(url, tok)
        if st2 == 200:
            body = b2
    for l in lines + srv_lines:
        print(f"[verify]    code · {l}")
    if not (code_ok and srv_ok):
        problems.append("THE CONTAINER IS NOT RUNNING WHAT WAS JUST DEPLOYED — see the code lines above. "
                        "The config being correct says nothing about which code is running.")

    # If the config is missing, say WHICH LINK broke — the file, the upload, or the container — because
    # "app.yaml did not reach it" was true twice today for two completely different reasons.
    if missing:
        import glob
        local = os.path.join("app", "app.yaml")
        problems.append("local app/app.yaml " + ("EXISTS" if os.path.exists(local) else "IS ABSENT")
                        + " — if it exists, the upload is the broken link (check `sync.include`, because "
                          "the bundle sync honours .gitignore); if it is absent, the predeploy hook did "
                          "not run")

    if problems:
        # The header used to say "THE APP IS NOT CONFIGURED" whatever the problem was, which is itself a
        # confident wrong message once this checks two different axes.
        print("[verify] ⛔ THE DEPLOY LOOKED GREEN AND THE RUNNING APP DISAGREES:")
        for p in problems:
            print(f"[verify]    - {p}")
        raise SystemExit(1)

    print(f"[verify] OK build={body['build']!r} weeks={body.get('weeks')} blanks={body.get('blanks')} "
          f"· log_writer alive · no missing config · served == SYNCED and the server code matches it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
