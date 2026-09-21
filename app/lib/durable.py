"""DURABLE APP SETTINGS FOR THE NO-CLI DEPLOY — a JSON object in the workspace, written by the app itself.

WHY THIS EXISTS. On the manual (no-DAB) path the person installing this has no CLI and does not edit
files, so the two things they must be able to point at — *which Genie space* and *which table the rows go
to* — have to be settable from the Operator page. Every other setting in this app is a row in the activity
log, and that is exactly what these two cannot be:

⛔ **THE CHICKEN AND EGG.** In local mode the log store is SQLite inside the container and the container is
   ephemeral by design. A pointer saying "the rows live in `cat.sch.tbl`" written *into* that store is lost
   on the next restart, and the app then boots in local mode with no idea where its data went — an empty
   game, with everybody's scores sitting in a table nobody is reading. That is the worst failure this
   product has, and it is silent.

## WHERE IT LIVES, AND WHY THERE (all measured against the live API, not read in a doc)

The app's own **service principal** can read and write the workspace over
`/api/2.0/workspace/{mkdirs,import,export}` using the `DATABRICKS_CLIENT_ID`/`SECRET` that Apps injects
into every container. Measured from inside a running container:

| path | mkdirs | import | export |
|---|---|---|---|
| `/Users/<sp-client-id>/…` — the SP's OWN home | 200 | 200 | 200, byte-identical |
| `/Shared/…` | 200 | 200 | 200, byte-identical |
| the deploying human's folder | **200** | **404** | 404 |

So the authoritative store is the **SP's own home**, and NOT `/Shared`, for one reason: `/Shared` grants
`users: CAN_MANAGE`, i.e. any *player* could rewrite the pointer that decides where the game's rows go.
The SP home is writable by the app and by admins, which is also what makes it auditable — an admin can
see, read and back up a workspace file, and cannot see a file inside a container.

Two alternatives were measured and rejected:
* **the viewer's forwarded token** — the OBO token carries `genie iam.access-control:read
  iam.current-user:read`, and a workspace write with it is `403 Invalid scope, required scopes: workspace`.
  It would cost an extra user-auth scope, an extra consent screen for every player, and it still could not
  be read at boot, when there is no viewer.
* **the container filesystem** — survives a deploy (measured elsewhere) but not a stop/start, and an admin
  cannot see it. "It usually survives" is not a property to keep scores on.

## ⛔ THE POINTER IS TIED TO THE APP'S IDENTITY, WHICH IS WHY THERE IS A SECOND, ADVISORY COPY

`/Users/<sp-client-id>/` is *per service principal*, and a **deleted-and-recreated app gets a new service
principal** (measured: same name, new `service_principal_client_id`). A no-CLI user who removes their app
in the UI and makes it again therefore orphans the pointer — the new app cannot read the old one's home,
and nothing in the primary store can tell "never configured" from "configured by an identity that is
gone".

So every save also writes a **breadcrumb** under `/Shared/<app-name>-state/previous-install.json`, keyed by
the APP NAME, which survives that. It is **advisory only and can never change behaviour**: the app will
not switch storage on it, because `/Shared` is player-writable. Its whole job is to let the Operator page
say *"this app was previously logging to `cat.sch.tbl`; that setting belonged to a service principal that
no longer exists — re-enter it to resume"* instead of presenting an empty game. One store decides, the
other only warns, which is what keeps two copies from becoming two truths.

## STATES, AND THE THIRD ONE THAT MATTERS

`read()` answers **present** / **absent** (404 `RESOURCE_DOES_NOT_EXIST`) / **could-not-determine**
(403, a network error, no credentials). A store that cannot be read must never be reported as empty: empty
means "nothing was ever configured" and would silently start an ephemeral game. Callers get `state` and are
expected to render all three.
"""
import base64, json, os, time, urllib.error, urllib.parse, urllib.request

# One document, one version field. Anything not listed here is ignored on read, so an older container
# reading a newer document degrades to "I do not know that setting" rather than crashing.
KEYS = ("log_table_fqn", "warehouse_id", "genie_space_title", "genie_space_id", "set_by", "set_at",
        "note")
VERSION = 1


class DurableSettings:
    """Reads and writes ONE json document in the workspace, as the app's own service principal."""

    def __init__(self, host, client_id, client_secret, app_name, path=None, token=None, ttl=10.0):
        self.host = (host or "").rstrip("/")
        self.client_id, self.client_secret = client_id, client_secret
        self.app_name = app_name or "genie-bake-off"
        self.dev_token = token            # laptop only: a personal token, same as the log store's
        # ⭐ DERIVED FROM THE SP's OWN ID, never configured. A path someone can set is a path two
        #    deployments can be pointed at by accident, and this document decides where the rows go.
        #    `STATE_PATH` exists only for the workspace that has locked the SP home down.
        self.path = (path or "").strip() or (
            f"/Users/{self.client_id}/{self.app_name}-state/settings.json" if self.client_id else "")
        self.hint_path = f"/Shared/{self.app_name}-state/previous-install.json"
        self.ttl = ttl
        self._cache = None                # (read_at, doc, state, detail)
        self.last_write = None
        self.last_error = None

    # ---------------------------------------------------------------- plumbing
    def available(self):
        return bool(self.host and self.path and (self.dev_token or (self.client_id and self.client_secret)))

    def _token(self):
        if self.dev_token:
            return self.dev_token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            self.host + "/oidc/v1/token", data=b"grant_type=client_credentials&scope=all-apis",
            method="POST", headers={"Authorization": "Basic " + basic,
                                    "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())["access_token"]

    def _call(self, method, path, body=None, timeout=30):
        """-> (status, text). Status FIRST and always: a non-2xx body is not data.

        ⛔ THE TOKEN CALL IS INSIDE THE TRY. It is a network call like any other, and this method runs at
           BOOT — an exception escaping here would take the app down before it served a page, over a
           settings file. Every failure has to come back as a state the caller can render.
        """
        try:
            auth = "Bearer " + self._token()
        except Exception as e:
            return -1, f"could not get a service-principal token: {type(e).__name__}: {e}"
        req = urllib.request.Request(
            self.host + path, data=(json.dumps(body).encode() if body is not None else None),
            method=method, headers={"Authorization": auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()[:600]
        except Exception as e:
            return -1, f"{type(e).__name__}: {e}"

    # ---------------------------------------------------------------- reading
    def read(self, force=False):
        """-> (doc, state, detail). state: present | absent | denied | unavailable | error."""
        if not self.available():
            return {}, "unavailable", ("this container has no service-principal credentials, so there is "
                                       "nowhere durable to keep a setting.")
        if not force and self._cache and time.time() - self._cache[0] < self.ttl:
            return self._cache[1], self._cache[2], self._cache[3]
        st, text = self._call(
            "GET", "/api/2.0/workspace/export?direct_download=true&path=" +
                   urllib.parse.quote(self.path))
        if st == 200:
            try:
                raw = json.loads(text or "{}")
            except Exception as e:
                doc, state, detail = {}, "error", f"the settings file is not valid json ({e})"
            else:
                doc = {k: raw.get(k) for k in KEYS if k in raw}
                state, detail = "present", None
        elif st == 404:
            doc, state, detail = {}, "absent", "nothing has been configured on this deployment yet."
        elif st in (401, 403):
            doc, state, detail = {}, "denied", (f"the app's service principal may not read {self.path} "
                                                f"(HTTP {st}). Settings cannot be kept across a restart "
                                                f"until that is fixed.")
        else:
            doc, state, detail = {}, "error", f"HTTP {st}: {str(text)[:200]}"
        self.last_error = detail if state in ("denied", "error") else None
        self._cache = (time.time(), doc, state, detail)
        return doc, state, detail

    def read_hint(self):
        """The advisory breadcrumb from a PREVIOUS install of this app name. Never used to switch
        anything — see the module docstring. -> (doc, state)."""
        if not self.available():
            return {}, "unavailable"
        st, text = self._call("GET", "/api/2.0/workspace/export?direct_download=true&path=" +
                              urllib.parse.quote(self.hint_path))
        if st != 200:
            return {}, ("absent" if st == 404 else "unreadable")
        try:
            raw = json.loads(text or "{}")
        except Exception:
            return {}, "unreadable"
        return {k: raw.get(k) for k in KEYS if k in raw}, "present"

    # ---------------------------------------------------------------- writing
    def write(self, updates, viewer=None):
        """Merge `updates` into the document and store it. A key whose value is None is REMOVED, which is
        how an operator clears a setting; a key simply absent from `updates` is left alone.

        -> (ok, detail). Never raises: a settings write that throws would take the operator page down.
        """
        if not self.available():
            return False, ("this container has no service-principal credentials, so a setting cannot be "
                           "made to survive a restart")
        doc, state, detail = self.read(force=True)
        if state in ("denied", "error"):
            return False, detail
        merged = dict(doc)
        for k, v in (updates or {}).items():
            if k not in KEYS:
                continue
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        merged["set_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if viewer:
            merged["set_by"] = viewer.get("email") or viewer.get("name") or viewer.get("user_key")
        payload = dict(merged)
        payload["_version"] = VERSION
        payload["_app_name"] = self.app_name
        body = json.dumps(payload, indent=1, sort_keys=True)
        ok, why = self._put(self.path, body)
        if not ok:
            self.last_error = why
            return False, why
        self.last_write = merged["set_at"]
        self._cache = (time.time(), {k: v for k, v in merged.items() if k in KEYS}, "present", None)
        # Best effort, and deliberately not fatal: the breadcrumb only makes a future failure legible.
        self._put(self.hint_path, body)
        return True, None

    def _put(self, path, body):
        parent = path.rstrip("/").rsplit("/", 1)[0]
        self._call("POST", "/api/2.0/workspace/mkdirs", {"path": parent})
        st, text = self._call("POST", "/api/2.0/workspace/import", {
            "path": path, "format": "AUTO", "overwrite": True,
            "content": base64.b64encode(body.encode()).decode()})
        # ⛔ `mkdirs` answers 200 on a directory this principal cannot write to, and the import then
        #    answers 404 RESOURCE_DOES_NOT_EXIST — a denial wearing a 404, after a success that meant
        #    nothing. So the import's own status is the only thing read here, and a 404 is reported as a
        #    permission problem rather than as "missing", because the parent was just created.
        if st == 200:
            return True, None
        if st == 404:
            return False, (f"could not write {path} (HTTP 404 after creating the folder, which is how "
                           f"this API reports 'not allowed'). The app's service principal cannot write "
                           f"there.")
        return False, f"could not write {path} (HTTP {st}): {str(text)[:200]}"

    def public(self, include_hint=True):
        """What the operator page and /api/health show. Never the credentials, always the state."""
        doc, state, detail = self.read()
        out = {"path": self.path, "state": state, "detail": detail,
               "writable_hint_path": self.hint_path,
               "set_at": doc.get("set_at"), "set_by": doc.get("set_by"),
               "has_log_table": bool(doc.get("log_table_fqn")),
               "has_genie_space": bool(doc.get("genie_space_title") or doc.get("genie_space_id"))}
        if include_hint and state == "absent":
            hint, hstate = self.read_hint()
            # ⭐ THE LOUD CASE. A previous install of this app name configured a table; this identity
            #    cannot read that setting. Reported as its own state so the page can shout rather than
            #    quietly showing an empty local-mode game.
            if hstate == "present" and (hint.get("log_table_fqn") or hint.get("genie_space_title")):
                out["previous_install"] = hint
        return out
