"""Genie Conversation API, always as the VIEWER.

There is deliberately no service-principal code path in this module. Measured: once the
audience group has CAN_RUN on the space the app's own service principal can also reach Genie, so a
"fall back to the app identity" branch would keep working while silently attributing every question to
the app — which would zero the only number this product exists to move. The attribution check below is
what makes that impossible to do by accident.
"""
import json, time, urllib.error, urllib.request

API = "/api/2.0/genie/spaces"


def _call(host, token, method, path, body=None, timeout=60, deadline=None):
    # A per-socket timeout is not a bound on the whole operation. If a deadline is given, the socket may
    # not outlive it — otherwise a poll starting just inside a 60s budget returned at 114.1s, and the
    # "budget" was really budget PLUS one socket timeout.
    if deadline is not None:
        timeout = max(1.0, min(timeout, deadline - time.time()))
    req = urllib.request.Request(
        host + path, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"message": raw[:400]}
    except Exception as e:                                    # network / timeout
        return -1, {"message": f"{type(e).__name__}: {e}"}


def resolve_space(host, token, title, pinned_id=None):
    """Find the space by title so nothing has to be plumbed from deploy time into app config."""
    if pinned_id:
        return pinned_id, None
    st, body = _call(host, token, "GET", API)
    if st != 200:
        return None, f"could not list Genie spaces (HTTP {st}): {body.get('message', '')[:200]}"
    spaces = [s for s in (body.get("spaces") or []) if s.get("title") == title]
    if not spaces:
        seen = ", ".join(sorted(s.get("title", "?") for s in (body.get("spaces") or []))[:6]) or "none"
        return None, f"no Genie space titled {title!r} is visible to you (visible: {seen})"
    spaces.sort(key=lambda s: s.get("create_time") or "")
    return spaces[0]["space_id"], None


def ask(host, token, space_id, question, viewer_user_id=None, budget=210, poll_s=2.0,
        conversation_id=None):
    """Ask as the viewer; return the answer plus the evidence that it was really the viewer who asked.

    ⛔ THE BUDGET IS A BOUND ON THE WHOLE CALL, and it did not used to be. Three holes, all measured by an
    independent battle test, and the third is the one that mattered:
      1. the budget was checked before each poll while each poll carried its own 60s socket timeout, so a
         "60s" greeting returned at 114.1s — the real bound was budget + 60s;
      2. the per-attachment query-result fetches ran AFTER the loop with NO budget check at all, 60s each;
      3. and because those fetches recorded `result_error` on the attachment while nothing read it, a call
         that took 120.0s came back **ok: TRUE** with text None — so the client took the SUCCESS path, the
         bubble read "(no answer text)" and the lamp went to answering. A success after a two-minute
         failure is the one outcome that makes a health check worthless, which is what this feature is.
    `elapsed_s` now also spans the result fetches; it used to exclude them and under-reported every real
    question (wall 15.9s against a reported 11.1s).
    """
    t0 = time.time()
    deadline = t0 + budget
    if conversation_id:
        st, r = _call(host, token, "POST",
                      f"{API}/{space_id}/conversations/{conversation_id}/messages", {"content": question},
                      deadline=deadline)
    else:
        st, r = _call(host, token, "POST", f"{API}/{space_id}/start-conversation", {"content": question},
                      deadline=deadline)
    if st != 200:
        return {"ok": False, "stage": "start", "http": st,
                "error": r.get("message") or r.get("error_code") or "could not reach the archive",
                "expired": st == 401, "elapsed_s": round(time.time() - t0, 1)}
    cid = r.get("conversation_id") or conversation_id
    mid = r.get("message_id") or (r.get("id"))
    msg = {}
    while time.time() < deadline:
        st, msg = _call(host, token, "GET", f"{API}/{space_id}/conversations/{cid}/messages/{mid}",
                        deadline=deadline)
        if st != 200:
            return {"ok": False, "stage": "poll", "http": st,
                    "error": msg.get("message") or "lost contact with the archive",
                    "expired": st == 401, "elapsed_s": round(time.time() - t0, 1)}
        if msg.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
            break
        time.sleep(min(poll_s, max(0.0, deadline - time.time())))
    else:
        return {"ok": False, "stage": "timeout", "error": "the archive did not answer in time",
                "conversation_id": cid, "message_id": mid, "elapsed_s": round(time.time() - t0, 1)}

    genie_user_id = str(msg.get("user_id") or "")
    out = {"ok": msg.get("status") == "COMPLETED", "status": msg.get("status"),
           "conversation_id": cid, "message_id": mid, "genie_user_id": genie_user_id,
           "attributed_to_viewer": bool(viewer_user_id) and genie_user_id == str(viewer_user_id),
           "elapsed_s": round(time.time() - t0, 1), "attachments": []}
    for att in msg.get("attachments") or []:
        rec = {"attachment_id": att.get("attachment_id")}
        if att.get("text"):
            rec["text"] = (att["text"] or {}).get("content")
        if att.get("query"):
            q = att["query"]
            rec["sql"] = q.get("query")
            rec["description"] = q.get("description")
            if time.time() >= deadline:
                # Out of budget before this attachment's rows could be fetched. Recorded so the caller
                # can tell a partial answer from a whole one, instead of the old behaviour: fetch anyway,
                # for another 60 seconds, outside the budget entirely.
                rec["result_error"] = "budget exhausted before the result could be fetched"
                out["attachments"].append(rec)
                continue
            st2, qr = _call(host, token, "GET",
                            f"{API}/{space_id}/conversations/{cid}/messages/{mid}"
                            f"/attachments/{att.get('attachment_id')}/query-result",
                            deadline=deadline)
            sr = qr.get("statement_response") or {}
            rec["rows"] = ((sr.get("result") or {}).get("data_array") or [])[:50]
            rec["schema"] = [c.get("name") for c in
                             ((sr.get("manifest") or {}).get("schema") or {}).get("columns", [])]
            rec["row_count"] = (sr.get("manifest") or {}).get("total_row_count")
            if st2 != 200:
                rec["result_error"] = qr.get("message") or f"HTTP {st2}"
        if len(rec) > 1:
            out["attachments"].append(rec)
    # ⛔ THE SUCCESS TEST HAS TO LOOK AT WHAT CAME BACK, not merely at whether anything did. The old guard
    #    was `ok and not attachments`, which CANNOT FIRE when the attachments exist and every one of them
    #    carries a result_error — exactly the 120-second failure that reported ok: TRUE.
    out["elapsed_s"] = round(time.time() - t0, 1)      # now spans the result fetches too
    if out["ok"]:
        atts = out["attachments"]
        usable = [a for a in atts if a.get("text") or a.get("rows")]
        errs = [a["result_error"] for a in atts if a.get("result_error")]
        if not atts:
            out["ok"] = False
            out["error"] = "the genie answered but sent nothing back"
        elif not usable:
            out["ok"] = False
            out["error"] = ("the genie started answering but its result never arrived"
                            + (f" ({errs[0]})" if errs else ""))
        elif errs:
            # Partially usable: say so rather than presenting it as whole.
            out["partial"] = errs
    if time.time() >= deadline and not out.get("ok"):
        out["timed_out"] = True
    return out
