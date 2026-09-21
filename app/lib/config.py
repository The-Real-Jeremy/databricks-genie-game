"""Configuration, all from environment (app.yaml), with the container's quirks handled once."""
import os

# The operator password used when a deployment does not set one. NOT a secret: it is in the repo, so it is
# public to anyone who can read the repo or the workspace. See the block in Config.__init__ for why that is
# the deliberate trade, and README "The operator password" for how to override it.
DEFAULT_OPERATOR_PASSWORD = "bake-off"

def _host():
    """`DATABRICKS_HOST` inside an Apps container has NO scheme, while the same variable from a local
    profile does. urllib raises on the bare host, and it surfaces as a vague degraded feature."""
    h = (os.environ.get("DATABRICKS_HOST") or "").strip().rstrip("/")
    if not h:
        return ""
    return h if h.startswith("http") else "https://" + h


class Config:
    def __init__(self, env=None):
        e = env or os.environ
        self.host = _host()
        self.port = int(e.get("DATABRICKS_APP_PORT") or e.get("PORT") or 8000)
        self.build = e.get("BUILD_ID") or "dev"
        self.client_id = e.get("DATABRICKS_CLIENT_ID")
        self.client_secret = e.get("DATABRICKS_CLIENT_SECRET")
        self.app_name = e.get("DATABRICKS_APP_NAME") or ""
        self.warehouse_id = e.get("WAREHOUSE_ID") or ""
        self.log_table = e.get("LOG_TABLE_FQN") or ""
        # ── WHAT THE ENVIRONMENT PINNED, kept separately from what is in force ──────────────────
        # On the no-CLI path an operator can point the app at a table and a Genie space from the Operator
        # page, and those settings are stored in the workspace (see lib/durable.py). `apply_overrides`
        # below folds them in. These two fields remember what app.yaml itself said, because "the
        # environment pinned this" is the reason the Operator page gives for disabling its own box — and a
        # UI that silently ignores what somebody typed is worse than one that will not accept it.
        self.log_table_env = self.log_table
        self.warehouse_id_env = self.warehouse_id
        self.state_path = (e.get("STATE_PATH") or "").strip()
        # Provenance for every resolved value, so /api/health and the Operator page can say where the
        # setting came from rather than only what it is.
        self.sources = {"log_table": ("env" if self.log_table else "unset"),
                        "warehouse_id": ("env" if self.warehouse_id else "unset"),
                        "genie_space": "env"}
        # ── STORAGE MODE ────────────────────────────────────────────────
        # "delta" writes to the target table as before; "local" writes to a SQLite file and needs no
        # warehouse, no catalog grant and no service-principal secret. Delta stays the DEFAULT so an
        # existing deployment that sets none of this keeps behaving exactly as it did.
        #
        # ⭐ AN UNRECOGNISED VALUE IS A HARD ERROR, NOT A FALLBACK TO DELTA. A typo'd STORAGE_MODE=sqlite
        #    silently falling back would deploy an app that looks configured, needs a warehouse nobody
        #    granted, and reports itself half-configured for a reason no one connects to the typo.
        mode = (e.get("STORAGE_MODE") or "delta").strip().lower()
        if mode in ("sqlite", "local-sqlite", "localdb", "local_db"):
            mode = "local"          # the spellings someone will actually type
        if mode not in ("delta", "local"):
            raise ValueError(f"STORAGE_MODE must be 'delta' or 'local', got {e.get('STORAGE_MODE')!r}")
        self.storage_mode = mode
        # What app.yaml declared, kept for the whole life of the process. `storage_mode` is the mode in
        # FORCE and can change once (see apply_overrides); this one never does, so a deploy verifier can
        # still assert what the deployment asked for.
        self.storage_mode_declared = mode
        # Default under /tmp because that is the one path writable in every Apps container. It is also
        # ephemeral — see the note in localstore.py; point this at a volume if one exists.
        self.local_db_path = e.get("LOCAL_DB_PATH") or "/tmp/night_desk/night_desk.db"
        self.local_db_table = e.get("LOCAL_DB_TABLE") or "activity_log"
        self.genie_space_title = e.get("GENIE_SPACE_TITLE") or "The Data Desk"
        # Must match SPACE_SUFFIX in the bootstrap, or the app looks for a space nobody created.
        # Do NOT strip the value: a leading space is meaningful (" (dev)"), and stripping it would
        # silently point the app at a different space title than the bootstrap created.
        suffix = e.get("GENIE_SPACE_SUFFIX") or ""
        self.genie_space_suffix = "" if suffix.strip().lower() in ("", "none") else suffix
        self.genie_space_id = e.get("GENIE_SPACE_ID") or ""
        self.season_id = e.get("SEASON_ID") or "s1_letters"
        # Which scenarios this DEPLOYMENT actually has a Genie space for. The bootstrap only builds a
        # space per scenario named in the bundle's `seasons` variable, so offering any other scenario in
        # Settings points the game at a space nobody created — which fails only when someone switches.
        self.seasons = [x.strip() for x in (e.get("SEASONS") or "").split(",") if x.strip()]
        self.timezone = e.get("SEASON_TZ") or "Australia/Sydney"
        self.tz_offset_hours = float(e.get("SEASON_TZ_OFFSET_HOURS") or 10)
        self.past_weeks_open = (e.get("PAST_WEEKS_OPEN") or "1") not in ("0", "false", "False")
        self.theme = e.get("THEME") or "databricks"
        self.alt_theme = e.get("ALT_THEME") or "neutral"
        # ── THE ADDRESS ALLOWLIST IS GONE ─────────────────────────────────────────
        # THE REQUIREMENT: no operator email list. Anyone with the password may act as operator, and
        # operator access does not need to be strict, because this is a game.
        # So there is no `operator_emails` any more — one lock, not two.
        #
        # ⚠️ WHAT THAT COSTS, recorded here because it is a real consequence of a deliberate choice and not
        #    an oversight. The allowlist used to be checked BEFORE the password, which stopped the unlock
        #    endpoint being a password ORACLE for anyone with a workspace account;  removed the attempt
        #    limiter as well, so the operator page is now guarded by ONE shared secret with UNLIMITED
        #    guesses. That page releases weeks for everybody, shows the activity log, and since  can
        #    RESET EVERY PLAYER'S PROGRESS. The reset keeps its own protections (a verified backup first, an
        #    exact confirmation, and a logged row naming who pressed it), which is what makes this
        #    acceptable for a game rather than merely convenient.
        self.allow_date_override = (e.get("ALLOW_DATE_OVERRIDE") or "0") not in ("0", "false", "False")
        # ── THE OPERATOR PASSWORD ──────────────────────────
        # `none` is the explicit "not set" sentinel, because the Apps API refuses an env entry with an EMPTY
        # value and fails AFTER the app has started, so "unset" cannot be expressed as "".
        #
        # ⚠️ WHAT `none` MEANS HAS CHANGED, and the history matters because the old behaviour was chosen for
        #    a reason that still holds elsewhere. It used to mean THE OPERATOR PAGE IS CLOSED: closed is
        #    discoverable (a wall that names the variable to set) where open-when-unset is the SILENT
        #    failure, and an open gate let a second identity download another player's answers.
        #    Both of those facts are still true. What changed is that `none` now falls back to a
        #    WORKING DEFAULT PASSWORD rather than to either extreme — so the page is neither silently open
        #    nor uselessly walled, and the answer-key leak stays fixed at its real root (no query projects
        #    `expected_value`) rather than depending on the gate.
        # ⭐ AND A PASSWORD ALWAYS DEPLOYS, by requirement: a default password is always deployed.
        #    `none` no longer means CLOSED, it means FALL BACK TO THE BUILT-IN DEFAULT — so a
        #    fresh workspace, a no-DAB upload that never edited app.yaml, and a bundle deploy with no
        #    variables all come up with a WORKING operator page instead of a wall.
        #    It lives HERE rather than in databricks.yml on purpose: config.py is the one place every deploy
        #    path passes through, including the manual `cp deploy/manual/app.yaml` route where nobody edits
        #    anything. A default in the bundle would have helped only the bundle.
        # ⛔ AND IT IS PUBLIC BY CONSTRUCTION. A default that always deploys is a default anyone who can read
        #    this repo can read. It gates PLAYERS — it is not a secret from anyone with workspace or repo
        #    access, and the README says so in those words. Pass `--var operator_password=…` (or
        #    OPERATOR_PASSWORD in the env, or ~/.night-desk/operator_password) to override it per deployment.
        raw_pw = (e.get("OPERATOR_PASSWORD") or "").strip()
        self.operator_password = (DEFAULT_OPERATOR_PASSWORD if raw_pw.lower() in ("", "none", "-")
                                  else raw_pw)
        self.operator_password_is_default = self.operator_password == DEFAULT_OPERATOR_PASSWORD

        self.in_databricks = bool(e.get("DATABRICKS_APP_NAME"))

    # ── FOLDING IN WHAT AN OPERATOR SET AT RUNTIME ───────────────────────────────────────────────
    def apply_overrides(self, doc):
        """Merge the durable settings document (lib/durable.py) into this config, ONCE, at boot — and
        again when an operator saves. -> a dict of what changed, for the log line.

        ⭐ THE PRECEDENCE IS DELIBERATELY NOT THE SAME FOR BOTH SETTINGS, and the asymmetry is the whole
        design, so it is written here rather than left to be inferred:

        * **The TABLE: app.yaml WINS.** `LOG_TABLE_FQN` in the environment is a deploy-time commitment about
          where a customer's data lands. If a stored setting could override it, a bundle deploy that names a
          table would keep serving while writing somewhere else, a redeploy would not fix it, and the rows
          already written would be in a table nobody reads. So when the environment names a table, the
          Operator page DISABLES its box and says why. Nothing is ignored silently.
        * **The GENIE SPACE: the stored setting WINS.** This is the one setting a manual install exists to let
          an operator point at afterwards, `GENIE_SPACE_TITLE` always has a value
          (its default is the shipped title), and being wrong costs a failed question rather than a lost
          row — resolution is lazy and by title, so a bad value is visible on the first question and
          reversible with one Clear. An "env wins" rule here would make the box permanently inert.
        """
        doc = doc or {}
        changed = {}
        tbl = (doc.get("log_table_fqn") or "").strip()
        if tbl and not self.log_table_env:
            self.log_table, self.sources["log_table"] = tbl, "operator"
            changed["log_table"] = tbl
        elif tbl and self.log_table_env and tbl != self.log_table_env:
            # Both name a table and they disagree. The environment is in force; this is surfaced rather
            # than resolved, because it is a person's mistake to see and fix, not a tie to break quietly.
            self.log_table_conflict = tbl
            changed["log_table_conflict"] = tbl
        wh = (doc.get("warehouse_id") or "").strip()
        if wh and not self.warehouse_id_env:
            self.warehouse_id, self.sources["warehouse_id"] = wh, "operator"
            changed["warehouse_id"] = wh
        sid = (doc.get("genie_space_id") or "").strip()
        title = (doc.get("genie_space_title") or "").strip()
        if sid or title:
            self.genie_space_id = sid
            if title:
                self.genie_space_title = title
                # A stored title is a whole title. The suffix exists so two BUNDLE targets in one workspace
                # get their own spaces; applying it to a name somebody typed would look for a space they
                # never made.
                self.genie_space_suffix = ""
            self.sources["genie_space"] = "operator"
            changed["genie_space"] = sid or title
        # The mode in force is DERIVED, never stored: delta exactly when there is somewhere to write and a
        # warehouse to write through. A declared `delta` with no table stays delta so `missing()` keeps
        # reporting it — that deployment is broken and must keep saying so.
        if self.storage_mode_declared == "local" and self.log_table and self.warehouse_id and \
                (self.client_id or not self.in_databricks):
            self.storage_mode = "delta"
            changed["storage_mode"] = "local -> delta"
        return changed

    @property
    def operator_password_set(self):
        return bool(self.operator_password)

    def unlock_hint(self):
        """What a would-be operator is told when no password is configured. Names the fix, never the value.

        ⚠️ UNREACHABLE AS SHIPPED, and kept deliberately. `operator_password` falls back to the
        built-in default, so `operator_password_set` is always true and this branch cannot fire. It is
        the message a build that removed that fallback would need, and it is the only record of which
        direction this gate is supposed to fail in.
        """
        if self.operator_password_set:
            return ""
        return ("The operator page is closed because no password is configured for this deployment. "
                "Set the bundle variable `operator_password` (or the OPERATOR_PASSWORD env var in "
                "app.yaml) and redeploy. It is closed rather than open on purpose — see the README.")

    def missing(self):
        """What is not configured — surfaced by /api/health so a half-configured app says so.

        ⭐ MODE-AWARE, because the whole point of local mode is that the Delta prerequisites do not apply.
        Reporting WAREHOUSE_ID and LOG_TABLE_FQN as "missing" in local mode would make a correctly
        configured deployment permanently describe itself as broken — and the health endpoint is exactly
        what someone checks to decide whether it is.
        """
        out = []
        need = [("DATABRICKS_HOST", self.host)]
        # Deliberately NOT in `missing()`: an unset password is not a broken deployment. It falls back
        # to the built-in default above, so the gate still works; reporting it as missing config would
        # make /api/health say ok=False and fail the deploy verifier over a gate that is fine.
        # It is surfaced separately as `operator_password_set` so it is visible without being fatal.
        if self.storage_mode == "delta":
            need += [("WAREHOUSE_ID", self.warehouse_id), ("LOG_TABLE_FQN", self.log_table),
                     ("DATABRICKS_CLIENT_ID", self.client_id),
                     ("DATABRICKS_CLIENT_SECRET", self.client_secret)]
        for name, val in need:
            if not val:
                out.append(name)
        return out

    def storage_public(self):
        """What the operator page says about where rows are going.

        `mode` is the mode IN FORCE and stays the key a deploy verifier reads. `declared` is what app.yaml
        asked for, and the two differ exactly when an operator has pointed a manual install at a table.
        """
        common = {"declared": self.storage_mode_declared,
                  "source": self.sources.get("log_table"),
                  "pinned_by_env": bool(self.log_table_env),
                  "conflict_with_env": getattr(self, "log_table_conflict", None)}
        if self.storage_mode == "local":
            return {"mode": "local", "target": self.local_db_path, "table": self.local_db_table,
                    "ephemeral": True, **common}
        return {"mode": "delta", "target": self.log_table, "warehouse_id": self.warehouse_id,
                "ephemeral": False, **common}

    def public(self):
        return {"build": self.build, "season_id": self.season_id,
                "timezone": self.timezone, "theme": self.theme, "alt_theme": self.alt_theme,
                "past_weeks_open": self.past_weeks_open, "storage_mode": self.storage_mode,
                # Whether a password is REQUIRED, never anything about what it is. The client needs this
                # to know whether to draw the unlock form at all.
                "operator_password_required": self.operator_password_set}
