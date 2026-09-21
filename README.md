# The Genie Bake-Off

A game that gets people asking questions of a Databricks Genie space, deployed by **one bundle command**.

**The app makes no external request of any kind** — no CDN, no webfont, no analytics, standard-library
Python only — so it runs happily in an air-gapped workspace. Note that the bundle install itself is
*not* offline: `databricks bundle deploy` drives Terraform, and the CLI downloads the Terraform
binary and the databricks provider on a first run. To install with no route to the internet, use the
no-DAB path in `deploy/manual/` (which needs no Terraform), or pre-seed the CLI's Terraform cache.

**Four weeks. Each week is a memo with five blanks in it.** A note to a CFO about a cookie chain's books,
a shelf review for the buying team, an expansion paper for the board, a concentration check for the
auditor. Every blank is a number or a name sitting in the data. The player asks a **Genie space** on the
right in plain English — always as themselves, never as the app — reads what comes back, and types the
value into the blank on the left. Right answers lock **green** and cannot be edited; wrong ones go **red**
and stay editable, with a nudge that names the classic mistake. Fill all five and the memo is ready to send.

An **admin releases** a week for everybody from the Operator screen; there is no schedule. Each player
then **clicks to unlock** it, and that click starts **their own clock** — which **pauses** whenever they
are not looking at that week.

**Scoring, in one line:** a blank is worth **100**, minus **5** for every whole minute that week has been
open in front of you, never below **40**. Taking a **hint** costs **5**, once per blank however many
times you reopen it. Wrong answers cost nothing, ever.

---

## Deploy

There are **three** ways in, and the first two are one command each. Pick by what your workspace will
let you have:

| | needs | activity log | survives a redeploy |
|---|---|---|---|
| **A. Delta mode** (default) | a warehouse + a catalog you can create a schema in | Unity Catalog table | **yes** |
| **B. Local mode** | a warehouse only | SQLite file in the app container | **no** — export/import instead |
| **C. No bundle, no CLI** | the UI and a SQL editor: upload a zip, create the app, point it at a space | starts local; a table is a setting, not a redeploy | **once you give it a table** |

**Use A unless something stops you.** B exists for the workspace that will not give an app a writable
table, and its trade is real: see *Local database mode* below. C exists because "we cannot deploy bundles"
should not be the end of the conversation — and it now reaches the same persistent Delta mode as A, from
inside the app, with nobody editing a file.

### A. Delta mode — the default

```bash
DATABRICKS_CONFIG_PROFILE=<your-profile> databricks bundle deploy -t customer \
  --var warehouse_id=<serverless-warehouse-id> \
  --var log_schema=<catalog>.<schema-you-already-have>
```

⛔ **`-t customer` for Delta logging, `-t customer-local` for the no-catalog mode.**
Neither pins a workspace host, so both deploy into whichever workspace you are
authenticated against. `customer` is the default if you pass no `-t` at all.

### B. Local database mode — one line, no table

```bash
DATABRICKS_CONFIG_PROFILE=<your-profile> databricks bundle deploy -t customer-local \
  --var warehouse_id=<serverless-warehouse-id>
```

No `log_schema`, no table, no grant. The warehouse is still required — **the Genie space uses
one, and that is the game, not the logging.** Read *Local database mode* before you choose this.

⛔ **Auth goes in the ENVIRONMENT — `DATABRICKS_CONFIG_PROFILE` (or `DATABRICKS_HOST`/`DATABRICKS_TOKEN` in
CI) — and NOT in `-p`.** The post-deploy hook shells out to the CLI and inherits only the environment. With
`-p` alone, `bootstrap.py` sees no profile, falls back to your **DEFAULT** one, and creates the
table, the grants and the Genie space **in that workspace instead** while the app deploys to this one —
silently, with a green deploy. The `customer` targets now **stop before deploying** if neither variable is
set; that guard exists because the CLI's own mismatch message recommends `-p`, so it is the natural mistake
rather than an exotic one.

### ⛔ The one thing a bundle cannot do for you: `USE CATALOG`

In **delta mode** the app's own service principal needs three grants on the schema you named, and the deploy
makes them for you — **if you are allowed to**. One of them you may not be:

> **A schema owner cannot grant `USE CATALOG`. Only the catalog's owner (or someone with `MANAGE` on it) can.**

So if you own your schema but not the catalog above it, the deploy stops and prints the exact statement
somebody else has to run:

```sql
GRANT USE CATALOG ON CATALOG <your-catalog> TO `<the app's service principal>`;
```

**What happens if nobody runs it:** the app **starts, serves every page, and records nothing** — no scores, no
leaderboard, no clock — because the only thing it cannot do is write its activity table. `/api/health` reports
the writer as degraded. That is why the deploy refuses rather than continuing: a half-granted deployment looks
perfectly healthy to the person who deployed it and is broken for every player.

The other two grants (`USE SCHEMA`, and `SELECT, MODIFY` on the table) are yours to make if you own the
schema, which is what `log_schema` asks you for.

⭐ **Everything else in the one-line deploy is genuinely one line**, and that was measured rather than assumed
on the app is granted `CAN_USE` for the `audience_group` by the bundle, the Genie space is granted
`CAN_RUN` by the post-deploy hook, and `users` already holds `CAN_USE` on a vended workspace's warehouse
without anyone granting it. A Genie space is **not** a resource a bundle can declare, so its permissions can
never be expressed in `databricks.yml` — they do not need to be.

### ⛔ Renaming the app destroys it — do not pass `--var app_name=` to a target you have deployed before

`name:` on the app resource is its **identity**, not a label. So if the bundle state for this target already
tracks an app under one name and you deploy the same target with another, terraform **destroys the old app
and creates a new one**. The deploy is green and says nothing.

**What that costs you, and it is not just the app object:** the new app gets a **new service principal**, so
every grant the old one held — `USE CATALOG`, `USE SCHEMA`, `SELECT, MODIFY` — belongs to a principal that no
longer exists. The replacement app then starts, serves every page, and **records nothing**, which is exactly
the symptom described under *`USE CATALOG`* above. The delta table and its rows survive (they are not bundle
resources), as do Genie spaces created by the post-deploy hook.

**So:** pick the name on your **first** deploy of a target and leave it. If you must rename, expect to
re-issue the three grants to the new service principal — or deploy a **different target** instead, which has
its own state. Measured the hard way: two apps deleted by an override chosen specifically to
*avoid* touching them.

### The one human step
Each player completes the workspace sign-in and an **authorisation prompt once**, on first visit. This is
unavoidable: the app asks for the non-default `genie` scope, and until a viewer approves it the app is not
reachable for them at all — the prompt is the access gate, not an optional extra. It costs one click per
person, once, and the screen names the scope it is granting.

### Redeploying
Re-run the same command. Every step is find-or-create: the table (with an additive column
migration), the Genie space, and the grants.

---

## Local database mode

`storage_mode: local` keeps the activity log in a SQLite file inside the app container. Nothing else
changes: the same five aggregate queries run against it, the leaderboard, the clock and the operator page
all behave identically. Verified by running all 17 of the app's SQL statements unmodified against SQLite
3.53 — the whole dialect difference is two constructs, translated in one place in `app/lib/localstore.py`.

### ⛔ What it costs, stated plainly

**A Databricks Apps container has no persistent volume, so anything that replaces the container takes the
file with it.** Measured: **stopping and starting the app took a database holding 8 rows back to an empty
one**. This is the trade you are making in exchange for needing no table.

⚠️ **And do not read that as "a code deploy is safe".** In the same session a `databricks apps deploy`
*preserved* the file — which is exactly why this cannot be relied on: nothing on the page tells you which
kind of restart you are getting, the platform can replace a container without being asked, and a file inside
one is not something an admin can see, back up or audit. Treat it as "gone at any moment"; the guarantee is
the export, or a table.

So in local mode the **CSV export is the backup and the import is the restore**, and they are not a
convenience — they are the whole durability story:

1. **Operator → Activity log → Download CSV.** Do this before any redeploy, and on a schedule during a
   pilot. There is no other copy.
2. After a restart, **Operator → Restore from a log export → Choose file → Check file → Import.** Scores,
   week unlocks, focus clocks and the leaderboard all come back, because every one of them is derived from
   the log rather than stored beside it.

The restore **merges on event id**, so importing the same file twice changes nothing and you can safely
import the most recent export you have. Nothing is written until the whole file has been validated, so a
truncated download is refused with the store untouched rather than half-applied.

### One thing the restore cannot recover, and it says so

A CSV exported by an **older build** has no `received_ts` column — the app's own clock.
Those files still restore every point, every unlock and every total exactly; but the leaderboard
*tie-break* and "who cracked it first" fall back to the warehouse write clock, which has been measured
2.6s out for simultaneous answers and p50 17.8s behind under load. Players separated by seconds can come
back in the wrong order.

The importer **does not paper over this**. It classifies each file as `authoritative`, `partial` or
`unrecoverable` and says which, on the page, before and after you import — and it never fills the missing
column in from the other one, because that would make an unrecoverable ordering look exact. Re-export from
a current build and the problem does not exist.

---

## C. No bundle, no CLI: upload the repo and point an app at it

For a workspace where you cannot run bundles, or a person who does not have the CLI at all. Everything here
happens in the Databricks UI and in a SQL editor.

**Five steps, and the last two are optional.** A manual install always starts in **local database mode** —
scores kept in a file inside the app's container — and is pointed at a Unity Catalog table afterwards, from
the app's own Operator page, if you want them to survive a restart.

### Step 1 — download this repo as a zip and upload it

On GitHub: **Code → Download ZIP**. In the Databricks UI: open your workspace folder (your own `Users`
folder is fine), **Create / Import**, and drop the zip in — the workspace **unzips it on upload**.

You end up with a folder like `/Workspace/Users/you@example.com/databricks-genie-game-main/`, and the thing
the app needs is the **`app` folder inside it**. Nothing has to be edited, moved or renamed — `app/app.yaml`
is in the repo and works as shipped.

⚠️ **If the upload lands as one file instead of a folder, unzip it on your own machine and drag the unzipped
folder in.** The extraction is a feature of the UI's importer, not of the workspace itself: the equivalent
API call stores the zip as an opaque 1.3 MB file, and an app pointed at that has no `app.py` to run. Either
way what you need is a FOLDER whose top level holds `app.py` and `app.yaml`.

⚠️ **Check the upload actually landed everything** before you go on. `app/` has subfolders (`content`,
`lib`, `static`, `static/art`, `theme`), and an upload that quietly stops short produces an app that starts
and then fails on a missing file. Open `app/static/art` and confirm the images are there; that is the
deepest folder and the one worth checking.

### Step 2 — create the app, with the `genie` scope

**Compute → Apps → Create app**, then:

* **Source code path** — the `app` folder from step 1 (the one holding `app.py` and `app.yaml`).
* **User authorisation scopes** — tick **`genie`**. ⛔ **This is the one that is not optional.** The game
  reaches Genie *as the signed-in player*, never as the app, and without this scope every question fails
  while the app looks perfectly healthy.
* **App resources** — nothing yet. Local mode needs no warehouse and no table.

Deploy it. Each person who opens it completes the workspace sign-in and an **authorisation prompt once**,
which is what grants that scope for them; the screen names what it is granting.

### Step 3 — create the Genie space

This is the one thing that cannot be created for you, and **the app will look completely healthy without
it** — every page renders, the log works, and the first player to ask anything gets nothing.

In the Genie UI, make a space with:

* **Title:** `The Data Desk`, or anything you like — you will point the app at it in step 4, so the name is
  yours to choose.
* **Tables:** these five, and only these —
  `samples.bakehouse.media_customer_reviews`, `samples.bakehouse.sales_customers`,
  `samples.bakehouse.sales_franchises`, `samples.bakehouse.sales_suppliers`,
  `samples.bakehouse.sales_transactions`
* **Instructions:** paste the paragraphs from `space_instructions` in `app/content/s1_letters.json` as
  **one** instruction. (They must be one: separate paragraphs come back glued together, and a string
  containing a blank line is silently truncated at the first newline.)
* Give your players' group **CAN RUN** on it.

### Step 4 — open the app, unlock Operator, and point it at your space

Open the app, go to **Operator**, and enter the password (out of the box it is the built-in default — see
*The operator password*). The first card is **Genie space**.

Paste the space's **title**, its **id**, or the **URL** from your browser while you are looking at it; any of
the three works. Then press **Test & save**.

⭐ **The test asks your space a real question** — week 1's first blank — as you, through exactly the path a
player's question takes, and checks the answer against the value that blank expects. That is the only thing
that can tell a working space from one that merely exists: the space resolves **lazily and by title**, so
until somebody asks something, "the space is fine" and "there is no space at all" look identical from every
health check in the product.

What it tells you, and what to do about it:

| what you see | what it means |
|---|---|
| answered, and it is the value week 1 wants | done — the space is right |
| answered, but not that value | the space is reading different tables; check the five above and the instruction |
| no space with that title | it lists the spaces you *can* see, so you can pick the name off that list |
| the space did not answer | a Genie or warehouse problem, not an app one — the message is Genie's own |

That setting is stored **in your workspace**, not in the app's container, so it survives a restart and a
redeploy. The card names the exact path it wrote to.

**At this point the game is fully playable.** Release a week (**Operator → Release weeks**) and people can
play. Everything below is about keeping the scores.

### Step 5 (optional) — make the scores persistent

Out of the box the activity log is a SQLite file inside the app's container, and **stopping and starting
the app deletes it** — measured, 8 rows to 0 — as can anything else that replaces the container. The Operator page says so in its loudest element, because the way that failure presents is an
*empty game*, which nobody can tell apart from a broken install.

To keep scores, give the app a Unity Catalog table. Three things, in this order:

**1 · Create the table.** The Operator page prints the exact `CREATE TABLE` under *"How to create the table,
and how to let this app write to it"* — it is generated from the same schema the app writes, so it cannot
drift from it. Paste it into a SQL editor. Any schema you can create tables in will do.

**2 · Let the app write to it.** Two routes, and **they are not equivalent**:

* **Add the table as an app resource** (recommended, no SQL). **Compute → Apps → your app → Edit → App
  resources → Add resource → Unity Catalog table**, pick your table, and give it **SELECT** and **MODIFY**
  (add it twice if the form takes one permission at a time). Add a second resource of type **SQL warehouse**
  with **CAN USE** — the app needs one to reach Unity Catalog at all. Databricks then grants the app's
  service principal `SELECT` and `MODIFY` on the table, `USE SCHEMA` on its schema and **`USE CATALOG` on
  its catalog**, which is more than the bundle path can do for itself.

  ⛔ **This removes the SQL step. It does not remove the permission requirement.** Those grants are issued
  **as you**, so whoever adds the resource still has to be the catalog's **owner** or hold **MANAGE** on it.
  If you are neither, the UI cannot do it for you either: the catalog's owner has to add the resource, or run
  the grants below. Owning the table is not enough, because `USE CATALOG` is a catalog-level privilege.

  ⚠️ And an accepted resource is not a conferred privilege — the API will accept a table it cannot actually
  grant on. Step 3 is what settles it.

* **Or run three grants yourself.** The Operator page prints them with your app's own service principal
  already substituted. The first one is the one that needs the catalog owner:

  ```sql
  GRANT USE CATALOG ON CATALOG <catalog>   TO `<the app's service principal>`;
  GRANT USE SCHEMA  ON SCHEMA  <cat>.<sch> TO `<the app's service principal>`;
  GRANT SELECT, MODIFY ON TABLE <cat>.<sch>.<tbl> TO `<the app's service principal>`;
  ```

**3 · Type the table into the Operator page and press *Check & use it*.** The app then, **as itself** rather
than as you, reads the table, counts what is in it, writes one audit row and proves it can delete. It tells
you which of these is true:

| what it says | what to do |
|---|---|
| readable and writable | it switched — no restart, no redeploy |
| no SQL warehouse | add the warehouse resource above |
| Unity Catalog refused | the message is UC's own and names the missing privilege |
| no such table | create it with the printed DDL |
| missing columns | it lists them; point at a different table or add them |

It has to be the **app's** identity that checks. An operator with `MANAGE` on the catalog passes every check
while the app still cannot write a row, and that deployment serves every page, scores nothing and shows an
empty leaderboard.

**A table that already holds rows is picked up exactly as it stands** — every score, unlock and clock in
this game is derived from the log rather than stored beside it, so pointing at last month's table brings
last month's game back. And anything already in the temporary local database is copied across on the switch,
so a player who answered before you did this is not wiped by it.

### ⛔ If you delete the app and create it again, tell the Operator page your table again

A recreated app gets a **new service principal**, and the settings above are stored under the old one. The
new app cannot read them, so it comes up in local mode. **Nothing is lost** — the rows are in your table —
and the Operator page will tell you which table it used to write to, because a deployment that quietly comes
up empty is indistinguishable from a broken one. Put the table back in the box and it resumes.

### With the CLI, if you have it

The same install, shorter:

```bash
databricks apps create --json @deploy/manual/create-app.json      # the `genie` scope, in one line
databricks workspace import-dir ./app /Workspace/Users/<you>/bake-off-src --overwrite
databricks apps deploy <app-name> --source-code-path /Workspace/Users/<you>/bake-off-src
```

⚠️ `import-dir` prints `Import complete` and exits 0 even when it has uploaded nothing, and it can be cut
short silently. **Compare counts** rather than trusting it, and derive both rather than reading a number off
this page:

```bash
git ls-files app | wc -l
databricks workspace list /Workspace/Users/<you>/bake-off-src        # per directory, so walk each
```

This paragraph used to name the figure. It said **22** while `app/` held **32** — it went stale the first
time anybody added an image, which is exactly the failure the check exists to catch.

### Checking it from outside

```bash
curl -H "Authorization: Bearer $(databricks auth token | jq -r .access_token)" \
  https://<app-url>/api/health
```

Check the status code before reading the body, then four things:

* `"missing_config": []` — the environment arrived
* `"storage": {"mode": …}` — `local`, or `delta` with your table once you have switched
* `"weeks": 4, "blanks": 20` — the content pack loaded
* `genie_space.status` — and read it as three states, not two. `not_resolved_yet` is **not a fault**: the
  space is resolved on the first question, so a fresh container reports it whether or not the space exists.
  `/api/health` deliberately asks Genie nothing, because a question is the number this product exists to
  move. **Only a real question proves Genie works**, which is what the Operator page's Test button is for.

---

## The answer key ships with the game

**`app/content/s1_letters.json` contains all 20 answers, in an `expected` field, and it must ship** — the
app cannot run without its content pack. So **anyone who can read the deployed source can read the
answers.** That is inherent to shipping a puzzle as code, not an oversight:

* **Do not hand the repository to the players.** Give them the app's URL.
* A deployer needs the files; a player needs only the link. Keep those two audiences apart.
* The app itself never sends an answer to the browser, and no export includes `expected_value` — those
  holes were closed deliberately. The content file is the one remaining copy, and it is unavoidable.

---
## The four components, and nothing else

| # | component | notes |
|---|---|---|
| 1 | **the app** | pure Python standard library + vanilla JS/CSS. No `requirements.txt`, no npm, no CDN, no web font. |
| 2 | **one Genie space per scenario** | created by the bundle. Reached **only** with the viewer's on-behalf-of token. |
| 3 | **`samples` tables** | read-only, out of the box, so it works in any workspace. |
| 4 | **one delta table** | `<catalog>.<schema>.activity_log`. The app's only write. Leaderboard, progress, stats and the settings history are all aggregations of it. |

Deliberately absent: no job, no scheduler, no Foundation Model API, no Lakebase, no model serving, no
vector search, no secret scope, and **no outbound network call at runtime** except to the workspace.

**Why there is no scheduler.** There is nothing to schedule. An admin ticks the weeks players may
unlock, on the Operator screen, and that applies to everybody at once: weeks are made unlockable
from the admin screen and nothing runs on a calendar. Each player's own clock starts when
**they** click to unlock, so the timing that matters is per-player and cannot drift. A fresh deployment
releases week 1 and waits for a human.

---

## Configuration

On the DAB path every value is a bundle variable and there is **no `app.yaml` to edit** — the predeploy hook
generates it per target and **overwrites the committed copy**, so after a bundle deploy your tree shows
`app/app.yaml` as modified. That copy carries the deployment's real password: `git checkout app/app.yaml`
rather than committing it.

On the **no-bundle path nothing has to be edited at all.** `app/app.yaml` is committed in local mode and
works as shipped, and the two settings a hand-installed deployment actually needs — the Genie space and the
log table — are set on its own Operator page and stored **in your workspace**, so they survive a restart.
Editing the file is still available if you would rather; the table set there **wins** over the page, and the
page says so rather than accepting a value it will not use.

| variable | default | what it does |
|---|---|---|
| `storage_mode` | `delta` | `delta` writes the log to a table; `local` writes it to SQLite in the container |
| `local_db_path` | `/tmp/night_desk/night_desk.db` | where the SQLite file lives in local mode |
| `warehouse_id` | — | serverless SQL warehouse for the Genie space and the log writer |
| `log_schema` | `none` | ⭐ `<catalog>.<schema>` — **a schema you already have** and can create tables in. **Required in delta mode**; the `none` sentinel is refused there rather than prompting. The deploy creates only the table inside it |
| `log_table` | `activity_log` | the table created inside that schema |
| `season_id` | `s1_letters` | the scenario the game opens on |
| `seasons` | `s1_letters` | scenarios to build a Genie space for (comma-separated) |
| `season_tz` | `Australia/Sydney` | timezone the operator page stamps times in |
| `theme` / `alt_theme` | `databricks` / `neutral` | click the wordmark to switch livery |
| `audience_group` | `users` | workspace group granted `CAN_RUN` on the Genie space |
| `space_suffix` | **`none`** | appended to space titles so two targets never share one. `none` means "no suffix" — it must **never** be set to an empty string, which the Apps API rejects *after* the app has started |
| `operator_password` | **`none`** | password for the Operator page. `none` means "use the **built-in default**", so the page always opens — see *The operator password* |
| `allow_date_override` | `1` | enables `?as_of=YYYY-MM-DD` to preview any week |

## The operator password

The Operator page needs a password as well as an address on the allowlist. **Two locks, both required**,
because they answer different questions: the allowlist needs a redeploy to change, so it cannot be handed
to a colleague on the day; the password is per-browser, so it cannot express "only these people".

Enter it once and **that browser stays unlocked for as long as the app keeps running**. The session is held
in the container's memory, so **a redeploy or restart means everyone enters it again** — that is what "as
long as the app is kept alive" means, and it fails to *locked*, never to open.

### Setting it

| where | when to use it |
|---|---|
| `--var operator_password=…` on the deploy | the normal way |
| `OPERATOR_PASSWORD=…` in the environment | CI, or to keep it out of your shell history |
| `~/.night-desk/operator_password` (a file, outside this repo) | your own repeated deploys — the predeploy hook reads it if the other two are unset |

```bash
DATABRICKS_CONFIG_PROFILE=<your-profile> databricks bundle deploy -t customer \
  --var warehouse_id=<id> --var log_schema=<catalog>.<schema> \
  --var operator_password='choose-something'
```

On the **no-bundle path**, set `OPERATOR_PASSWORD` in `app/app.yaml` before you upload it.

### ⚠️ Where your password ends up — it is not in git, and it *is* in your workspace

Two true statements, and only the second tells you where the value actually lives:

* **It never enters this repository.** `app/app.yaml` is gitignored and the committed default is `none`, so
  nothing containing your password reaches git or GitHub.
* **It is written into your workspace.** The deploy generates `app/app.yaml` with the value in it and
  **syncs that file into the app's source directory** (`databricks.yml` lists it under `sync.include`
  precisely so it cannot be left out). **Anyone who can read that workspace path can read your password.**

That is the normal Databricks Apps configuration path and it is proportionate — anyone with read access to
the app's source directory can already redeploy or modify the app, which is strictly more power than the
operator password confers. But you should decide that rather than discover it.

**If that is unacceptable in your environment**, do not use an env value: put the password in a
**Databricks secret** and read it at startup instead. That adds a component this app deliberately does not
have, which is a trade worth making if your threat model includes people with workspace read access.

⭐ **The general trap, worth knowing beyond this one value:** a gitignored file is invisible to **git** and
**not** invisible to the **sync**. A clean git history is a claim about the history and about nothing else.
(The same asymmetry once ran the other way here: a generated `app.yaml` was silently *not uploaded* because
`bundle sync` honours `.gitignore` — which is why the explicit `sync.include` exists.)

### A password ALWAYS deploys, and `none` means "use the built-in default"

**You do not have to set anything.** `none` — the value in the bundle, and what the committed
`app/app.yaml` ships with — resolves to a **built-in default password** in `app/lib/config.py`. So a one-line
deploy, and the no-bundle route where nobody edits a file, both come up with an operator page somebody can
actually open.

⛔ **That default is PUBLIC by construction.** It is in this repo. It gates **players** wandering into the
operator page; it is not a secret from anyone who can read the repo or the workspace. Override it per
deployment with `--var operator_password=…`, `OPERATOR_PASSWORD` in the environment, or the file
`~/.night-desk/operator_password` — in that order of precedence, and the deploy log says which one it used.

**This changed deliberately.** It used to mean
**CLOSED**, and the reasoning for that is still worth knowing because it applies to any gate: open-when-unset
is the *silent* failure — a player clicks Operator and lands on the page that **releases weeks for everyone**
— while closed-when-unset is **discoverable**. An open gate here once let a second identity download
another player's answers. What makes a known default acceptable now is that the leak was fixed at its
**root**: `expected_value` is not projected by any query, so even an operator cannot download the answers.

### ⚠️ What this password is, and what it is not

It reaches the container through `app.yaml`, so **anyone who can read the app's source in your workspace can
read it.** It is a gate against **players** wandering into the operator page. It is **not** a secret store
and **not** protection from someone with workspace access. If you need that, put the value in a Databricks
secret scope and read it at startup — which adds a component this app deliberately does not have.

Also: it is **not** a per-person credential. Everyone who operates shares it, and changing it needs a
redeploy. There is no *who* any more — the address allowlist was removed, so **anyone with the password
can operate**, which also means anyone with a workspace account can guess at it: there is no attempt limit.
That is a deliberate trade for a game. What is behind the gate: releasing weeks, the activity log and its
export, and the **reset**, which deletes everyone's progress (and which keeps its own protections — a
verified backup, an exact confirmation phrase, and a logged row naming who pressed it).

---

### Before real players see this

Two things are worth a moment, and neither is an allowlist any more:

* **Change the operator password** if anything about your deployment is not "just a game". The default is
  public, and the Operator page releases weeks, exports the log and can reset everyone's progress.
* **Know who can open the app at all.** The bundle grants `CAN_USE` to the `audience_group` (default
  `users`), which is what makes "anyone in the workspace can just start playing" true. Narrow that variable
  if you want a smaller audience — it is the same group the Genie space is granted to, deliberately, so the
  two cannot drift apart.

One place changes the gate: `operator_gate()` in `app/app.py`, which every operator surface calls.

---

## Changing the theme, the story, or the data

* **Livery** — `app/theme/*.json`: 21 semantic colour tokens plus a wordmark. Add a file, point `theme` at
  it. Check every token for WCAG AA contrast against the background it is actually painted on, in the
  rendered page rather than on paper. One design rule is worth stating because it is
  what a bright palette gets wrong: **a fill colour and a boundary colour are different roles and cannot be
  the same value** — a saturated yellow is 12.6:1 as a button fill with dark ink on it and 1.4:1 as a lone border,
  which is why `accent-line` exists.
* **Story and clues** — `app/content/<season>.json` is the content pack. A content pack
  carries its cases, its clues, its Genie space title, the tables that space needs, and the instructions
  that space is created with, so **the pack and the space cannot drift**.
* **A new scenario** — write a pack, add its id to `seasons`, redeploy. It gets its own Genie space.

**One space per scenario is a measured decision.** A single space holding two unrelated sample domains
answered a bakery question from a travel dataset — asked for the top payment method by revenue it
returned `creditcard` from `samples.wanderbricks.payments` (17/18 correct, versus 18/18 for the
single-domain space). A wrong-domain answer raises no
error, so a player is simply told "wrong" for asking a perfectly good question.

---

## How answer checking works, and how it fails

There is no Foundation Model API in the target workspace, so nothing may *infer* correctness. Every
verdict is a normalised comparison against a value computed with SQL at authoring time.

**Asking and submitting are separate acts.** Players ask as often as they like — unlimited, free, and the
only thing that ever costs points is taking a **hint** — a flat **−5**, charged once per blank. A wrong answer costs
nothing, because a strike system would teach people to stop asking, and asking is the point.

**The first question is one press away, and it scores.** The ask box opens with *Type your question below*
and four **starter chips** under it; pressing one types that question and asks it — so a first-time visitor
presses a chip, presses the green value that comes back, presses Submit, and has a point, and the loop is
taught by doing it rather than by reading about it. The chips are the Genie space's **own sample
questions** (`space_sample_questions`), so they cannot drift from what the space was set up to answer.

The check looks at the **value**, never the phrasing, so any question that yields the right value wins.
Numbers get an authored tolerance; strings are compared after casefolding, accent- and
punctuation-stripping. **Near misses are authored, not inferred**: each clue lists the values you land on
when you make the classic mistake, with a nudge that names it — answer *Tokyo Tidbits* for the
second-best-selling product and you are told "you counted tickets, not takings", because that product is
second by receipts and fourth by revenue.

**The residual unfairness, stated plainly:** Genie is not deterministic, so a genuinely correct question
can return a defensible value nobody banded. Mitigations: every clue's accept-list was built by asking it
several ways and recording what recurred; numeric clues carry a ±10% near band; and every numeric clue
treats `0` as a near miss with a nudge, because a zero almost always means the question filtered
everything out rather than that the answer is none. The Operator page surfaces **values several players
submitted and the checker refused** — that list is the feedback loop, and folding it into a clue's
accept-list between weeks is how the game improves instead of degrading.

---

## Screens

* **Play** — the week's animation and the line under it, the ask box, the answer, this week's five answer
  chevrons each with a **Hint (−5)** button, and the four-week board.
* **Leaderboard** — this week and all-time, plus who found each answer first.
* **Operator** — total unique users; **unique users who asked at least one question**, which is the number
  this whole exercise exists to move; total questions and sessions; averages per user and per session;
  correct/near/wrong split; a per-week and per-day funnel; the rejected-value feedback list; the log
  writer's own health so an empty page can never be mistaken for quiet data; the settings history; the raw
  log, paginated; and CSV export.

**Export** — the button fetches the CSV and hands the browser a blob to save; a real file lands on disk.
An embedded viewer can suppress a download, so the button says so and offers the data on screen to copy,
with the SQL underneath, because the delta table is the real export either way.

---

## Development

```bash
DATABRICKS_HOST=<host> WAREHOUSE_ID=<id> LOG_TABLE_FQN=<catalog>.<schema>.activity_log \
  python3 app/app.py                        # runs the whole game on a laptop
STORAGE_MODE=local python3 app/app.py       # ... with no warehouse and no table
```


`DEV_VIEWER_*` and `DEV_SQL_TOKEN` are read **only** when `DATABRICKS_APP_NAME` is absent, i.e. never in
a deployed container.
