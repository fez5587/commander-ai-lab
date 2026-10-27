"""Run:  python -I -X utf8 tests/farmhouse/test_farmhouse_laya.py   (offline: a stub /v1/systemone server on localhost)

Loads the two modules by file path and registers them under a stub `commander_ai_lab.sim` package, so no upstream package code runs."""
import json
import os
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load  # noqa: E402

L = load("farmhouse_laya", "src/commander_ai_lab/sim/farmhouse_laya.py")
pkg, sim = types.ModuleType("commander_ai_lab"), types.ModuleType("commander_ai_lab.sim")
pkg.sim, sim.farmhouse_laya = sim, L
sys.modules.update({"commander_ai_lab": pkg, "commander_ai_lab.sim": sim, "commander_ai_lab.sim.farmhouse_laya": L})
D = load("farmhouse_decide", "src/commander_ai_lab/sim/farmhouse_decide.py")

FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:240]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


SEEN = []
B = {"pick": None, "conf": 0.9, "delay": 0.0, "bogus": False}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _answer(self, qs):
        ans = {}
        for name, q in qs.items():
            crit = list(q.get("criteria") or [])
            if q["type"] == "choice":
                pick = B["pick"] if B["pick"] in crit else crit[0]
                if B["bogus"] and name == "action":
                    pick = "not_offered"
                probs = {k: (B["conf"] if k == pick else (1 - B["conf"]) / max(len(crit) - 1, 1)) for k in crit}
                ans[name] = {"type": "choice", "choice": pick, "probabilities": probs}
            else:
                ans[name] = {"type": "noul", "noul": 0.25}
        return ans

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        SEEN.append((self.path, body))
        time.sleep(B["delay"])
        if self.path.endswith("/batch"):
            out = {"results": [{"answers": self._answer(body["questions"])} for _ in body["states"]]}
        else:
            out = {"answers": self._answer(body["questions"])}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["DECISION_URL"] = "http://127.0.0.1:%d" % srv.server_port
CAP = tempfile.mkdtemp()
os.environ["LAYA_CAPTURE_DIR"] = CAP


def captured():
    rows = []
    for f in sorted(os.listdir(CAP)):
        rows += [json.loads(l) for l in open(os.path.join(CAP, f), encoding="utf-8")]
    return rows


OBS = {"priority_seat": 0, "active_seat": 1, "phase": "MAIN1", "stack": [{"controller_seat": 1, "card": "Doomsday", "immediate_win_threat": True}],
       "players": [{"seat": 0, "life": 20, "mana_available": 5, "total_power": 3, "cards_in_hand": 2, "hand": ["Counterspell", "Island"],
                    "battlefield": ["Llanowar Elves"]},
                   {"seat": 1, "life": 40, "total_power": 9, "cards_in_hand": 4, "hand": ["SECRET OPPONENT CARD"], "battlefield": ["Wurm"]}]}
CANDS = [{"id": "d1:0", "kind": "counter", "card_name": "Counterspell", "mana_value": 2, "tags": ["counter", "stops_immediate_win"]},
         {"id": "d1:1", "kind": "land", "mana_value": 0},
         {"id": "d1:pass", "kind": "pass", "mana_value": 0}]
RANK = {"selected_id": "d1:0", "ranked": [{"id": "d1:0", "score": 9.1}, {"id": "d1:pass", "score": 0.2}]}

# --- text and hidden information
t = L.observation_text(OBS, RANK)
ck("the opponent's hand names are never in the text", "SECRET" not in t, t)
ck("your own hand is", "Counterspell" in t and "Island" in t)
ck("public facts, the stack and the ranking are", all(x in t for x in ("Wurm", "Doomsday", "immediate win threat", "d1:0".rsplit(":", 1)[-1])), t)
ck("opponent hand COUNT is", "4 cards in hand" in t)
ck("a candidate is described by what it is, not its opaque id", "d1" not in L.describe_candidate(CANDS[0]) and "Counterspell" in L.describe_candidate(CANDS[0]))
ck("pass is described", "pass" in L.describe_candidate(CANDS[2]))

# --- modes
for k in ("DECISION_MODE", "DECISION_MODEL", "DECISION_API_KEY"):
    os.environ.pop(k, None)
ck("no mode: nothing asked", L.advise_concrete(OBS, CANDS, RANK) is None and not SEEN)
os.environ["DECISION_MODE"] = "shadow"
B.update(pick="d1:1", conf=0.9)
adv = L.advise_concrete(OBS, CANDS, RANK)
path, body = SEEN[-1]
ck("one request, the action question plus the two extras, criteria keyed by the offered ids",
   path == "/v1/systemone" and set(body["questions"]) == {"action", "threat", "must_answer"}
   and list(body["questions"]["action"]["criteria"]) == ["d1:0", "d1:1", "d1:pass"], body["questions"].keys())
ck("the answer, its extras, and the text it read", adv and adv["choice"] == "d1:1" and abs(adv["confidence"] - 0.9) < 1e-9
   and adv["extras"]["must_answer"] == 0.25 and adv["extras"]["threat"]["choice"] in L.THREAT_LEVELS and adv["state_text"] == t, adv)

# --- shadow never changes the selection
res = L.attach_concrete(dict(RANK), OBS, CANDS, adv)
ck("shadow: selection untouched, Laya's view attached", res["selected_id"] == "d1:0" and res["laya"]["choice"] == "d1:1"
   and res["laya"]["agrees_with_selected"] is False and "laya_override" not in res, res)
rows = captured()
ck("and a laya-shadow-v1 record was written with both labels", rows and rows[-1]["schema"] == "laya-shadow-v1"
   and rows[-1]["labels"] == {"csie": "d1:0", "selected": "d1:0"} and "SECRET" not in json.dumps(rows[-1]), rows[-1:] )

# --- assist
os.environ["DECISION_MODE"] = "assist"
res = L.attach_concrete(dict(RANK), OBS, CANDS, adv)
ck("assist does NOT override a pick Forge tagged stops_immediate_win", res["selected_id"] == "d1:0" and "laya_override" not in res, res)
free = [dict(CANDS[0], tags=["counter"]), CANDS[1], CANDS[2]]
res = L.attach_concrete(dict(RANK), OBS, free, adv)
ck("assist overrides an unprotected pick when sure, keeping what CSIE chose", res["selected_id"] == "d1:1" and res["csie_selected_id"] == "d1:0"
   and res["laya_override"] is True, res)
B["conf"] = 0.4
low = L.advise_concrete(OBS, free, RANK)
res = L.attach_concrete(dict(RANK), OBS, free, low)
ck("assist ignores a pick below DECISION_MIN_CONF", res["selected_id"] == "d1:0" and "laya_override" not in res, res)
B["conf"] = 0.9
B["bogus"] = True
ck("an answer outside the offered ids is discarded", L.advise_concrete(OBS, free, RANK) is None)
B["bogus"] = False
same = dict(RANK)
B["pick"] = "d1:0"
res = L.attach_concrete(dict(same), OBS, free, L.advise_concrete(OBS, free, RANK))
ck("agreement is reported", res["laya"]["agrees_with_selected"] is True and res["selected_id"] == "d1:0")
ck("no advice: the result is returned as it was", L.attach_concrete(dict(RANK), OBS, CANDS, None) == RANK)
ck("duplicate candidate ids are refused", L.advise_concrete(OBS, [CANDS[0], CANDS[0]], RANK) is None)

# --- slow server: Forge must not wait
os.environ["DECISION_TIMEOUT_FORGE"] = "0.2"
B["delay"] = 0.8
t0 = time.time()
ck("a slow server gives None within the Forge timeout", L.advise_concrete(OBS, CANDS, RANK) is None and time.time() - t0 < 0.7)
B["delay"] = 0.0
L._fail.update(n=0, until=0.0)

# --- capture switches
n = len(captured())
os.environ["LAYA_CAPTURE"] = "0"
L.attach_concrete(dict(RANK), OBS, CANDS, adv)
ck("LAYA_CAPTURE=0 writes nothing", len(captured()) == n)
os.environ.pop("LAYA_CAPTURE")

# --- Farmhouse path through the existing client
os.environ["DECISION_MODE"] = "shadow"
B["pick"] = "cast_creature"
snap = {"turn": 4, "my_life": 31, "opp_life": 20, "my_mana_total": 4, "my_hand": [{"name": "Forest", "type": "land"}],
        "strategic_advice": {"ranking": {"selected_id": "hold", "ranked": [{"id": "hold", "score": 1.0, "explanation": "x"}]}}}
ans = D.ask(snap, actions=["play_land", "cast_creature", "hold"])
sent = SEEN[-1][1]["questions"]
ck("ask() sends the extras in the same request and returns them with the text and options",
   set(sent) == {"action", "threat", "must_answer"} and ans["extras"]["must_answer"] == 0.25 and ans["state_text"] and
   set(ans["options"]) == {"play_land", "cast_creature", "hold"} and ans["csie_selected"] == "hold", ans)
n = len(captured())
D.record(ans, "hold")
last = captured()[-1]
ck("record() writes the shadow record with the LLM and CSIE labels", len(captured()) == n + 1 and last["source"] == "farmhouse"
   and last["labels"] == {"llm": "hold", "csie": "hold"} and last["laya"]["choice"] == "cast_creature", last)

# --- fine-tuning rows
row = L.training_row(last, "llm")
ck("a training row is /v1/systemone-shaped with the options and the teacher's answer",
   row and row["answer"] == {"action": "hold"} and row["questions"]["action"]["criteria"] == last["options"]
   and row["meta"]["teacher"] == "llm" and row["state"]["body"] == last["state_text"], row)
ck("a label that is not an offered option yields no row", L.training_row(dict(last, labels={"llm": "nonsense"}), "llm") is None
   and L.training_row(last, "selected") is None)

# --- roles: batched and cached
L._role_cache.clear()
B["pick"] = "removal"
n_req = len(SEEN)
cards = [{"name": "Swords to Plowshares", "type_line": "Instant", "oracle": "Exile target creature."},
         {"name": "Rampant Growth", "type_line": "Sorcery", "oracle": "Search for a basic land."}]
roles = L.classify_roles(cards)
req = SEEN[-1]
ck("two unseen cards = ONE batch request with both states", len(SEEN) == n_req + 1 and req[0] == "/v1/systemone/batch" and len(req[1]["states"]) == 2)
ck("the roles carry provenance", roles["Swords to Plowshares"]["role"] == "removal" and roles["Swords to Plowshares"]["source"].startswith("laya:"), roles)
L.classify_roles(cards)
ck("a second call is served from the cache", len(SEEN) == n_req + 1)
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
