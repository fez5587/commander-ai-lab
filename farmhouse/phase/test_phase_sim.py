"""Run:  python -I -X utf8 test_phase_sim.py   (offline: a stub engine prints output in the format phase's ai-commander prints)"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase_sim as P  # noqa: E402

FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:260]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


def deck(name, cmd, n=3):
    return {"name": name, "commander": [cmd], "main": [{"name": "Sol Ring", "count": 1}, {"name": "Forest", "count": n}]}


GOOD = {"decks": [deck("A", "Cmd A"), deck("B", "Cmd B"), deck("C", "Cmd C"), deck("D", "Cmd D")], "games": 3, "seed": 10}

# --- validation
def bad(mut, why):
    req = json.loads(json.dumps(GOOD))
    mut(req)
    try:
        P.validate(req)
        ck("rejects " + why, False)
    except P.BadRequest as exc:
        ck("rejects " + why, True, exc)

ck("a good request validates", P.validate(GOOD)["games"] == 3)
bad(lambda r: r["decks"].pop(), "three decks")
bad(lambda r: r["decks"][0].pop("commander"), "a deck with no commander")
bad(lambda r: r["decks"][1].update(main=[]), "an empty main list")
bad(lambda r: r["decks"][2]["main"].append({"name": "X", "count": 0}), "a zero count")
bad(lambda r: r.update(games=P.MAX_GAMES + 1), "too many games")
bad(lambda r: r.update(difficulty="Impossible"), "an unknown difficulty")
bad(lambda r: r.update(seat_difficulty={"7": "Easy"}), "a seat outside 0-3")
bad(lambda r: r.update(action_cap=5), "a tiny action cap")
v = P.validate(dict(GOOD, decks=[dict(deck("A", "Cmd A"), commander="Cmd A", main=None, cards=["Forest", "Forest", "Sol Ring"])] + GOOD["decks"][1:]))
ck("a plain card list becomes counted rows and a commander string becomes a list",
   v["feed"][0]["commander"] == ["Cmd A"] and {"name": "Forest", "count": 2} in v["feed"][0]["main"], v["feed"][0])

# --- parsing the engine's output
OUT = """=== 4-player Commander AI test ===
Feed: feed.json
Batch mode: 5 games from games.txt

  A  |  commander: Cmd A  |  main: 99 cards
  B  |  commander: Cmd B  |  main: 99 cards
  SKIP C: commander 'Cmd C' not in card db
  D  |  commander: Cmd D  |  main: 99 cards
Resolved deck sizes (post name-resolution, 0-indexed by seat):
  P0  main= 99  commander=1
  P1  main= 97  commander=1
  P2  main= 99  commander=1
  P3  main= 99  commander=1

ExecutionMode: measurement
--- GAME seed=10 difficulty=Medium ---
Game started. 4 players.
ELIMINATED: P1  turn=9  actions=  400  elapsed=3.1s
ELIMINATED: P3  turn=12  actions=  600  elapsed=5.0s

=== RESULT ===
Elapsed: 8.0s
Turns played: 14

Game ended cleanly. Winner: P2
--- GAME seed=11 difficulty=Medium ---
Game started. 4 players.
=== RESULT ===
Turns played: 40
Game ended cleanly. Winner: draw
--- GAME seed=12 difficulty=Medium ---
=== RESULT ===
Turns played: 7
STALL: waiting_for=ChooseTarget turn=7 active=P0 priority=P0 pending_cast=none
Game did NOT reach GameOver. waiting_for = ChooseTarget
--- GAME seed=13 difficulty=Medium ---
ABORT: hit action cap=50000
=== RESULT ===
Turns played: 31
Game did NOT reach GameOver. waiting_for = Priority
--- GAME seed=14 difficulty=Medium ---
GAME 14 PANICKED: index out of bounds
"""
parsed = P.parse(OUT)
st = [g["status"] for g in parsed["games"]]
ck("five games found with their outcomes in order", st == ["won", "draw", "stalled", "action_cap", "panicked"], st)
g0 = parsed["games"][0]
ck("winner seat, turns and elimination order are read", g0["winner_seat"] == 2 and g0["turns"] == 14
   and [e["seat"] for e in g0["eliminated"]] == [1, 3], g0)
ck("a draw has no winner seat", parsed["games"][1]["winner_seat"] is None)
ck("pre-resolution and resolved deck sizes and the skipped deck are read", len(parsed["pre"]) == 3 and parsed["resolved"][1]["main"] == 97
   and parsed["skipped"] == [{"deck": "C", "commander": "Cmd C"}], (parsed["pre"], parsed["resolved"], parsed["skipped"]))

spec = P.validate(dict(GOOD, games=5))
res = P.summarize(spec, parsed, [10, 11, 12, 13, 14])
ck("only finished games are counted", res["games_finished"] == 2 and res["games_not_finished"] == 3 and res["draws"] == 1, res)
ck("wins by seat and per-deck win rate", res["wins_by_seat"] == [0, 0, 1, 0] and res["decks"][2]["win_rate"] == 0.5 and res["decks"][0]["win_rate"] == 0.0)
lo, hi = res["decks"][2]["win_rate_95"]
ck("a 95% interval wraps the rate and stays in 0..1", 0 <= lo < 0.5 < hi <= 1)
w = " | ".join(res["warnings"])
ck("warnings: a skipped deck, unrecognised cards, and unfinished games (never counted)",
   "was skipped" in w and "2 of 99" in w and "not counted as wins or draws" in w and "stalled" in w, w)
ck("each game carries its seed", [g["seed"] for g in res["games"]] == [10, 11, 12, 13, 14])
ck("interval of nothing is zeros, of a sweep is high", P.wilson(0, 0) == [0.0, 0.0] and P.wilson(10, 10)[0] > 0.7)

# --- a whole job through the real worker path with a stub engine
STUB = os.path.join(tempfile.mkdtemp(), "stub_engine.py")
with open(STUB, "w", encoding="utf-8") as f:
    f.write('''import sys, os
root = sys.argv[1]
seeds = [l.split(",")[0] for l in open(os.path.join(root, "games.txt")) if l.strip()]
feed = __import__("json").load(open(os.path.join(root, "feed.json")))
assert os.path.exists(os.path.join(root, "card-data.json")) and len(feed["decks"]) == 4
for d in feed["decks"]:
    print("  %s  |  commander: %s  |  main: %d cards" % (d["name"], d["commander"][0], sum(e["count"] for e in d["main"])))
print("Resolved deck sizes (post name-resolution, 0-indexed by seat):")
for i, d in enumerate(feed["decks"]):
    print("  P%d  main=%3d  commander=1" % (i, sum(e["count"] for e in d["main"])))
for k, s in enumerate(seeds):
    print("--- GAME seed=%s ---" % s)
    print("Turns played: %d" % (10 + k))
    print("Game ended cleanly. Winner: P%d" % (k % 4))
''')
CD = os.path.join(os.path.dirname(STUB), "card-data.json")
open(CD, "w").write("{}")
P.CARD_DATA = CD
ORIG_COMMAND = P.command
P.command = lambda spec, root: [sys.executable, STUB, root]
job_id = P.submit(dict(GOOD, games=4))
threading.Thread(target=P.worker, daemon=True).start()
for _ in range(100):
    j = P._jobs[job_id]
    if j["status"] in ("done", "error"):
        break
    time.sleep(0.1)
pub = P.public(P._jobs[job_id])
ck("a queued job runs to done with four finished games, one win each", pub["status"] == "done" and pub["result"]["games_finished"] == 4
   and pub["result"]["wins_by_seat"] == [1, 1, 1, 1] and pub["games_done"] == 4, pub)
ck("no warnings when every card resolved", pub["result"]["warnings"] == [], pub["result"]["warnings"])
ck("the temp folder is cleaned up", not [d for d in os.listdir(tempfile.gettempdir()) if d.startswith("phase-sim-")])

# --- the HTTP surface
srv = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_port
def call(path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)
code, h = call("/health")
ck("/health answers (ok is false only because the real binary is not here)", code == 200 and "engine" in h and h["max_games"] == P.MAX_GAMES, h)
code, r = call("/run", dict(GOOD, games=1))
ck("POST /run is accepted with a job id", code == 202 and len(r["job"]) == 12, r)
for _ in range(100):
    code, j = call("/job/" + r["job"])
    if j["status"] in ("done", "error"):
        break
    time.sleep(0.1)
ck("GET /job/<id> returns the result", code == 200 and j["status"] == "done" and j["result"]["games_finished"] == 1, j)
ck("a bad request is a 400 with a reason", call("/run", {"decks": []})[0] == 400)
ck("an unknown job is a 404", call("/job/deadbeef")[0] == 404)
ck("an unknown path is a 404", call("/nope")[0] == 404)
# --- model seats: validation, environment, relays
from http.server import BaseHTTPRequestHandler, HTTPServer

UP = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        UP.append((self.path, body, self.headers.get("authorization")))
        out = json.dumps({"echo": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


up = HTTPServer(("127.0.0.1", 0), Upstream)
threading.Thread(target=up.serve_forever, daemon=True).start()
UPBASE = "http://127.0.0.1:%d" % up.server_port
os.environ["PHASE_SIM_ALLOW_MISSING_LLM_BIN"] = "1"
LLMREQ = dict(GOOD, llm_seats=[1, 3], llm={"kind": "chat", "model": "twin-turbo", "max_calls": 120})

P.CHAT_URL, P.DECISION_URL = "", ""
try:
    P.validate(LLMREQ)
    ck("model seats are refused when no model endpoint is configured", False)
except P.BadRequest as exc:
    ck("model seats are refused when no model endpoint is configured", "no chat model endpoint" in str(exc), exc)
P.CHAT_URL, P.DECISION_URL, P.DECISION_KEY = UPBASE + "/v1", UPBASE + "/v1/systemone", "sekret"
v = P.validate(LLMREQ)
ck("a chat model seat request validates", v["llm"] == {"seats": [1, 3], "kind": "chat", "model": "twin-turbo", "max_calls": 120}, v["llm"])
ck("a request with no model seats has no llm settings", P.validate(GOOD)["llm"] is None)
for mut, why in ((lambda r: r.update(llm_seats=[4]), "a seat outside 0-3"),
                 (lambda r: r["llm"].pop("model"), "a chat model with no name"),
                 (lambda r: r["llm"].update(max_calls=P.MAX_LLM_CALLS + 1), "too many model calls"),
                 (lambda r: r["llm"].update(kind="telepathy"), "an unknown kind")):
    req = json.loads(json.dumps(LLMREQ))
    mut(req)
    try:
        P.validate(req)
        ck("rejects " + why, False)
    except P.BadRequest:
        ck("rejects " + why, True)
dv = P.validate(dict(GOOD, llm_seats=[0], llm={"kind": "decision"}))
ck("a decision-model seat needs no model name", dv["llm"]["kind"] == "decision" and dv["llm"]["model"] == "decision-model")

env = P.engine_env(v)
ck("chat seats: the engine is pointed at this service's /llm relay as an OpenAI-compatible endpoint with no key",
   env["PHASE_LLM_SEATS"] == "1,3" and env["PHASE_LLM_PROVIDER"] == "OpenAiCompatible" and env["PHASE_LLM_BASE_URL"].endswith("/llm/v1")
   and env["PHASE_LLM_MODEL"] == "twin-turbo" and env["PHASE_LLM_API_KEY"] == "" and env["PHASE_LLM_MAX_CALLS"] == "120", env)
denv = P.engine_env(dv)
ck("decision seats: provider Jev, base is this service (the engine adds /jev/systemone)", denv["PHASE_LLM_PROVIDER"] == "Jev"
   and denv["PHASE_LLM_BASE_URL"] == "http://127.0.0.1:%d" % P.PORT)
ck("no model seats: no special environment, the plain engine", P.engine_env(P.validate(GOOD)) is None
   and ORIG_COMMAND(P.validate(GOOD), "/r")[0] == P.BIN and ORIG_COMMAND(v, "/r")[0] == P.BIN_LLM)

code, out = P.relay_chat(json.dumps({"model": "twin-turbo", "messages": [{"role": "user", "content": "x"}], "max_tokens": 4000, "stream": True}).encode(), "127.0.0.1")
path, body, auth = UP[-1]
ck("the chat relay forwards to /chat/completions with thinking off, the answer capped and streaming off",
   code == 200 and path == "/v1/chat/completions" and body["reasoning_effort"] == "none" and body["max_tokens"] == P.CHAT_MAX_TOKENS
   and body["stream"] is False and body["model"] == "twin-turbo", (code, path, body))
ck("a request without max_tokens gets the cap", (P.relay_chat(b'{"model": "m", "messages": []}', "127.0.0.1"), UP[-1][1]["max_tokens"] == P.CHAT_MAX_TOKENS)[1])
ck("the relays refuse anyone but this machine", P.relay_chat(b"{}", "10.1.2.3")[0] == 403 and P.relay_decision(b"{}", "10.1.2.3")[0] == 403)
ck("a malformed chat body is a 400, not an exception", P.relay_chat(b"not json", "127.0.0.1")[0] == 400)
envelope = {"apiKey": "ignored-by-us", "request": {"state": {"body": "t"}, "questions": {"pick": {"type": "choice"}}}}
code, out = P.relay_decision(json.dumps(envelope).encode(), "127.0.0.1")
path, body, auth = UP[-1]
ck("the decision relay unwraps phase's envelope and sends only the request, with OUR key", code == 200 and path == "/v1/systemone"
   and body == envelope["request"] and auth == "Bearer sekret", (code, path, body, auth))
ck("a decision body with no request is a 400", P.relay_decision(b'{"apiKey": "k"}', "127.0.0.1")[0] == 400)
P.CHAT_URL = "http://127.0.0.1:1/v1"
ck("an unreachable model endpoint is a 502", P.relay_chat(b'{"model": "m", "messages": []}', "127.0.0.1")[0] == 502)
P.CHAT_URL = UPBASE + "/v1"

summ = P.llm_summary({"seats": [1], "kind": "chat", "model": "m", "max_calls": 9},
                     ["LLM-SEAT seat=1 asked=25 used=20 total_calls=25", "noise", "LLM-SEAT seat=1 asked=50 used=41 total_calls=50"])
ck("the model seats' counters are read from the engine's output (last value wins)", summ["per_seat"] == {1: {"asked": 50, "used": 41}}, summ)

code, r = call("/llm/v1/chat/completions", {"model": "m", "messages": []})
ck("over HTTP from this machine the chat relay answers", code == 200 and r["echo"] is True, (code, r))
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
