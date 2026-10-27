"""Laya connections beyond "which macro action?" (the decision model also serves Kev: POST /v1/systemone).

Laya answers typed questions about a short text in ONE forward pass, so extra questions in the same request cost almost nothing. This
module is where the repository uses that, without touching CSIE, ITI or PBIE:

  advise_concrete / attach_concrete   Forge's priority path. Laya picks among the SAME concrete candidate IDs Forge sent (never an
                                      ID it did not offer). shadow: recorded beside CSIE's pick. assist: may replace it when sure,
                                      but never when CSIE's pick carries a Forge-established `stops_immediate_win` or `wins_now`.
  extra questions                     "how dangerous are the opponents' boards?" and "is there a threat that must be answered?" ride
                                      in the same call and are recorded (a free weak signal for the learned-threat work).
  capture                             every shadow/assist call is appended to data/decisions/laya-shadow-<date>.jsonl as
                                      `laya-shadow-v1`: the text Laya read, the options, its answer, and the other deciders' picks.
                                      This is the raw material for fine-tuning (scripts/farmhouse_laya_dataset.py): Laya is a base to
                                      specialise, and zero-shot it is no better than guessing (farmhouse/laya/LAYA-EVALUATION.md).
  classify_roles                      card role evidence from Oracle text (batch, cached), with provenance, for ITI / Tagger-style use.

Hidden information: text is built from public facts and the ACTING player's own hand. An opponent's `hand` field is never read.
Failures (no server, timeout, a bad answer) return None and never raise; three in a row pause asking for a minute.

Settings (shared with farmhouse_decide): DECISION_URL, DECISION_MODE (off|shadow|assist), DECISION_MODEL, DECISION_MIN_CONF,
DECISION_TIMEOUT (5 s), DECISION_TIMEOUT_FORGE (1.5 s: Forge waits on this), DECISION_API_KEY, LAYA_CAPTURE_DIR, LAYA_CAPTURE=0 to disable.
"""
import datetime
import json
import os
import threading
import time
import urllib.request

CAPTURE_SCHEMA = "laya-shadow-v1"

THREAT_LEVELS = {
    "none": "no meaningful pressure on you",
    "low": "a small board that is not a danger yet",
    "moderate": "a board that will become dangerous if ignored",
    "high": "a board that threatens you within a turn or two",
    "lethal": "a board or stack that can kill you or win the game very soon",
}

ROLE_HELP = {
    "removal": "destroys, exiles or otherwise removes one permanent or creature",
    "board_wipe": "destroys or removes many creatures or permanents at once",
    "ramp": "adds mana or fetches lands so you have more mana",
    "card_draw": "draws or otherwise gives you extra cards",
    "counterspell": "counters a spell or ability",
    "protection": "protects your permanents or players from removal or damage",
    "tutor": "searches your library for a card other than a basic land",
    "creature": "a creature whose main job is attacking, blocking or generating value on the battlefield",
    "other": "none of the above",
}

_lock = threading.Lock()
_fail = {"n": 0, "until": 0.0}
_role_cache = {}
_capture_lock = threading.Lock()


def mode():
    m = os.environ.get("DECISION_MODE", "off").strip().lower()
    return m if m in ("shadow", "assist") and os.environ.get("DECISION_URL") else "off"


def min_conf():
    try:
        return float(os.environ.get("DECISION_MIN_CONF", "0.7"))
    except ValueError:
        return 0.7


def _post(path, body, timeout):
    """JSON reply or None. Circuit breaker: three failures in a row pause for a minute."""
    url = os.environ.get("DECISION_URL")
    if not url:
        return None
    with _lock:
        if _fail["until"] > time.time():
            return None
    headers = {"content-type": "application/json"}
    if os.environ.get("DECISION_API_KEY"):
        headers["authorization"] = "Bearer " + os.environ["DECISION_API_KEY"]
    try:
        req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(), headers=headers)
        out = json.load(urllib.request.urlopen(req, timeout=timeout))
        with _lock:
            _fail["n"] = 0
        return out
    except Exception:
        with _lock:
            _fail["n"] += 1
            if _fail["n"] >= 3:
                _fail["until"] = time.time() + 60
                _fail["n"] = 0
        return None


def _extra_questions():
    return {"threat": {"type": "choice", "instructions": "How dangerous are the opponents' boards to you right now?",
                       "criteria": dict(THREAT_LEVELS)},
            "must_answer": {"type": "noul", "instructions": "Is there an immediate threat that must be answered this turn?"}}


def _extras(answers):
    out = {}
    t = (answers or {}).get("threat") or {}
    if t.get("choice") in THREAT_LEVELS:
        probs = t.get("probabilities") or {}
        out["threat"] = {"choice": t["choice"], "confidence": round(float(probs.get(t["choice"], 0.0)), 4)}
    m = (answers or {}).get("must_answer") or {}
    if "noul" in m:
        try:
            out["must_answer"] = round(float(m["noul"]), 4)
        except (TypeError, ValueError):
            pass
    return out


# ---------- Forge's priority path -------------------------------------------------------------------------------------------

def _names(battlefield, limit=8):
    out = []
    for c in battlefield or []:
        out.append(str(c.get("name", "?")) if isinstance(c, dict) else str(c))
        if len(out) >= limit:
            break
    return out


def observation_text(obs, ranking=None):
    """Public facts plus the acting player's own hand. An opponent's `hand` field is NEVER read."""
    actor = int(obs.get("priority_seat", 0))
    players = obs.get("players") or []
    parts = ["Phase %s, seat %s has priority (active seat %s)." % (obs.get("phase", "?"), actor, obs.get("active_seat", actor))]
    for p in players:
        seat = p.get("seat")
        head = "You (seat %s)" % seat if seat == actor else "Opponent seat %s" % seat
        bits = ["life %s" % p.get("life", "?"), "power on board %s" % p.get("total_power", 0)]
        if seat == actor:
            bits.append("mana available %s" % p.get("mana_available", "?"))
            hand = p.get("hand")                       # the actor's own hand only
            if isinstance(hand, list) and hand:
                bits.append("hand: " + ", ".join(_names(hand, 10)))
            else:
                bits.append("%s cards in hand" % p.get("cards_in_hand", "?"))
        else:
            bits.append("%s cards in hand" % p.get("cards_in_hand", p.get("hand_count", "?")))
        board = _names(p.get("battlefield"))
        bits.append("board: %s" % (", ".join(board) if board else "empty"))
        parts.append("%s: %s." % (head, "; ".join(bits)))
    stack = obs.get("stack") or []
    if stack:
        parts.append("Stack: %s." % "; ".join(
            "%s by seat %s%s" % (s.get("card", s.get("name", "a spell")), s.get("controller_seat", "?"),
                                 " (immediate win threat)" if s.get("immediate_win_threat") else "") for s in stack[:5]))
    else:
        parts.append("The stack is empty.")
    rows = (ranking or {}).get("ranked") or []
    if rows:
        parts.append("CSIE relative ranking: %s." % "; ".join(
            "%s %.2f" % (r["id"].rsplit(":", 1)[-1], r["score"]) for r in rows[:5]))
    return " ".join(parts)


def describe_candidate(c):
    """What the candidate IS (kind, card, cost, tags). The opaque request-scoped ID is the key, never the description."""
    kind = str(c.get("kind", "other"))
    if kind == "pass":
        return "pass priority and do nothing"
    bits = [kind.replace("_", " ")]
    name = c.get("card_name") or c.get("card") or c.get("name")
    if name:
        bits.append(str(name))
    if c.get("mana_value") not in (None, ""):
        bits.append("mana value %s" % c["mana_value"])
    if c.get("target_seat") is not None:
        bits.append("targeting seat %s" % c["target_seat"])
    tags = [str(t) for t in (c.get("tags") or [])]
    if tags:
        bits.append("tags " + ", ".join(tags))
    return ", ".join(bits)


def advise_concrete(observation, candidates, ranking=None):
    """Laya's pick among Forge's concrete candidate IDs, or None. Shape: choice, confidence, probabilities, extras, ms, model, state_text, options."""
    ids = [c.get("id") for c in candidates if isinstance(c.get("id"), str)]
    if not ids or len(ids) != len(set(ids)) or mode() == "off":
        return None
    text = observation_text(observation, ranking)
    options = {c["id"]: describe_candidate(c) for c in candidates if isinstance(c.get("id"), str)}
    questions = {"action": {"type": "choice", "instructions": "Which action is best right now?", "criteria": options}}
    questions.update(_extra_questions())
    body = {"state": {"body": text}, "questions": questions}
    if os.environ.get("DECISION_MODEL"):
        body["model"] = os.environ["DECISION_MODEL"]
    t0 = time.time()
    out = _post("/v1/systemone", body, float(os.environ.get("DECISION_TIMEOUT_FORGE", "1.5")))
    try:
        ans = out["answers"]["action"]
        choice, probs = ans["choice"], ans.get("probabilities") or {}
        if choice not in options:
            return None
        return {"choice": choice, "confidence": float(probs.get(choice, ans.get("answer_confidence", 0.0))),
                "probabilities": probs, "extras": _extras(out["answers"]), "ms": round((time.time() - t0) * 1000, 1),
                "model": os.environ.get("DECISION_MODEL") or "auto", "state_text": text, "options": options}
    except Exception:
        return None


def attach_concrete(result, observation, candidates, advice):
    """Add Laya's view to the concrete-decision response; in assist mode it may replace the selection (guarded).

    result is the dict /api/policy/decide/concrete returns ({"selected_id", "ranked", ...}). Forge executes only an ID it sent."""
    if not advice:
        return result
    by_id = {c.get("id"): c for c in candidates}
    csie_pick = result.get("selected_id")
    view = {k: advice[k] for k in ("choice", "confidence", "probabilities", "extras", "ms", "model")}
    view["agrees_with_selected"] = advice["choice"] == csie_pick
    out = dict(result)
    out["laya"] = view
    protected = bool(set((by_id.get(csie_pick) or {}).get("tags") or []) & {"stops_immediate_win", "wins_now"})
    if (mode() == "assist" and advice["choice"] in by_id and advice["choice"] != csie_pick
            and advice["confidence"] >= min_conf() and not protected):
        out["csie_selected_id"] = csie_pick
        out["selected_id"] = advice["choice"]
        out["laya_override"] = True
    capture({"source": "forge", "state_text": advice["state_text"], "options": advice["options"],
             "laya": view, "labels": {"csie": csie_pick, "selected": out["selected_id"]},
             "game_id": (observation or {}).get("game_id")})
    return out


# ---------- recording (also the fine-tuning raw material) --------------------------------------------------------------------

def capture(record):
    """Append one laya-shadow-v1 record. Never raises."""
    if os.environ.get("LAYA_CAPTURE", "1") == "0" or mode() == "off":
        return
    try:
        d = os.environ.get("LAYA_CAPTURE_DIR", os.path.join("data", "decisions"))
        os.makedirs(d, exist_ok=True)
        rec = dict(record, schema=CAPTURE_SCHEMA, ts=round(time.time(), 3), mode=mode())
        path = os.path.join(d, "laya-shadow-%s.jsonl" % datetime.datetime.utcnow().strftime("%Y%m%d"))
        with _capture_lock, open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


# ---------- card role evidence ------------------------------------------------------------------------------------------------

def classify_roles(cards, batch_size=32):
    """{name: {"role", "confidence", "probabilities", "source"}} for cards given as {"name", "oracle", "type_line"}.

    One batched request per `batch_size` unseen cards (the server shares forward passes), cached by name. This is evidence with
    provenance (source says model and mode), never a rules fact; a card the server cannot classify is simply absent."""
    out, todo = {}, []
    for c in cards:
        n = c.get("name")
        if not n:
            continue
        if n in _role_cache:
            out[n] = _role_cache[n]
        elif all(n != t["name"] for t in todo):
            todo.append(c)
    questions = {"role": {"type": "choice", "instructions": "What is this card's main role in a Commander deck?", "criteria": dict(ROLE_HELP)}}
    model = os.environ.get("DECISION_MODEL") or "auto"
    for i in range(0, len(todo), batch_size):
        chunk = todo[i:i + batch_size]
        body = {"states": [{"body": ("%s. %s. %s" % (c["name"], c.get("type_line", ""), c.get("oracle", "")))[:1500]} for c in chunk],
                "questions": questions}
        if os.environ.get("DECISION_MODEL"):
            body["model"] = os.environ["DECISION_MODEL"]
        res = _post("/v1/systemone/batch", body, float(os.environ.get("DECISION_TIMEOUT", "5")) * max(2, len(chunk) // 4))
        results = (res or {}).get("results") or []
        for c, r in zip(chunk, results):
            try:
                a = r["answers"]["role"]
                probs = a.get("probabilities") or {}
                if a["choice"] not in ROLE_HELP:
                    continue
                row = {"role": a["choice"], "confidence": round(float(probs.get(a["choice"], 0.0)), 4), "probabilities": probs,
                       "source": "laya:%s" % model}
                _role_cache[c["name"]] = out[c["name"]] = row
            except Exception:
                continue
    return out


# ---------- fine-tuning rows --------------------------------------------------------------------------------------------------

def training_row(rec, label="llm"):
    """One /v1/systemone-shaped training example from a laya-shadow-v1 record, or None when it has no usable label.

    label: which decider is the teacher: llm, csie, selected. The row carries provenance so a student trained on one teacher can be
    told apart from one trained on another, and the options (not just the winner) so the model learns to choose among them."""
    labels = rec.get("labels") or {}
    target = labels.get(label)
    options = rec.get("options") or {}
    if not rec.get("state_text") or target not in options:
        return None
    return {"state": {"body": rec["state_text"]},
            "questions": {"action": {"type": "choice", "instructions": "Which action is best right now?", "criteria": options}},
            "answer": {"action": target},
            "meta": {"schema": "laya-train-v1", "source": rec.get("source"), "teacher": label, "game_id": rec.get("game_id"),
                     "laya_choice": (rec.get("laya") or {}).get("choice"), "ts": rec.get("ts")}}
