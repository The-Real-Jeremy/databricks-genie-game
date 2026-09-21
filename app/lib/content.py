"""Loading the season pack, and the redaction that keeps the answers out of the browser.

Everything a player could win by reading — expected values, accept lists, near bands and the nudges
attached to them — stays on the server. The client is sent prompts, and hints only once it has paid for
them. Without this, view-source is the winning strategy.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT_DIR = os.path.join(os.path.dirname(HERE), "content")

SAFE_CLUE_KEYS = ("clue_id", "label", "prompt", "answer_type", "points", "unit")


class Season:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self.pack = json.load(f)
        self.cases = {c["week"]: c for c in self.pack["cases"]}
        self.clues = {cl["clue_id"]: (c, cl) for c in self.pack["cases"] for cl in c["clues"]}

    @property
    def total_weeks(self):
        return len(self.cases)

    @property
    def requires_tables(self):
        return self.pack.get("requires_tables", [])

    @property
    def space_title(self):
        """One Genie space per scenario — the pack names its own, so switching scenario switches space."""
        return self.pack.get("space_title")

    def meta(self):
        out = {k: self.pack.get(k) for k in ("season_id", "title", "subtitle", "premise",
                                            "archive_name")}
        # The space's own sample questions double as the chat's starter chips — one list, so the chips
        # can never suggest something the space was not set up to answer.
        # The greeting already asked "What can you do?" on this player's behalf, so the chips offer the
        # questions that come AFTER it rather than repeating one they can already see answered.
        qs = [q for q in (self.pack.get("space_sample_questions") or [])
              if q.strip().lower() != "what can you do?"]
        out["starters"] = qs[:4]
        return out

    def clue(self, clue_id):
        return self.clues.get(clue_id, (None, None))

    def case_for_client(self, week, state):
        """A week's file with answers stripped and this player's progress folded in.

        `state` is {clue_id: {"solved": bool, "points": int, "hints": int}}.
        """
        case = self.cases.get(week)
        if not case:
            return None
        out = {k: case.get(k) for k in ("case_id", "week", "title", "dateline", "brief",
                                        "opening_question", "starters")}
        clues, solved = [], 0
        for cl in case["clues"]:
            st = state.get(cl["clue_id"]) or {}
            n_hints = int(st.get("hints") or 0)
            c = {k: cl.get(k) for k in SAFE_CLUE_KEYS if cl.get(k) is not None}
            c["solved"] = bool(st.get("solved"))
            c["earned"] = int(st.get("points") or 0)
            c["hints_taken"] = n_hints
            c["hints_available"] = len(cl.get("hints") or [])
            c["hints"] = list((cl.get("hints") or [])[:n_hints])
            c["next_hint_cost"] = hint_cost(n_hints)
            c["value_if_solved_now"] = points_for(cl, n_hints)
            if c["solved"]:
                c["on_correct"] = cl.get("on_correct") or "Confirmed."
                # Safe to reveal only now: a solved clue's answer is already known to this player, and
                # seeing the figure they proved is the reward. Unsolved clues carry nothing.
                c["revealed_value"] = str(cl.get("expected"))
            solved += 1 if c["solved"] else 0
            clues.append(c)
        out["clues"] = clues
        out["solved_count"] = solved
        out["clue_count"] = len(clues)
        out["complete"] = solved == len(clues)
        out["epilogue"] = case.get("epilogue") if out["complete"] else None
        return out

    def season_board(self, state):
        """The season-long object: one tile per week, only fully lit when that file is closed."""
        board = []
        for wk in sorted(self.cases):
            case = self.cases[wk]
            ids = [cl["clue_id"] for cl in case["clues"]]
            got = sum(1 for i in ids if (state.get(i) or {}).get("solved"))
            board.append({"week": wk, "case_id": case["case_id"], "title": case["title"],
                          "solved": got, "of": len(ids), "closed": got == len(ids)})
        return board


HINT_COSTS = (10, 25, 40)


def hint_cost(hints_already_taken):
    if hints_already_taken >= len(HINT_COSTS):
        return None
    return HINT_COSTS[hints_already_taken]


def points_for(clue, hints_taken, floor=10):
    """Only hints reduce a score. A wrong lodge costs nothing — a strike system would teach people to
    stop asking, and asking is the entire point of the exercise."""
    base = int(clue.get("points", 100))
    spent = sum(HINT_COSTS[:min(hints_taken, len(HINT_COSTS))])
    return max(base - spent, floor)


def discover_seasons():
    out = {}
    if not os.path.isdir(CONTENT_DIR):
        return out
    for fn in sorted(os.listdir(CONTENT_DIR)):
        if fn.endswith(".json"):
            try:
                s = Season(os.path.join(CONTENT_DIR, fn))
                out[s.pack["season_id"]] = s
            except Exception:
                continue
    return out
