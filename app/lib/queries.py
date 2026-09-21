"""Every aggregate the app shows, as SQL over the one activity table.

Points are deduplicated per (player, clue) so replaying a solved clue cannot inflate a score, and the
operator page counts DISTINCT users rather than rows so a chatty session cannot look like adoption.
"""

def _t(table):
    return table


SEASON_FILTER = "AND season_id = :season_id"

# An admin_setting row records a configuration change, not a person playing. Left in, it counted the
# operator as a player, invented a phantom week in the by-week table, and showed up as "off-calendar"
# activity in the demo/live split - three wrong numbers from one included row.
#
# ⭐ `import_receipt` IS EXCLUDED FOR THE SAME REASON AND IT IS THE SAME MISTAKE WAITING TO HAPPEN AGAIN.
# It records that a CSV was restored, not a person playing. Added with the importer: the
# receipt names the operator who ran it, so left in it would have counted them as a player and inflated
# the `players` denominator that all three leaderboard averages divide by. The lesson generalises past
# these two names - ANY meta row added here must be excluded from the player aggregates, so keep this a
# LIST rather than reverting it to a single <>.
# ⭐ `admin_reset` JOINED THIS LIST FOR BOTH OF ITS REASONS AT ONCE.
# It is not a person playing — so it must never inflate the `players` denominator, exactly like the two
# above. AND because the reset PRESERVES the meta types, its own marker row SURVIVES the wipe it records:
# the operator page can therefore say when the last reset happened and who pressed it. A reset with no trace
# is indistinguishable from data loss the next time somebody asks where the scores went.
# ⭐ `admin_storage` JOINED IT IN , the same shape for the third time. The operator page can now point the
# app at a Unity Catalog table, and it proves the table is writable by inserting ONE row before switching —
# that row is configuration history, not a person playing. Meta for both reasons above: it must not inflate
# the `players` denominator, and it must survive a reset so the table itself records who pointed the game at
# it and when.
# ⛔ AND THE SQL IS NOW DERIVED FROM THE TUPLE rather than typed out a second time. Adding a name meant
# editing two places, and the copy that gets missed is a meta row silently counted as a player.
META_EVENT_TYPES = ("admin_setting", "import_receipt", "admin_reset", "admin_storage")
NOT_ADMIN = "event_type NOT IN (" + ", ".join("'%s'" % t for t in META_EVENT_TYPES) + ")"


def last_reset(table):
    """When the game was last reset, and by whom — read from the marker the reset itself wrote.

    ⭐ THIS IS ALSO HOW A SECOND CONTAINER FINDS OUT. A reset pressed in one container clears THAT
    container's caches; any other process is still holding a forward-only clock and a warm score cache that
    a cleared table cannot correct (`Clock._acc` is `max(memory, table)`). So every container compares this
    timestamp against the last one it acted on, and clears its own memory when it changes. That makes the
    button correct without assuming how many containers there are — which is the assumption I could not
    verify from outside the platform.
    """
    return f"""
    SELECT MAX(COALESCE(received_ts, event_ts)) AS reset_at,
           MAX(user_email)                      AS by_email,
           MAX(user_display_name)               AS by_name,
           COUNT(*)                             AS resets
    FROM {_t(table)} WHERE event_type = 'admin_reset'"""


# ─────────────────────────────────────────────────────────── IDENTITY
#
# THE REQUIREMENT: the logs carry each user's email address, because names may not be unique; the
# leaderboard identifies users uniquely by email without displaying it.
#
# ⭐ WHAT WAS ALREADY TRUE, measured before changing anything: `user_email` is a populated log column and is
#    projected by `raw_page` and `export_all`, so **the logs already carried the email**. What `user_key` is,
#    though, is NOT the email — `viewer()` builds it from `x-forwarded-user`, i.e. the workspace USER ID
#    (numeric), falling back to the email only when no user is forwarded. So this is a real identity change
#    for the AGGREGATES, not merely a display question.
#
# ⭐ AND HIS REASON IS BETTER THAN NAME-UNIQUENESS. The board never grouped by name, so two people called
#    "J Smith" were already two rows. The concrete gain is RECONCILIATION: `raw_page` does not project
#    `user_key` at all, so an operator looking at the log could not match a leaderboard row to the rows
#    behind it. Grouping on the same field the log shows makes the two views joinable by eye.
#
# ⛔ NEVER `GROUP BY user_email` BARE. It is nullable, and SQL groups all NULLs together — every row with a
#    missing email would collapse into ONE phantom player, which is the silent-wrong-number direction. So the
#    identity is COALESCE(email, user_key): an email when there is one, the old key when there is not, and
#    never a merged bucket.
PLAYER_ID = "COALESCE(NULLIF(TRIM(user_email), ''), user_key)"

# Per-VIEWER lookups (player_state, player_weeks, player_question_count) deliberately stay keyed on
# `user_key`: they answer "what is MY progress" for the person making the request, they are already correct,
# and re-keying them would rewrite how existing history is matched for no gain. The split is intentional —
# aggregation identity and per-request identity answer different questions.


# ───────────────────────────────────────────── THE HINT PENALTY, AS ONE FRAGMENT USED THREE TIMES
#
# A per-blank "Hint (-5)" button: it opens a pop-up revealing the hint for that blank, and
# permanently adds -5 to that blank's score.
#
# ⭐ SO THE PENALTY IS AN EVENT THAT IS RECORDED, NOT A NUMBER HELD IN THE PAGE. A -5 kept in the browser is
#    wiped by a refresh and the button comes back; a `hint_used` row is still there after a reload, a new
#    container, or a CSV restore. Everything below derives the score from that row.
#
# ⛔ MAX, NEVER SUM. This is what makes the penalty IDEMPOTENT PER BLANK — and it is a property of the
#    DERIVATION rather than of the button, which is strictly stronger. Two clicks, two tabs, a retried
#    write, a replayed import: all of them produce the same -5, because duplicate rows cannot add up. The
#    write is guarded too, but that guard reads a per-container cache which a second container starts cold.
#    The guard is the courtesy; this is the guarantee.
# ⛔ ABS, so a row whose sign was written the other way still DEDUCTS rather than paying the player.
# ⛔ THE VALUE COMES FROM THE ROW, not from a constant repeated in SQL: a hint keeps the price it was
#    revealed at, which is what "permanently adds -5 for THAT answer" means. If two rows on one blank ever
#    carried different prices the larger is charged — a consequence of MAX, stated here rather than
#    discovered later.
# ⛔ AND A HINT ROW CARRIES NO `verdict`, so every aggregate that filters `verdict = 'correct'` ignores it.
#    That is what makes this additive — nothing existing changed meaning — and it is also why the penalty
#    had to be added EXPLICITLY to all THREE places that sum points. Miss one and it does not error: the
#    board simply disagrees with the player's own page.
EARNED = "MAX(CASE WHEN verdict = 'correct' THEN points ELSE 0 END)"
HINT_PENALTY_SQL = "COALESCE(MAX(CASE WHEN event_type = 'hint_used' THEN ABS(points) END), 0)"
SOLVED_FLAG = "MAX(CASE WHEN verdict = 'correct' THEN 1 ELSE 0 END)"
# The rows a net score is built from. `clue_id IS NOT NULL` because these GROUP BY clue_id and SQL groups
# every NULL together — the same trap that would have collapsed every missing email into one phantom
# player.
SCORING_ROWS = "clue_id IS NOT NULL AND (verdict = 'correct' OR event_type = 'hint_used')"


def player_state(table):
    """Per-blank progress for ONE player.

    ⭐ IT RETURNS THE TWO COMPONENTS, `earned` AND `hint_penalty`, AND NOT THE NET. `state.py` subtracts
    them in exactly one place. That is deliberate: this table is read on a 45-second TTL while the player's
    OWN click updates the same cache immediately, so if both paths did the subtraction separately an answer
    would read 100 at the moment of answering and 95 after the TTL — a self-correcting number, which is
    worse than a wrong one because nothing ever looks broken and the player simply stops trusting the score.
    The BOARD has to net across blanks in SQL, so that derivation exists too; `test_r9_hint_penalty` asserts
    the two agree rather than trusting that they do.
    """
    return f"""
    SELECT clue_id,
           {EARNED}                                                                AS earned,
           {HINT_PENALTY_SQL}                                                       AS hint_penalty,
           {SOLVED_FLAG}                                                            AS solved,
           SUM(CASE WHEN event_type = 'hint_used' THEN 1 ELSE 0 END)               AS hints,
           MAX(CASE WHEN verdict IN ('wrong', 'near') THEN 1 ELSE 0 END)            AS tried,
           MAX(week_index)                                                         AS week_index
    FROM {_t(table)}
    WHERE user_key = :user_key AND clue_id IS NOT NULL {SEASON_FILTER}
    GROUP BY clue_id"""


def player_question_count(table):
    return f"""
    SELECT COUNT(*) AS questions,
           COUNT(DISTINCT session_id) AS sessions
    FROM {_t(table)} WHERE user_key = :user_key AND event_type = 'question_asked'"""


def leaderboard(table, scope="all"):
    """scope: 'all' or 'week' (week_index bound by parameter).

    ⛔ IT HAS TO READ HINT ROWS TOO, which is why the filter is no longer `verdict = 'correct'` alone. A
    hint carries no verdict, so the old filter dropped every penalty and this board would have printed a
    GROSS score directly beneath the player's own NET one — two numbers on one page, both claiming to be
    the score, exactly the documented failure. So the per-clue CTE takes correct answers AND
    hint rows, nets them, and the outer SUM is over the net.

    ⛔ AND `solved` IS NOW `SUM(solved)`, NOT `COUNT(*)`. Once a hint-only clue can appear in the CTE, a row
    count is no longer a solve count: a hint on a blank they never answered would have read as a solve. That
    is the half of this change that would have been invisible — nobody re-derives that column.

    ⛔ MEMBERSHIP IS UNCHANGED, by `HAVING SUM(solved) > 0`: the board lists players who have actually
    scored, which is precisely what the old WHERE clause did. So a player who took a hint and solved nothing
    is not listed here at -5; it shows on their own page, which is where their own penalty belongs.

    The tie-break only considers rows that were actually solved — a hint row has no solve time and must not
    be allowed to become one.
    """
    where = SCORING_ROWS + " " + SEASON_FILTER
    if scope == "week":
        where += " AND week_index = :week_index"
    return f"""
    WITH per_clue AS (
      SELECT {PLAYER_ID} AS player_id,
             MAX(user_display_name) AS display_name,
             clue_id,
             {EARNED} AS earned,
             {HINT_PENALTY_SQL} AS hint_penalty,
             {SOLVED_FLAG} AS solved,
             -- the app's clock: this is the tie-break that decides the board order
             MIN(CASE WHEN verdict = 'correct' THEN COALESCE(received_ts, event_ts) END) AS first_solved
      FROM {_t(table)} WHERE {where} GROUP BY {PLAYER_ID}, clue_id
    )
    SELECT player_id AS user_key, MAX(display_name) AS display_name,
           SUM(earned - hint_penalty) AS points, SUM(solved) AS solved,
           MAX(first_solved) AS last_solved
    FROM per_clue GROUP BY player_id
    HAVING SUM(solved) > 0
    ORDER BY points DESC, last_solved ASC
    LIMIT 100"""


def filter_player_counts(table, scope="all"):
    """TOTAL PLAYERS for one leaderboard filter.

    THE REQUIREMENT: for the overall and per-week leaderboards, show the total players for that
    filter, so it is clear how many people took part in a given week's questions.

    ⭐ "PARTICIPATED" IS THREE DIFFERENT NUMBERS AND HE WILL BE QUOTED AS WHICHEVER ONE HE IS NOT GIVEN, so
    all three are returned with their denominators in the name rather than one being chosen for them:

      * `players_opened`    — anyone with ANY row in scope. For a week that means they unlocked it and the
                              clock started; it counts someone who looked and typed nothing.
      * `players_attempted` — anyone who SUBMITTED an answer in scope, right or wrong. This is the one that
                              matches "participated in the week 2 questions" most closely, and it is the one
                              I would put on the board.
      * `players_scored`    — anyone with at least one CORRECT answer in scope. Always <= attempted.

    Identity is PLAYER_ID (email, falling back to user_key) so these reconcile with the board above them.
    Meta rows are excluded, so an operator saving a setting is never a player.

    ⚠️ `players_opened` for a WEEK is not "opened the app" — it is "has a row tagged with that week", which
    includes a `week_unlocked` click with nothing after it. That is the honest reading of participation-by-
    presence and it is why the name says opened rather than played.
    """
    where = NOT_ADMIN + " " + SEASON_FILTER
    if scope == "week":
        where += " AND week_index = :week_index"
    return f"""
    SELECT
      COUNT(DISTINCT {PLAYER_ID})                                                  AS players_opened,
      COUNT(DISTINCT CASE WHEN verdict IN ('correct','near','wrong','empty')
                          THEN {PLAYER_ID} END)                                    AS players_attempted,
      COUNT(DISTINCT CASE WHEN verdict = 'correct' THEN {PLAYER_ID} END)           AS players_scored,
      COUNT(DISTINCT CASE WHEN event_type = 'question_asked' THEN {PLAYER_ID} END) AS players_asked
    FROM {_t(table)} WHERE {where}"""


def leaderboard_stats(table):
    """The five figures at the top of the leaderboard.

    ⭐ THE DENOMINATOR OF ALL THREE AVERAGES IS `players` — the SAME number shown as the first figure —
    so the strip reconciles against itself. Someone WILL divide figure 2 by figure 1 and expect figure 4;
    if the averages quietly used "players who did that thing" as their denominator, that arithmetic would
    fail and the strip would look wrong while every figure in it was individually defensible.

    The active-player variants are returned ALONGSIDE rather than instead, because "average score" over
    everyone who opened the app is a genuinely different number from "average score over people who
    scored", and whichever one is published alone gets quoted as the other. Both are named, so a reader
    cannot mistake which they have: `avg_score` / `avg_score_active`, and `scorers` is the second one's
    denominator.

    ⛔ POINTS ARE DEDUPLICATED PER (user, clue) WITH MAX, exactly as `leaderboard()` does it. A plain
    SUM(points) over every row counts a re-solved blank twice and would make this strip disagree with the
    board printed directly beneath it — two numbers, one page, both claiming to be the score.

    ⛔ AND IT NETS THE HINT PENALTY, with the same membership as the board (`HAVING SUM(solved) > 0`), for
    the same reason: `total_points` here is read as the sum of the points column printed below. A net board
    under a gross strip is the identical defect in the identical place.
    """
    return f"""
    WITH per_clue AS (
      SELECT {PLAYER_ID} AS player_id, clue_id,
             {EARNED} AS earned, {HINT_PENALTY_SQL} AS hint_penalty, {SOLVED_FLAG} AS solved
      FROM {_t(table)} WHERE {SCORING_ROWS} GROUP BY {PLAYER_ID}, clue_id
    ), per_player AS (
      SELECT player_id, SUM(earned - hint_penalty) AS points, SUM(solved) AS solves
      FROM per_clue GROUP BY player_id HAVING SUM(solved) > 0
    ), base AS (
      SELECT COUNT(DISTINCT {PLAYER_ID})                                         AS players,
             SUM(CASE WHEN event_type = 'question_asked' THEN 1 ELSE 0 END)       AS total_questions
      FROM {_t(table)} WHERE {NOT_ADMIN}
    )
    SELECT
      base.players                                                               AS players,
      base.total_questions                                                       AS total_questions,
      COALESCE((SELECT SUM(points) FROM per_player), 0)                          AS total_points,
      COALESCE((SELECT SUM(solves) FROM per_player), 0)                          AS total_correct,
      (SELECT COUNT(*) FROM per_player)                                          AS scorers,
      (SELECT COUNT(DISTINCT {PLAYER_ID}) FROM {_t(table)}
         WHERE event_type = 'question_asked')                                    AS askers
    FROM base"""

def week_solve_counts(table):
    """How many players have cracked each clue — powers the 'x others found this' colour."""
    return f"""
    SELECT clue_id, COUNT(DISTINCT {PLAYER_ID}) AS solvers
    FROM {_t(table)} WHERE verdict = 'correct' AND week_index = :week_index {SEASON_FILTER}
    GROUP BY clue_id"""


def first_blood(table):
    """Who cracked each clue of the week first — the only competitive flourish on the board.

    ⛔ ORDERED BY received_ts, THE APP'S CLOCK, NOT event_ts. `event_ts` is current_timestamp() evaluated by
    the WAREHOUSE when the INSERT runs, which is a queue away from when the player pressed the key: two
    players answering the same blank simultaneously came out 2.639s apart, and under load the write queued
    at p50 17.8s — orders of magnitude more than any human gap. Ranked on event_ts, this board was
    substantially a picture of our own infrastructure. COALESCE keeps rows written before the column
    existed from vanishing from the board.

    ⛔ AND IT PROJECTS THE IDENTITY, which it did not until . The CTE selected `user_key` and the FINAL
    projection dropped it, so the payload carried a NAME and nothing else — meaning the client could not tell
    two players called the same thing apart on this list, and could not fix it itself because the field its
    disambiguating tag derives from was not there. That sat INSIDE the "identify users uniquely"
    requirement rather than beside it.

    ⭐ AND IT MUST BE THE SAME `PLAYER_ID` THE BOARD USES, not `user_key`. The client derives a short tag by
    hashing this field. Had this list carried the workspace id while the board carried the email, the SAME
    PERSON would have been given TWO DIFFERENT TAGS in two lists on ONE page — which is worse than no tag,
    because a tag that disagrees with itself looks like two people.
    """
    return f"""
    WITH firsts AS (
      SELECT clue_id, {PLAYER_ID} AS player_id, user_display_name,
             COALESCE(received_ts, event_ts) AS at_ts,
             ROW_NUMBER() OVER (PARTITION BY clue_id
                                ORDER BY COALESCE(received_ts, event_ts) ASC) rn
      FROM {_t(table)} WHERE verdict = 'correct' AND week_index = :week_index {SEASON_FILTER}
    )
    SELECT clue_id, player_id AS user_key, user_display_name, at_ts AS event_ts
    FROM firsts WHERE rn = 1"""


def operator_stats(table):
    """The numbers this whole exercise exists to move. Activation is the second row."""
    return f"""
    SELECT
      COUNT(DISTINCT user_key)                                                          AS unique_users,
      COUNT(DISTINCT CASE WHEN event_type = 'question_asked' THEN user_key END)         AS activated_users,
      SUM(CASE WHEN event_type = 'question_asked' THEN 1 ELSE 0 END)                    AS total_questions,
      COUNT(DISTINCT session_id)                                                        AS total_sessions,
      COUNT(DISTINCT CASE WHEN verdict = 'correct' THEN user_key END)                   AS users_who_solved,
      SUM(CASE WHEN verdict = 'correct' THEN 1 ELSE 0 END)                              AS correct_lodges,
      SUM(CASE WHEN verdict = 'near'    THEN 1 ELSE 0 END)                              AS near_lodges,
      SUM(CASE WHEN verdict = 'wrong'   THEN 1 ELSE 0 END)                              AS wrong_lodges,
      SUM(CASE WHEN event_type = 'hint_used' THEN 1 ELSE 0 END)                         AS hints_used,
      MIN(event_ts)                                                                     AS first_event,
      MAX(event_ts)                                                                     AS last_event,
      COUNT(*)                                                                          AS total_events
    FROM {_t(table)} WHERE {NOT_ADMIN}"""


def operator_by_season(table):
    return f"""
    SELECT COALESCE(season_id, '(unset)') AS season_id,
           COUNT(DISTINCT user_key) AS players,
           COUNT(DISTINCT CASE WHEN event_type='question_asked' THEN user_key END) AS askers,
           SUM(CASE WHEN event_type='question_asked' THEN 1 ELSE 0 END) AS questions,
           SUM(CASE WHEN verdict='correct' THEN 1 ELSE 0 END) AS solves,
           MIN(event_ts) AS first_event, MAX(event_ts) AS last_event
    FROM {_t(table)} WHERE {NOT_ADMIN} GROUP BY 1 ORDER BY questions DESC"""


def operator_demo_split(table):
    """Rows written while an admin override was in force are exactly those where the week the row was
    scored under differs from the week the calendar said. That is what tells demo apart from live."""
    return f"""
    SELECT CASE WHEN real_week_index IS NULL THEN 'unknown'
                WHEN week_index = real_week_index THEN 'live'
                ELSE 'off-calendar' END AS bucket,
           COUNT(*) AS events,
           COUNT(DISTINCT user_key) AS users,
           SUM(CASE WHEN event_type='question_asked' THEN 1 ELSE 0 END) AS questions
    FROM {_t(table)} WHERE {NOT_ADMIN} GROUP BY 1 ORDER BY events DESC"""


def operator_by_week(table):
    return f"""
    SELECT week_index,
           COUNT(DISTINCT user_key)                                                  AS players,
           COUNT(DISTINCT CASE WHEN event_type='question_asked' THEN user_key END)    AS askers,
           SUM(CASE WHEN event_type='question_asked' THEN 1 ELSE 0 END)               AS questions,
           COUNT(DISTINCT CASE WHEN verdict='correct' THEN user_key END)              AS solvers,
           SUM(CASE WHEN verdict='correct' THEN 1 ELSE 0 END)                         AS solves
    FROM {_t(table)} WHERE week_index IS NOT NULL AND {NOT_ADMIN}
    GROUP BY week_index ORDER BY week_index"""


def operator_daily(table):
    return f"""
    SELECT to_date(event_ts) AS day,
           COUNT(DISTINCT user_key) AS players,
           COUNT(DISTINCT CASE WHEN event_type='question_asked' THEN user_key END) AS askers,
           SUM(CASE WHEN event_type='question_asked' THEN 1 ELSE 0 END) AS questions
    FROM {_t(table)} WHERE {NOT_ADMIN} GROUP BY 1 ORDER BY 1 DESC LIMIT 60"""


def repeat_wrong_values(table):
    """The feedback loop: one value lodged repeatedly and rejected is usually a defensible answer we
    did not band. This is the list to fold into a clue's accept-list between weeks."""
    return f"""
    SELECT clue_id, extracted_value, COUNT(*) AS lodges, COUNT(DISTINCT user_key) AS players
    FROM {_t(table)}
    WHERE verdict IN ('wrong','near') AND extracted_value IS NOT NULL AND extracted_value <> ''
    GROUP BY clue_id, extracted_value
    HAVING COUNT(DISTINCT user_key) >= 2
    ORDER BY players DESC, lodges DESC LIMIT 50"""


def raw_page(table):
    return f"""
    SELECT event_ts, event_type, season_id, user_display_name, user_email, session_id, week_index,
           real_week_index, case_id, clue_id, verdict, points, latency_ms, question_text,
           extracted_value, genie_status, genie_conversation_id, genie_user_id, app_build
    FROM {_t(table)} ORDER BY event_ts DESC LIMIT :lim OFFSET :off"""


def raw_count(table):
    return f"SELECT COUNT(*) AS n FROM {_t(table)}"


def export_all(table):
    """⛔ `expected_value` IS DELIBERATELY NOT PROJECTED HERE, and must never be added back.

    It is the answer key. With it in the export, one URL ends the puzzle for everyone — not a leak of our
    data, a leak of the GAME. Measured on a live deployment: 5 of 13 rows already carried an
    answer, and the operator gate was open to every viewer, so any player could download it.

    Nothing is lost by dropping it: `verdict` already says whether the player was right, and
    `extracted_value` says what they submitted. The delta table still holds the column for anyone with
    table access, which is a different and much smaller population than "anyone who can open the app".

    ⭐ `event_id`, `received_ts` and `extra` ARE projected, and the importer does not work without them.
    Established by checking real exports: not one carried `received_ts`,
    so every export this app had ever produced was missing the ONE column the DDL calls "the only column
    safe to order players by". A restore from such a file brought the points back correct and silently
    re-ranked the board onto the warehouse clock, which `COALESCE(received_ts, event_ts)` hid — values
    restored, ordering corrupted, nothing complaining. `event_id` is the idempotency key: without it a
    second import of the same file doubles `total_questions`, the one number this product exists to move.
    `extra` carries the `admin_setting` payload, i.e. which weeks are released.

    So the rule this docstring is really stating: `expected_value` is withheld because it is the ANSWER;
    everything that is merely a RECORD of what happened belongs in the export, because the export is the
    backup.
    """
    return f"""
    SELECT event_id, event_ts, received_ts, event_type, season_id, user_key, user_email,
           user_display_name, session_id,
           week_index, real_week_index, case_id, clue_id, question_text, genie_conversation_id,
           genie_message_id, genie_user_id, genie_status, extracted_value, verdict,
           points, latency_ms, app_build, extra
    FROM {_t(table)} ORDER BY event_ts DESC LIMIT :lim"""


# ─────────────────────────────────────────────────────────────── the four-week game

def player_weeks(table):
    """One row per week this player has unlocked, with their accumulated FOCUS time.

    Two facts, one query, both keyed on the week:
      * `unlocked_at` — the first `week_unlocked` row, which is when that player started that week.
      * `focus_ms`    — the largest accumulated focus time ever persisted for it. MAX, not LAST, so an
        out-of-order or duplicated write can only ever lose a tick, never rewind somebody's clock.

    The clock PAUSES when the player is not looking at the week, so elapsed time cannot be derived by
    subtracting `unlocked_at` from now — the accumulated figure is the only true one, which is why it is
    persisted rather than recomputed.
    """
    return f"""
    SELECT week_index,
           MIN(CASE WHEN event_type = 'week_unlocked' THEN event_ts END)        AS unlocked_at,
           MAX(CASE WHEN event_type = 'week_time'     THEN latency_ms ELSE 0 END) AS focus_ms
    FROM {_t(table)}
    WHERE user_key = :user_key AND week_index IS NOT NULL {SEASON_FILTER}
      AND event_type IN ('week_unlocked', 'week_time')
    GROUP BY week_index"""
