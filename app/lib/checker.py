"""Deterministic answer checking.

There is no FMAPI in the target workspace, so nothing here may *infer* correctness. Every verdict is a
normalised comparison against a value that was computed with SQL at authoring time and shipped in the
content pack. The check looks at the VALUE, never at the user's phrasing — any question that yields the
right value wins, which is what makes the game fair and keeps it about the data rather than the wording.
"""
import re
import unicodedata

CURRENCY = "$£€¥₹"

# Deliberately NOT underscores. Genie emphasises with `**` (measured: `**mastercard**`, `**66,471**`),
# while underscores are everywhere in real column names and values — stripping them turned the answer
# `credit_card` into `creditcard`, which then failed its own clue. Defined here rather than further
# down because to_number and norm_date both need it: a value pasted out of the chat wears its asterisks.
_EMPHASIS_RE = re.compile(r"(\*{1,3}|`+)")
# Includes an exponent AND a non-breaking space as a thousands separator. Measured: Genie returned a
# float SUM as `4.000798716568315E7`, which without the exponent branch parses as 4.0 — a correct
# answer failed by seven orders of magnitude, silently.
_NUM_RE = re.compile(r"[-+]?\d[\d,\u00a0_ ]*(?:\.\d+)?(?:[eE][-+]?\d+)?")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# ---------------------------------------------------------------- normalisation

def norm_text(s, noise_words=()):
    """Casefold, strip accents and punctuation, collapse whitespace, drop authored noise words."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = s.replace("_", " ")          # credit_card, credit card and Credit Card are the same answer
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip().casefold()
    if noise_words:
        drop = {w.casefold() for w in noise_words}
        s = " ".join(t for t in s.split() if t not in drop)
    return s


# A token immediately after the number that CHANGES ITS VALUE. Two behaviours, and the distinction is
# the whole fix:
#   * a magnitude word is APPLIED — `6m` is six million, which then correctly fails a clue whose answer
#     is 6, and `1.08k` correctly passes one whose answer is 1080;
#   * a percent, a ratio or a range is REFUSED outright — there is no single value to compare.
# Ordinary trailing prose ("6 shops") is neither, and still parses as 6, because a person who types the
# right value with a noun after it has found the answer.
_MAG_RE = re.compile(r"^[  ]*(k|thousand|m|mn|million|b|bn|billion)\b\.?", re.I)
_MAG = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
        "b": 1e9, "bn": 1e9, "billion": 1e9}
# `6%` is a proportion, `6/12` and `6:12` are ratios, `6-12` and `6 to 12` are ranges. None of them
# states the number 6.
# ⚠️ NOTE THERE IS NO LEADING-WHITESPACE REQUIREMENT. _NUM_RE already consumes the space after the
#    digits, so an alternative written as `\s+to\s+\d` can never fire — which is exactly how
#    `6 to 12` survived the first attempt at this fix while the edit reported success.
# `6 out of 48` is deliberately NOT refused: it states 6, with context, and rejecting it is a false RED.
# `to\b` cannot fire on "6 total" because "to|tal" has no word boundary between them.
_NOT_A_VALUE_RE = re.compile(r"^[  ]*(%|percent\b|/\s*\d|:\s*\d|-\s*\d|to\b\s*\d)", re.I)


def to_number(s):
    """First number in a string, tolerant of currency, separators and trailing prose — None if there is
    no single number being stated.

    ⛔ THE BUG THIS DOCSTRING USED TO DENY. An earlier version took the first number and discarded
    everything after it, so `6m`, `6k`, `6bn`, `6%`, `6/12` and `6 million` ALL parsed as 6.0 and locked
    GREEN on a clue whose answer is 6 — 63 of 70 false greens in an independent battle test. Worse, the
    comment right here asserted that removing the magnitude branch PREVENTED exactly that. It did the
    opposite: with the branch, `6m` becomes six million and fails; without it, `6m` IS six and matches.
    I had the direction backwards, and a confident wrong comment is worse than the defect because the
    next reader trusts it.

    So: magnitude words are applied, value-changing markers are refused, and plain trailing words are
    ignored. A false green in this game is unrecoverable — a wrong answer locks uneditable — so the
    refusing direction is the safe one wherever a string does not state one number.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    txt = str(s).strip()
    neg = txt.startswith("(") and txt.endswith(")")      # (1,234) accounting negative
    for ch in CURRENCY:
        txt = txt.replace(ch, "")
    txt = txt.replace("A$", "").replace("US$", "")
    txt = _EMPHASIS_RE.sub("", txt)                      # **66,471** pasted straight out of the chat
    m = _NUM_RE.search(txt)
    if not m:
        return None
    raw = re.sub(r"[,_ \u00a0]", "", m.group(0))
    try:
        val = float(raw)
    except ValueError:
        return None
    rest = txt[m.end():]
    if _NOT_A_VALUE_RE.match(rest):
        return None                                      # a proportion, a ratio or a range, not a value
    mag = _MAG_RE.match(rest)
    if mag:
        val *= _MAG[mag.group(1).lower()]
    return -val if neg else val


_MONTHS = {}
for _i, _m in enumerate(("january february march april may june july august september october "
                         "november december").split(), start=1):
    _MONTHS[_m] = _i
    _MONTHS[_m[:3]] = _i
_MONTHS["sept"] = 9


def _mnum(word):
    return _MONTHS.get(str(word).strip(".").lower())


def norm_date(s):
    """ISO date string, or None.

    Every accepted form is one a player can actually end up holding. Measured on the live Genie space:
    this game's one date answer came back as `2024-05-13T00:00:00.000Z` in the result row and
    `**2024-05-13**` in the prose — and a person reading either may type `13 May 2024`, `May 13, 2024`,
    `13/05/2024` or `13-May-2024`. The space is INSTRUCTED to answer dates as YYYY-MM-DD, so all of this
    is a safety net rather than the mechanism.
    """
    if s is None:
        return None
    t = _EMPHASIS_RE.sub("", str(s).strip())[:400]   # 64 cut the date off the END of a prose answer
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)            # 2024-05-13 · 2024-05-13T00:00Z
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[ \-/]+([A-Za-z]{3,9})\.?,?[ \-/]+(\d{4})", t)
    if m and _mnum(m.group(2)):                                       # 13 May 2024 · 13-May-2024
        return f"{m.group(3)}-{_mnum(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"([A-Za-z]{3,9})\.?[ \-/]+(\d{1,2})(?:st|nd|rd|th)?,?[ \-/]+(\d{4})", t)
    if m and _mnum(m.group(1)):                                       # May 13, 2024 · May 13 2024
        return f"{m.group(3)}-{_mnum(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", t)            # 13/05/2024 · 05/13/2024
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        day, month = (a, b) if a > 12 else (b, a) if b > 12 else (a, b)
        return f"{y}-{month:02d}-{day:02d}"
    return None


def split_set(s):
    if isinstance(s, (list, tuple, set)):
        return [str(x) for x in s]
    return [p for p in re.split(r"[,;\n|]+", str(s or "")) if p.strip()]


# ---------------------------------------------------------------- comparison

def _num_match(got, want, tol):
    if got is None or want is None:
        return False
    if tol.get("abs") is not None and abs(got - want) <= float(tol["abs"]):
        return True
    if tol.get("pct") is not None:
        scale = max(abs(want), 1e-9)
        if abs(got - want) / scale * 100.0 <= float(tol["pct"]):
            return True
    return got == want


def _equal(kind, got, want, clue):
    noise = clue.get("noise_words", ())
    if kind == "number":
        return _num_match(to_number(got), to_number(want), clue.get("tolerance") or {})
    if kind == "date":
        g, w = norm_date(got), norm_date(want)
        return bool(g) and g == w
    if kind == "set":
        g = {norm_text(x, noise) for x in split_set(got)} - {""}
        w = {norm_text(x, noise) for x in split_set(want)} - {""}
        if not w:
            return False
        need = clue.get("min_overlap")
        if need:
            return len(g & w) >= int(need)
        return g == w
    g, w = norm_text(got, noise), norm_text(want, noise)
    if g == w != "":
        return True
    # Last resort for multi-token identifiers only: `creditcard` means `credit_card`. Restricted to
    # answers that genuinely have a separator, so ordinary words cannot collide by being squashed.
    if " " in w and len(w) >= 6:
        return g.replace(" ", "") == w.replace(" ", "")
    return False


# ---------------------------------------------------------------- "you pasted the whole sentence"

def _tokens(s, noise=()):
    return norm_text(s, noise).split()


def _contains_value(submitted, want, kind, clue):
    """Does a long answer CONTAIN the right value, without being it?

    This exists because of a measured false red. Genie answers in prose — `**Japan** has the most
    franchises.` — and a player who pastes that sentence into a blank has found the answer but filled the
    box wrongly. A strict compare calls that WRONG, which is indistinguishable from naming the wrong
    country; a substring compare would call it CORRECT, which is worse, because one real answer contains
    two candidate values ("...is amex... **Mastercard** was used most often") and whichever is tested
    first would win arbitrarily.

    So it is neither: it returns True only to say "the value is in there", and the caller turns that into
    a NEAR with an instruction. Near never locks and never scores, so a wrong guess costs nothing.
    Matching is on a TOKEN SUBSEQUENCE, never a raw substring: a substring test fires on "de*pending*".
    """
    noise = clue.get("noise_words", ())
    if kind == "number":
        # EVERY number in the string, not just the first one the parser reaches. Re-added deliberately
        # after a measured false red: Genie's answer to "how many transactions used each payment method"
        # opens "There are **3** payment methods…", so the first number is 3 and the answer 1,083 is four
        # numbers later. A paste of that sentence was scored a flat WRONG, which reads identically to
        # naming the wrong figure. This path can only ever produce a NEAR with an instruction — it never
        # returns `correct`, so it cannot lock a wrong value in green.
        for chunk in re.findall(r"[-+]?\d[\d,\u00a0_ ]*(?:\.\d+)?(?:[eE][-+]?\d+)?",
                                _EMPHASIS_RE.sub("", str(submitted))):
            if _num_match(to_number(chunk), to_number(want), clue.get("tolerance") or {}):
                return True
        return False
    if kind == "date":
        return False        # norm_date already searches the whole string
    hay, needle = _tokens(submitted, noise), _tokens(want, noise)
    if not needle or len(hay) <= len(needle):
        return False
    return any(hay[i:i + len(needle)] == needle for i in range(len(hay) - len(needle) + 1))


TRIM_NUDGE = ("That answer contains the right value — but put just the value in the box, "
              "nothing else around it.")


def check(clue, submitted):
    """-> {verdict: correct|near|wrong|empty, points, message, matched}

    `near` is AUTHORED, never inferred: each clue lists the values you land on when you make the classic
    mistake, with the nudge that names the mistake. Numeric clues also get a band, so an answer that is
    off by a filter is recognised as near even when the exact wrong value was not predicted.
    """
    kind = clue.get("answer_type", "string")
    if submitted is None or str(submitted).strip() == "":
        return {"verdict": "empty", "points": 0, "message": "Nothing lodged yet.", "matched": None}

    for want in [clue.get("expected")] + list(clue.get("accept") or []):
        if want is not None and _equal(kind, submitted, want, clue):
            return {"verdict": "correct", "points": int(clue.get("points", 100)),
                    "message": clue.get("on_correct") or "Filled in.", "matched": want}

    for near in clue.get("near") or []:
        if _equal(kind, submitted, near.get("value"), clue):
            return {"verdict": "near", "points": 0,
                    "message": near.get("nudge") or "Close, but not the value this blank wants.",
                    "matched": near.get("value")}

    band = clue.get("near_band")
    if kind == "number" and band:
        got, want = to_number(submitted), to_number(clue.get("expected"))
        if got is not None and want is not None and got != want:
            scale = max(abs(want), 1e-9)
            within = (band.get("abs") is not None and abs(got - want) <= float(band["abs"])) or (
                band.get("pct") is not None and abs(got - want) / scale * 100.0 <= float(band["pct"]))
            if within:
                return {"verdict": "near", "points": 0,
                        "message": clue.get("on_near")
                                   or "Close — you are in the right area, so something in the "
                                      "question is filtering differently.",
                        "matched": None}

    # Found-it-but-pasted-too-much, recognised as NEAR with an instruction rather than a flat red. See
    # _contains_value: it is deliberately not a substring match and deliberately not a `correct`.
    if len(str(submitted).split()) > 3:
        for want in [clue.get("expected")] + list(clue.get("accept") or []):
            if want is not None and _contains_value(submitted, want, kind, clue):
                return {"verdict": "near", "points": 0, "message": TRIM_NUDGE, "matched": None,
                        "trim": True}

    return {"verdict": "wrong", "points": 0,
            "message": clue.get("on_wrong") or "Not the value this blank wants. Try asking the genie "
                                                "another way.",
            "matched": None}


# ---------------------------------------------------------------- reading Genie's answer

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def drop_repeated_sentences(s):
    """Genie sometimes ends an answer by repeating its last sentence verbatim — measured on the greeting:
    "…Ask your next question. Ask your next question." Dropping an exact consecutive repeat is safe
    because it removes no information; it is deliberately NOT a near-match, so nothing that differs by a
    number can be collapsed."""
    if not s:
        return s
    parts = _SENT_RE.split(str(s).strip())
    out = []
    for p in parts:
        if out and p.strip() and p.strip() == out[-1].strip():
            continue
        out.append(p)
    return " ".join(out)


def strip_markdown(s):
    """Genie's prose answers come back with markdown emphasis — a real answer arrived as
    `**mastercard**`. Left alone it reaches the UI as asterisks and, worse, is what gets written into
    the log as the value the player lodged."""
    if s is None:
        return ""
    return _WS_RE.sub(" ", _EMPHASIS_RE.sub("", str(s))).strip()


def extract_candidates(answer):
    """Values a user could plausibly lodge, pulled from a Genie answer.

    Prefers the RESULT ROWS over the prose: the rows are typed and structured, the prose is a sentence.
    Returns them in the order the UI should offer them, de-duplicated, capped.
    """
    out = []
    for att in (answer or {}).get("attachments", []):
        rows, schema = att.get("rows") or [], att.get("schema") or []
        if rows and len(rows) == 1 and len(rows[0]) == 1:            # single cell: the usual case
            out.append(str(rows[0][0]))
        elif rows and len(rows[0]) == 1:                             # one column, many rows
            out.extend(str(r[0]) for r in rows[:8])
        elif rows:
            skip = {i for i, n in enumerate(schema) if str(n).lower().endswith("id")}
            for r in rows[:5]:
                cells = [(i, str(c)) for i, c in enumerate(r) if i not in skip]
                # A name split across columns is the common case and it broke a real clue: asked who
                # spent the most, Genie returned first_name | last_name | total as three columns, so the
                # player was offered "Lorraine" and "James" and could lodge neither. Offer the joined
                # non-numeric cells FIRST, then the individual ones.
                words = [c for _, c in cells if c and to_number(c) is None]
                if len(words) > 1:
                    out.append(" ".join(words))
                out.extend(c for _, c in cells)
        if att.get("text"):
            out.append(str(att["text"]).strip())
    # Prefer atomic values over whole sentences. Genie returns both the cell (`3333`) and the prose
    # ("There are 3,333 total sales transactions."), and offering the sentence as something to submit
    # invites a "not quite" for a person who was right — the UI should not hand out a losing option.
    # WORD COUNT, not character length: the sentence above is only 41 characters, so a length cutoff
    # generous enough to keep "Austin Almond Biscotti" also keeps the sentence. And prose that merely
    # WRAPS a shorter candidate carries no new information, so it goes; prose that says something else
    # stays, because it may be the only answer available.
    out = [v.strip() for v in out if v and v.strip()]
    atomic = [v for v in out if len(v.split()) <= 4]
    if atomic:
        keep = list(atomic)
        # Compare with whitespace removed as well: norm_text turns "3,333" into "3 333", so a plain
        # substring test never matched the atomic "3333" inside the sentence that contains it.
        squash = lambda x: norm_text(x).replace(" ", "")
        atomic_norms = {squash(v) for v in atomic if squash(v)}
        for v in out:
            if v in atomic:
                continue
            n = squash(v)
            if not any(a in n for a in atomic_norms):
                keep.append(v)
        out = keep
    seen, uniq = set(), []
    for v in out:
        v = strip_markdown(v)
        # de-duplicate on the NORMALISED form: the row value and the prose sentence often carry the
        # same answer in two spellings, and offering both as separate chips is noise.
        key = norm_text(v)
        if v and key and key not in seen and len(v) <= 120:
            seen.add(key)
            uniq.append(v)
    return uniq[:10]
