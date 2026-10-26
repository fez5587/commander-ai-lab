"""Run:  python -I -X utf8 tests/farmhouse/test_farmhouse_decide.py   (offline: a stub /v1/systemone server on localhost)"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load  # noqa: E402

D = load("farmhouse_decide", "src/commander_ai_lab/sim/farmhouse_decide.py")
FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:200]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


SEEN = []
BEHAVIOUR = {"mode": "ok"}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        SEEN.append((self.path, body, self.headers.get("authorization")))
        if BEHAVIOUR["mode"] == "down":
            self.send_response(500)
            self.end_headers()
            return
        crit = list(body["questions"]["action"]["criteria"])
        pick = "cast_creature" if "cast_creature" in crit else crit[0]
        probs = {k: (0.8 if k == pick else 0.2 / max(len(crit) - 1, 1)) for k in crit}
        out = json.dumps({"answers": {"action": {"type": "choice", "choice": pick, "probabilities": probs, "confidence": 0.5}}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % srv.server_port

SNAP = {"turn": 4, "my_life": 31, "opp_life": 20, "my_mana_total": 4,
        "my_hand": [{"name": "Forest", "type": "land", "cmc": 0, "castable": True},
                    {"name": "Llanowar Elves", "type": "creature", "cmc": 1, "castable": True, "roles": ["ramp"]}, "Sol Ring"],
        "my_board": [{"name": "Bear", "type": "creature", "pt": "2/2"}, {"name": "Rock", "type": "artifact"}],
        "opp_board": [{"name": "Wurm", "type": "creature", "pt": "6/6"}],
        "opp_threat": {"level": "HIGH", "total_power": 6}, "commander": {"name": "Atraxa", "zone": "command zone"}}

# --- the text it reads
t = D.compact_state(SNAP)
ck("the text has the facts the choice depends on", all(x in t for x in ("Turn 4", "life 31", "Llanowar Elves", "Bear (creature, 2/2)", "Wurm", "HIGH")), t)
ck("short enough for a 512-token reader", len(D.compact_state(dict(SNAP, my_hand=[{"name": "Card %d" % i, "type": "creature", "cmc": 3} for i in range(40)]))) < 1900)
ck("an empty snapshot does not raise", isinstance(D.compact_state({}), str))

# --- modes
for k in ("DECISION_URL", "DECISION_MODE", "DECISION_MODEL", "DECISION_API_KEY"):
    os.environ.pop(k, None)
ck("no URL: off, and ask() does nothing", D.mode() == "off" and D.ask(SNAP) is None and not SEEN)
os.environ["DECISION_URL"] = URL
ck("a URL alone is still off (the mode must be chosen)", D.mode() == "off")
os.environ["DECISION_MODE"] = "shadow"
ck("shadow", D.mode() == "shadow")
os.environ["DECISION_MODE"] = "assist"
ck("assist", D.mode() == "assist")
os.environ["DECISION_MODE"] = "bogus"
ck("an unknown mode is off", D.mode() == "off")
os.environ["DECISION_MODE"] = "shadow"

# --- asking
os.environ["DECISION_MODEL"] = "typed-decisions"
os.environ["DECISION_API_KEY"] = "k"
a = D.ask(SNAP, actions=["play_land", "cast_creature", "hold", "not_an_action"])
path, body, auth = SEEN[-1]
ck("it posts to /v1/systemone with the model, key and only the known actions",
   path == "/v1/systemone" and body["model"] == "typed-decisions" and auth == "Bearer k"
   and list(body["questions"]["action"]["criteria"]) == ["play_land", "cast_creature", "hold"], (path, body, auth))
ck("the answer carries the choice, its probability and timing", a and a["choice"] == "cast_creature" and abs(a["confidence"] - 0.8) < 1e-9 and a["ms"] >= 0, a)
d = D.as_decision(a, time.time())
ck("as_decision is a brain result with no target (the engine picks one)", d["action"] == "cast_creature" and d["target_card"] == "" and d["source"] == "decision", d)

# --- shadow bookkeeping
D.record(a, "cast_creature")
D.record(a, "hold")
D.record(None, "hold")
s = D.stats()
ck("agreement is counted over answered questions only", s["asked"] == 2 and s["agreed"] == 1 and s["agreement"] == 0.5 and s["answered_conf"] == 2, s)
ck("per-action tallies", s["by_action"]["hold"] == {"n": 1, "agreed": 0} and s["by_action"]["cast_creature"]["agreed"] == 1)

# --- a server that is down costs nothing after three failures
BEHAVIOUR["mode"] = "down"
n0 = len(SEEN)
res = [D.ask(SNAP) for _ in range(6)]
ck("errors give None, never raise", all(r is None for r in res))
ck("after three failures it stops asking for a while", len(SEEN) - n0 == 3, len(SEEN) - n0)
BEHAVIOUR["mode"] = "ok"
ck("still backing off even though the server is back", D.ask(SNAP) is None)
D._fail["until"] = 0
ck("and asks again once the pause ends", D.ask(SNAP) is not None)
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
