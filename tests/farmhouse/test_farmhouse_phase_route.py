"""Run INSIDE the image (needs the app's FastAPI): python /tmp/test_farmhouse_phase_route.py
Checks routes/farmhouse_phase.py against an in-memory database and a stubbed phase-sim service."""
import json
import os
import sqlite3
import sys

import os as _os; sys.path[:0] = [_os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")), _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", "src"))]
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import routes.farmhouse_phase as R  # noqa: E402

FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:240]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


db = sqlite3.connect(":memory:", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT, commander_name TEXT DEFAULT '');
CREATE TABLE deck_cards (id INTEGER PRIMARY KEY, deck_id INTEGER, scryfall_id TEXT DEFAULT '', card_name TEXT, quantity INTEGER DEFAULT 1, is_commander INTEGER DEFAULT 0);
INSERT INTO decks VALUES (1, 'Atraxa', ''), (2, 'No commander', ''), (3, 'Empty', 'Solo'), (4, 'Named only', 'Ghave');
INSERT INTO deck_cards (deck_id, card_name, quantity, is_commander) VALUES
  (1, 'Atraxa, Praetors'' Voice', 1, 1), (1, 'Sol Ring', 1, 0), (1, 'Forest', 20, 0), (1, 'Forest', 3, 0), (1, 'Fire // Ice', 1, 0), (1, 'Atraxa, Praetors'' Voice', 1, 0),
  (2, 'Sol Ring', 1, 0),
  (4, 'Sol Ring', 1, 0);
""")
R._get_db_conn = lambda: db

feed = R.deck_feed(1)
ck("the commander row is the commander, and is not also in the main list", feed["commander"] == ["Atraxa, Praetors' Voice"]
   and all(e["name"] != "Atraxa, Praetors' Voice" for e in feed["main"]), feed)
ck("duplicate rows add up (20 + 3 Forests) and split cards keep their full name", {"name": "Forest", "count": 23} in feed["main"]
   and {"name": "Fire // Ice", "count": 1} in feed["main"], feed["main"])
ck("a deck with only a named commander uses it", R.deck_feed(4)["commander"] == ["Ghave"])
for did, why in ((2, "no commander"), (3, "no cards"), (99, "missing")):
    try:
        R.deck_feed(did)
        ck("refuses a deck with " + why, False)
    except Exception as exc:
        ck("refuses a deck with " + why, getattr(exc, "status_code", 0) in (400, 404), exc)

app = FastAPI()
app.include_router(R.router)
client = TestClient(app)
os.environ.pop("PHASE_SIM_URL", None)
ck("not configured: 503, nothing else", client.get("/api/farmhouse/phase/status").status_code == 503
   and client.post("/api/farmhouse/phase/run", json={"deckIds": [1, 1, 1, 1]}).status_code == 503)

os.environ["PHASE_SIM_URL"] = "http://phase-sim.invalid:8090"
calls = []
def fake_call(path, body=None, timeout=20):
    calls.append((path, body))
    return {"job": "abc123def456"} if path == "/run" else {"ok": True, "status": "done", "path": path}
R._call = fake_call
ck("fewer than four decks is a 400", client.post("/api/farmhouse/phase/run", json={"deckIds": [1, 4]}).status_code == 400)
r = client.post("/api/farmhouse/phase/run", json={"deckIds": [1, 4, 1, 4], "games": 6, "seed": 5, "difficulty": "Hard", "seatDifficulty": {"2": "VeryHard"}})
body = calls[-1][1]
ck("the run is forwarded with four feed decks, the games, seed, difficulty and per-seat difficulty",
   r.status_code == 200 and r.json() == {"job": "abc123def456", "decks": ["Atraxa", "Named only", "Atraxa", "Named only"]}
   and calls[-1][0] == "/run" and len(body["decks"]) == 4 and body["games"] == 6 and body["seed"] == 5 and body["difficulty"] == "Hard"
   and body["seat_difficulty"] == {"2": "VeryHard"}, (r.status_code, r.text, body))
r = client.post("/api/farmhouse/phase/run", json={"deckIds": [1, 4, 1, 4], "llmSeats": [1, 3], "llm": {"kind": "chat", "model": "twin-turbo", "max_calls": 80}})
body = calls[-1][1]
ck("model seats and the model settings are forwarded as the service names them", r.status_code == 200 and body["llm_seats"] == [1, 3]
   and body["llm"] == {"kind": "chat", "model": "twin-turbo", "max_calls": 80}, (r.status_code, body))
client.post("/api/farmhouse/phase/run", json={"deckIds": [1, 4, 1, 4]})
ck("no model seats: nothing about models is sent", "llm_seats" not in calls[-1][1] and "llm" not in calls[-1][1])
ck("a job id is passed through and cleaned of anything but hex", client.get("/api/farmhouse/phase/job/abc123").json()["path"] == "/job/abc123"
   and client.get("/api/farmhouse/phase/job/abc123..%2f..%2fx").status_code in (200, 404, 422))
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
