"""Rules-accurate simulation through phase-sim (copied into the app as routes/farmhouse_phase.py by the Dockerfile).

The phase-rs engine (Rust, headless) plays four-player Commander with a real stack, priority, combat, layers and triggers; the
phase-sim service wraps it. This route turns four SAVED decks into the service's request, relays the job, and returns what the
engine reported. Nothing is simulated here, and a game that stalled, hit the action cap or panicked is reported, never counted as a win.

  GET  /api/farmhouse/phase/status               the service's health (is the engine there, how many jobs are waiting)
  POST /api/farmhouse/phase/run                  {"deckIds": [4 saved deck ids], "games": 1..50, "seed": 1, "difficulty": "Medium",
                                                  "seatDifficulty": {"2": "Hard"}}  ->  {"job": id, "decks": [names]}
  GET  /api/farmhouse/phase/job/{job}            status, progress and, when finished, wins by seat, win rate with a 95% interval,
                                                 warnings (cards the engine did not recognise) and each game's outcome

Setting: PHASE_SIM_URL (e.g. http://phase-sim.commander-lab.svc.cluster.local:8090). Unset, every endpoint answers 503 and nothing else changes.
"""
import json
import os
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.database import _get_db_conn

router = APIRouter(tags=["farmhouse-phase"])


class PhaseRun(BaseModel):
    deckIds: List[int]
    games: int = 1
    seed: int = 1
    difficulty: str = "Medium"
    seatDifficulty: Optional[Dict[str, str]] = None
    actionCap: Optional[int] = None
    # Seats (0-3) a MODEL plays instead of phase's built-in AI, and which kind: {"kind": "chat"|"decision", "model": "twin-turbo", "max_calls": 300}.
    # The model only ever picks among the engine's own legal options; the service falls back to phase's AI on any problem.
    llmSeats: Optional[List[int]] = None
    llm: Optional[Dict] = None


def _base():
    url = os.environ.get("PHASE_SIM_URL", "").rstrip("/")
    if not url:
        raise HTTPException(503, "the rules engine service is not configured (PHASE_SIM_URL)")
    return url


def _call(path, body=None, timeout=20):
    req = urllib.request.Request(_base() + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("error", "")
        except Exception:
            detail = ""
        raise HTTPException(exc.code if exc.code in (400, 404, 413) else 502, detail or "the rules engine service refused the request")
    except (urllib.error.URLError, OSError, ValueError):
        raise HTTPException(503, "the rules engine service is not reachable")


def deck_feed(deck_id):
    """A saved deck as the phase-sim feed entry: {"name", "commander": [...], "main": [{"name", "count"}]}."""
    conn = _get_db_conn()
    deck = conn.execute("SELECT id, name, commander_name FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if not deck:
        raise HTTPException(404, "deck %s not found" % deck_id)
    rows = conn.execute("SELECT card_name, quantity, is_commander FROM deck_cards WHERE deck_id = ?", (deck_id,)).fetchall()
    commanders, main = [], {}
    for r in rows:
        name, qty = (r["card_name"] or "").strip(), int(r["quantity"] or 1)
        if not name:
            continue
        if r["is_commander"]:
            if name not in commanders:
                commanders.append(name)
        else:
            main[name] = main.get(name, 0) + qty
    if not commanders and (deck["commander_name"] or "").strip():
        commanders = [deck["commander_name"].strip()]
    if not commanders:
        raise HTTPException(400, "deck %r has no commander set" % deck["name"])
    main = {n: c for n, c in main.items() if n not in commanders}
    if not main:
        raise HTTPException(400, "deck %r has no cards" % deck["name"])
    return {"name": deck["name"], "commander": commanders, "main": [{"name": n, "count": c} for n, c in sorted(main.items())]}


@router.get("/api/farmhouse/phase/status")
def phase_status():
    return _call("/health", timeout=5)


@router.post("/api/farmhouse/phase/run")
def phase_run(req: PhaseRun):
    if len(req.deckIds) != 4:
        raise HTTPException(400, "pick exactly 4 decks (one per seat)")
    feed = [deck_feed(i) for i in req.deckIds]
    body = {"decks": feed, "games": req.games, "seed": req.seed, "difficulty": req.difficulty}
    if req.seatDifficulty:
        body["seat_difficulty"] = req.seatDifficulty
    if req.actionCap:
        body["action_cap"] = req.actionCap
    if req.llmSeats:
        body["llm_seats"] = req.llmSeats
        body["llm"] = req.llm or {}
    out = _call("/run", body)
    return {"job": out["job"], "decks": [d["name"] for d in feed]}


@router.get("/api/farmhouse/phase/job/{job}")
def phase_job(job: str):
    return _call("/job/" + "".join(c for c in job if c in "0123456789abcdef"), timeout=10)
