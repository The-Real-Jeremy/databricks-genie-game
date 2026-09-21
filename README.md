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
| **C. No DAB at all** | neither DAB nor the CLI (upload + point an app at it) | either of the above | per the mode you pick |

**Use A unless something stops you.** B exists for the workspace that will not give an app a writable
table, and its trade is real: see *Local database mode* below. C exists because "we cannot deploy bundles"
should not be the end of the conversation.

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

**A Databricks Apps container has no persistent volume, so a restart or a redeploy loses the file.** Not
"may lose" — will. This is the trade you are making in exchange for needing no table.

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

## C. No DAB: upload the repo and point an app at it

For a workspace where bundles are not available. **This path is walked and verified, not theorised** — the
steps below are the ones that were actually run, and the warnings are the things that actually bit.

### What you need
The Databricks UI, and either the CLI or the UI's own file upload. No bundle, no terraform, no notebook.

### Step 1 — put `app.yaml` in place. **This is the step everyone misses.**

A fresh clone has **no `app/app.yaml`**, because the DAB path generates it and it is gitignored. An app
pointed at a source folder with no `app.yaml` starts with no command and no environment, and fails in a way
that reads as broken code.

```bash
cp deploy/manual/app.yaml app/app.yaml      # local mode
# or, if you have a writable table:
cp deploy/manual/app.delta.yaml app/app.yaml
```

**Nothing in it has to be edited.** It works as shipped: the operator page opens with the built-in
default password (see *The operator password*). Set `OPERATOR_PASSWORD` if you want your own.

### Step 2 — upload the `app/` folder to your workspace

```bash
databricks workspace import-dir ./app /Workspace/Users/<you>/data-desk-src --overwrite
```

Or drag the folder into the workspace file browser. **Upload the *contents* of `app/`** so that `app.py`
and `app.yaml` sit at the top of the destination folder — not a nested `app/app.py`.

⚠️ **Count the files afterwards.** `import-dir` prints `Import complete` and exits 0 even when it has
uploaded nothing, and it can be cut short silently. So compare the two counts — and **derive both**, never
read one off this page:

```bash
git ls-files app | wc -l        # what a stock checkout holds, plus one for the generated app.yaml
databricks workspace list /Workspace/Users/<you>/data-desk-src | wc -l      # per directory, so walk each
```

This sentence used to name the figure. It said **22** while `app/` held **32** — it went stale the first time
anyone added an image, which is precisely the failure this check exists to catch.

### Step 3 — create the app **with the `genie` scope**

The game reaches Genie *as the signed-in viewer*, which needs a non-default scope. The UI's create form may
not offer it, so create the app with the API body:

```bash
databricks apps create --json '{
  "name": "data-desk",
  "description": "The Genie Bake-Off — a four-week Genie game.",
  "user_api_scopes": ["genie"]
}'
```

(`deploy/manual/create-app.json` is this file, ready to edit.) Verify it took:
`databricks apps get data-desk` must show `user_api_scopes: ["genie"]`. **Without it every question fails**
even though the app looks perfectly healthy.

### Step 4 — deploy the app at that path

```bash
databricks apps deploy data-desk --source-code-path /Workspace/Users/<you>/data-desk-src
```

### Step 5 — ⛔ create the Genie space. **The app will look fine without it and fail on the first question.**

This is the one the DAB path does for you in its post-deploy hook, and the one a manual deploy silently
skips. After step 4 the app starts, `/api/health` reports `ok: true` with `missing_config: []`, every page
renders, the operator log works — and the first player to ask anything gets nothing, because there is no
space to ask.

Either run the bundle's own bootstrap, which in local mode creates **only** the space:

```bash
STORAGE_MODE=local APP_NAME=data-desk WAREHOUSE_ID=<id> \
  SEASON_ID=s1_letters SEASONS=s1_letters SPACE_SUFFIX=none \
  AUDIENCE_GROUP=users python3 bootstrap/bootstrap.py
```

…or build it by hand in the Genie UI, which needs no tooling at all:

* **Title:** exactly `The Data Desk` — the app finds its space **by title**, so the title *is* the wiring.
  If you set `GENIE_SPACE_SUFFIX` in `app.yaml` to anything but `none`, the title must carry that suffix
  too.
* **Tables:** these five, and only these —
  `samples.bakehouse.media_customer_reviews`, `samples.bakehouse.sales_customers`,
  `samples.bakehouse.sales_franchises`, `samples.bakehouse.sales_suppliers`,
  `samples.bakehouse.sales_transactions`
* **Instructions:** paste the five paragraphs from `space_instructions` in
  `app/content/s1_letters.json` as **one** instruction. (They must be one: separate paragraphs come back
  glued together, and a string containing a blank line is silently truncated at the first newline.)
* Grant the players' group `CAN_RUN`.

### Step 6 — verify, and verify the *space* specifically

```bash
curl -H "Authorization: Bearer $(databricks auth token | jq -r .access_token)" \
  https://<app-url>/api/health
```

Check **four** things, and check the status code before reading the body:

* `"missing_config": []` — the environment arrived
* `"storage": {"mode": "local"|"delta", …}` — the mode you intended
* `"weeks": 4, "blanks": 20` — the content pack loaded
* then **ask one question in the app**, and re-check `/api/health`: `genie_space.id` must now be filled
  in, and it must be **your** space.

That last one matters more than it looks. Space resolution is **lazy** — `genie_space` is empty until the
first question, so a fresh health check cannot tell "no space exists" from "nobody has asked yet". And
because resolution is **by title**, an app in a workspace that already has a space with that title will
silently bind to *that* one. Exactly that happened on the verification run of this path: the app worked
first time because a space named `The Data Desk` was already present from an earlier deploy. Read the
resolved id and confirm it is the space you made.

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

On the DAB path every value is a bundle variable and there is **no `app.yaml` to edit** — it is generated
per target by the predeploy hook and is gitignored, so a stale copy can never be committed and deployed to
the wrong target. On the **no-DAB path you edit `app.yaml` directly**; see path C, and start from
`deploy/manual/app.yaml`.

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

On the **no-DAB path**, set `OPERATOR_PASSWORD` in `app/app.yaml` (see `deploy/manual/app.yaml`).

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

**You do not have to set anything.** `none` — the value in the bundle, and what `deploy/manual/app.yaml`
ships with — resolves to a **built-in default password** in `app/lib/config.py`. So a one-line deploy, and
the no-DAB route where nobody edits a file, both come up with an operator page somebody can actually open.

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
