"""A fast "which action?" answer from a decision model (Laya, or Kev: both serve POST /v1/systemone).

The LLM brain answers the same question about 25 times a game, each taking seconds. A decision model reads a short text and picks ONE of
a fixed list of options in a single forward pass (tens of milliseconds on a GPU, about a second on a small CPU) and never writes text.
It cannot choose a target card; the engine's heuristics do that, exactly as when the LLM names no target.

Modes (DECISION_MODE; with no DECISION_URL nothing here runs at all):
  off     do nothing
  shadow  ask it too and RECORD whether it agreed with the LLM; the game is played with the LLM's answer (the safe way to measure)
  assist  ask it first; use its answer when it is at least DECISION_MIN_CONF sure, otherwise ask the LLM as before

Settings: DECISION_URL (e.g. http://laya:8080), DECISION_MODEL (a Laya checkpoint name such as english or typed-decisions),
DECISION_MIN_CONF (default 0.7, on the probability of the chosen option), DECISION_TIMEOUT (seconds, default 5), DECISION_API_KEY.
After three failures in a row the model is not asked again for a minute, so a down server costs the game nothing.
"""
import json
import os
import threading
import time
import urllib.request

ACTION_HELP = {
    "play_land": "play a land from hand (always do this first)",
    "cast_creature": "cast a creature spell",
    "cast_removal": "use removal on the opponent's best threat",
    "cast_board_wipe": "wipe the board when behind on creatures",
    "cast_ramp": "cast mana ramp to accelerate",
    "cast_spell": "cast any other spell such as an enchantment or artifact",
    "attack_all": "attack with all untapped creatures",
    "attack_safe": "attack only with evasive or large creatures",
    "hold": "pass and save mana",
}

_lock = threading.Lock()
_fail = {"n": 0, "until": 0.0}
STATS = {"asked": 0, "agreed": 0, "answered_conf": 0, "errors": 0, "ms_total": 0.0, "by_action": {}}


def mode():
    m = os.environ.get("DECISION_MODE", "off").strip().lower()
    return m if m in ("shadow", "assist") and os.environ.get("DECISION_URL") else "off"


def _card_text(c):
    if isinstance(c, str):
        return c
    bits = [str(c.get("type") or "").strip()]
    if c.get("cmc") is not None:
        bits.append("cmc %s" % c["cmc"])
    if c.get("pt"):
        bits.append(str(c["pt"]))
    if c.get("castable") is not None:
        bits.append("castable" if c["castable"] else "not castable")
    roles = c.get("roles")
    if roles:
        bits.append("/".join(roles) if isinstance(roles, (list, tuple)) else str(roles))
    return "%s (%s)" % (c.get("name", "?"), ", ".join(b for b in bits if b))


def compact_state(snap):
    """The snapshot as a short paragraph (a decision model reads about 500 tokens, the LLM's indented JSON is two to three times that)."""
    hand = snap.get("my_hand") or []
    mine = [c for c in (snap.get("my_board") or []) if isinstance(c, dict)]
    theirs = [c for c in (snap.get("opp_board") or []) if isinstance(c, dict)]
    my_creatures = [c for c in mine if "creature" in str(c.get("type", "")).lower()]
    opp_creatures = [c for c in theirs if "creature" in str(c.get("type", "")).lower()]
    threat = snap.get("opp_threat") or {}
    cmdr = snap.get("commander") or {}
    parts = ["Turn %s. Your life %s, opponent life %s. Mana available %s." % (
        snap.get("turn"), snap.get("my_life"), snap.get("opp_life"), snap.get("my_mana_total", snap.get("my_mana_available")))]
    parts.append("Opponent threat %s, total power %s." % (threat.get("level", "?"), threat.get("total_power", "?")))
    if cmdr:
        parts.append("Your commander %s is in the %s." % (cmdr.get("name", "?"), cmdr.get("zone", "?")))
    parts.append("Hand: %s." % ("; ".join(_card_text(c) for c in hand[:10]) if hand else "empty"))
    parts.append("Your creatures: %s." % (", ".join(_card_text(c) for c in my_creatures[:8]) if my_creatures else "none"))
    parts.append("Your other permanents: %d." % (len(mine) - len(my_creatures)))
    parts.append("Opponent creatures: %s." % (", ".join(_card_text(c) for c in opp_creatures[:8]) if opp_creatures else "none"))
    parts.append("Opponent other permanents: %d." % (len(theirs) - len(opp_creatures)))
    advice = snap.get("strategic_advice") or {}
    ranking = (advice.get("ranking") or {}).get("ranked") or []
    if ranking:
        parts.append("CSIE relative ranking: %s." % "; ".join(
            "%s %.2f (%s)" % (r["id"], r["score"], r["explanation"])
            for r in ranking[:5]))
    if snap.get("stack_supported") and snap.get("stack"):
        parts.append("Visible stack: %s." % json.dumps(snap["stack"], separators=(",", ":")))
    return " ".join(parts)


def ask(snap, actions=None):
    """{'choice', 'confidence', 'probabilities', 'ms', 'model'} or None (not configured, backing off, or an error)."""
    url = os.environ.get("DECISION_URL")
    if not url:
        return None
    now = time.time()
    with _lock:
        if _fail["until"] > now:
            return None
    acts = [a for a in (actions or ACTION_HELP) if a in ACTION_HELP]
    body = {"state": {"body": compact_state(snap)},
            "questions": {"action": {"type": "choice", "instructions": "Which action is best for this turn?",
                                     "criteria": {a: ACTION_HELP[a] for a in acts}}}}
    model = os.environ.get("DECISION_MODEL")
    if model:
        body["model"] = model
    headers = {"content-type": "application/json"}
    if os.environ.get("DECISION_API_KEY"):
        headers["authorization"] = "Bearer " + os.environ["DECISION_API_KEY"]
    t0 = time.time()
    try:
        req = urllib.request.Request(url.rstrip("/") + "/v1/systemone", data=json.dumps(body).encode(), headers=headers)
        out = json.load(urllib.request.urlopen(req, timeout=float(os.environ.get("DECISION_TIMEOUT", "5"))))
        ans = out["answers"]["action"]
        probs = ans.get("probabilities") or {}
        choice = ans["choice"]
        if choice not in acts:
            raise ValueError("Decision model selected an action outside the supplied candidates")
        with _lock:
            _fail["n"] = 0
        return {"choice": choice, "confidence": float(probs.get(choice, ans.get("answer_confidence", 0.0))),
                "probabilities": probs, "ms": round((time.time() - t0) * 1000, 1), "model": model or "auto"}
    except Exception:
        with _lock:
            _fail["n"] += 1
            STATS["errors"] += 1
            if _fail["n"] >= 3:
                _fail["until"] = time.time() + 60
                _fail["n"] = 0
        return None


def as_decision(answer, t_start):
    """The brain's own result dict for a decision model's answer (no target card: the engine's heuristics pick it)."""
    return {"action": answer["choice"], "target_card": "", "source": "decision",
            "reasoning": "decision model %s, p=%.2f" % (answer["model"], answer["confidence"]),
            "latency_ms": round((time.time() - t_start) * 1000, 1)}


def record(answer, llm_action):
    """Count one shadow comparison."""
    if not answer:
        return
    with _lock:
        STATS["asked"] += 1
        STATS["ms_total"] += answer["ms"]
        agree = answer["choice"] == llm_action
        STATS["agreed"] += 1 if agree else 0
        if answer["confidence"] >= float(os.environ.get("DECISION_MIN_CONF", "0.7")):
            STATS["answered_conf"] += 1
        row = STATS["by_action"].setdefault(llm_action, {"n": 0, "agreed": 0})
        row["n"] += 1
        row["agreed"] += 1 if agree else 0


def stats():
    with _lock:
        n = STATS["asked"]
        return dict(STATS, agreement=round(STATS["agreed"] / n, 3) if n else None, avg_ms=round(STATS["ms_total"] / n, 1) if n else None)
