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
        self.warehouse_id = e.get("WAREHOUSE_ID") or ""
        self.log_table = e.get("LOG_TABLE_FQN") or ""
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
        """What the operator page says about where rows are going."""
        if self.storage_mode == "local":
            return {"mode": "local", "target": self.local_db_path, "table": self.local_db_table,
                    "ephemeral": True}
        return {"mode": "delta", "target": self.log_table, "warehouse_id": self.warehouse_id,
                "ephemeral": False}

    def public(self):
        return {"build": self.build, "season_id": self.season_id,
                "timezone": self.timezone, "theme": self.theme, "alt_theme": self.alt_theme,
                "past_weeks_open": self.past_weeks_open, "storage_mode": self.storage_mode,
                # Whether a password is REQUIRED, never anything about what it is. The client needs this
                # to know whether to draw the unlock form at all.
                "operator_password_required": self.operator_password_set}
