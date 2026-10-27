"""Smoke test, run INSIDE the image (CI and by hand): four real decks from phase's own sample feed play real games through the service.

    python /opt/phase/phase_smoke.py [games] [difficulty]            plain engine
    python /opt/phase/phase_smoke.py --llm                           model seats against a stub model inside this process

Exits non-zero unless at least one game finished with a winner or a draw. Prints what the engine reported, including any cards it did
not recognise, so a bad data/engine pairing shows up here and not in a result someone trusts.

--llm starts a stub OpenAI-style model on 127.0.0.1:9099 (the service must have been started with PHASE_LLM_CHAT_URL=http://127.0.0.1:9099/v1),
puts seats 1 and 3 on it, and passes when the engine's own counters show the model was asked AND its answers were used by the engine: that
proves the Rust hook, the relay and phase's answer check end to end without any outside service. The stub always answers option 0, so the
game itself may not finish (it can hit the action cap); that is not what is being tested.
"""
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = "http://127.0.0.1:8090"
LLM = "--llm" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
games = int(args[0]) if args else (1 if LLM else 2)
difficulty = args[1] if len(args) > 1 else "VeryEasy"

ASKED = []


class StubModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        ASKED.append(body)
        out = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "{\"choice\": 0, \"reason\": \"stub\"}"}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if LLM:
    threading.Thread(target=HTTPServer(("127.0.0.1", 9099), StubModel).serve_forever, daemon=True).start()


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


for _ in range(60):
    try:
        h = call("/health")
        break
    except Exception:
        time.sleep(1)
else:
    sys.exit("the service did not come up")
print("health:", h)
assert h["ok"], "binary or card data missing"

feed = json.load(open("/opt/phase/sample-feed.json", encoding="utf-8"))["decks"]
decks = []
for d in feed:
    cmd = d.get("commander") or [d["name"]]
    decks.append({"name": d["name"], "commander": cmd, "main": d["main"]})
    if len(decks) == 4:
        break
request = {"decks": decks, "games": games, "seed": 7, "difficulty": difficulty, "action_cap": 60000}
if LLM:
    request.update(action_cap=6000, llm_seats=[1, 3], llm={"kind": "chat", "model": "stub-model", "max_calls": 60})
job = call("/run", request)["job"]
t0 = time.time()
while True:
    j = call("/job/" + job)
    if j["status"] in ("done", "error"):
        break
    if time.time() - t0 > 1500:
        sys.exit("timed out; last: %s" % json.dumps(j)[:300])
    time.sleep(3)
print("job: %s in %.0f s" % (j["status"], time.time() - t0))
res = j.get("result") or {}
print(json.dumps({k: res.get(k) for k in ("games_requested", "games_finished", "games_not_finished", "draws", "wins_by_seat", "avg_turns", "warnings")}, indent=1))
for g in res.get("games", []):
    print("  game seed=%s %s winner=%s turns=%s eliminated=%s" % (g.get("seed"), g["status"], g.get("winner_seat"), g.get("turns"), g.get("eliminated")))
if LLM:
    llm = res.get("llm") or {}
    print("model seats:", json.dumps(llm), "| requests the stub model received:", len(ASKED))
    print("\n".join((j.get("output_tail") or [])[-15:]))
    seats = llm.get("per_seat") or {}
    asked = sum(v["asked"] for v in seats.values())
    used = sum(v["used"] for v in seats.values())
    reasoned = [a for a in ASKED if a.get("reasoning_effort") == "none"]
    if not ASKED or not asked or not used:
        sys.exit("the model seats were not exercised: stub got %d requests, engine counters asked=%d used=%d" % (len(ASKED), asked, used))
    if len(reasoned) != len(ASKED):
        sys.exit("the relay did not switch the model's thinking off on every request")
    print("PHASE LLM SMOKE OK (asked %d, engine used %d, stub saw %d requests, all with thinking off)" % (asked, used, len(ASKED)))
    sys.exit(0)
if j["status"] != "done" or res.get("games_finished", 0) < 1:
    print("\n".join(j.get("output_tail") or []))
    sys.exit("no game finished")
print("PHASE SMOKE OK")
