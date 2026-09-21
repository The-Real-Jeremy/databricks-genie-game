# `deploy/manual/` — the no-CLI install kit

The instructions are in the main **README**, under *"No bundle, no CLI: upload the repo and point an app
at it"*. This directory holds the one file that route cannot get from the UI.

* **`create-app.json`** — the body for `databricks apps create --json @deploy/manual/create-app.json`, for
  somebody who has the CLI but not bundles. The UI's own create form can set the `genie` user-auth scope, so
  a UI install does not need this file at all.

## ⛔ WHERE `app.yaml` WENT

There used to be two files here — `app.yaml` and `app.delta.yaml` — and the instructions told you to copy
one of them over `app/app.yaml` before uploading. Both are gone, and the replacement is that
**`app/app.yaml` is now committed**, in local-database mode, working as shipped.

Two things made the copy step wrong rather than merely tedious:

1. **The manual install is now a ZIP.** You download the repo, upload it, and point an app at the `app`
   folder. There is no step in that flow where a file gets copied, and a zip with no `app.yaml` produces an
   app that starts with no command and no environment — which reads as broken code rather than as a missing
   file.
2. **The delta/local choice is no longer a file choice.** A manual install always starts in local mode and
   is pointed at a Unity Catalog table afterwards, from the app's own Operator page, which stores that
   setting in the workspace. Two `app.yaml` variants meant two sources of truth for one setting, and the
   one nobody was using was free to drift.
