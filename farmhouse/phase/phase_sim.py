"""phase-sim: run rules-accurate four-player Commander games with phase-rs's headless `ai-commander` and report what happened.

Standard library only. One job runs at a time (a game is CPU-heavy); more wait in a queue. The service never touches the network.

  GET  /health            {"ok": true, "engine": ..., "queued": n, "running": id|null}
  POST /run               {"decks": [4 x {"name", "commander": ["Card"], "main": [{"name","count"}, ...]}],
                           "games": 1..MAX_GAMES, "seed": int, "difficulty": "Medium", "seat_difficulty": {"2": "Hard"}, "action_cap": int}
                          -> {"job": id}
  GET  /job/<id>          status queued|running|done|error, games finished, and when done: per-game rows, wins by seat and by deck,
                          win rate with a 95% interval, the cards the engine could not resolve, and the tail of the engine's output

Seat i is the i-th deck in the request (P0 = first). The engine treats every seat as AI; difficulty is per seat if given.
A game that stalls, hits the action cap, or panics is reported as such and is NEVER counted as a win or a draw.

Model seats (optional, needs the image's ai-commander-llm binary): "llm_seats": [1, 3] and "llm": {"kind": "chat" | "decision", "model": "...",
"max_calls": 300}. Those seats ask a model to pick among the ENGINE'S OWN legal options (phase-llm builds the prompt and checks the answer;
anything wrong or slow falls back to phase's AI). kind "chat" goes to PHASE_LLM_CHAT_URL (an OpenAI-style endpoint, e.g. Olla) through this
service's /llm relay, which switches the model's thinking off and caps its answer; kind "decision" goes to PHASE_LLM_DECISION_URL (a System One
server: Laya or Kev) through /jev/systemone. Both relays accept connections from this machine only.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN = os.environ.get("PHASE_AI_COMMANDER", "/opt/phase/ai-commander")
CARD_DATA = os.environ.get("PHASE_CARD_DATA", "/opt/phase/data/card-data.json")
ENGINE = os.environ.get("PHASE_ENGINE_VERSION", "phase-rs (version unknown)")
MAX_GAMES = int(os.environ.get("PHASE_SIM_MAX_GAMES", "50"))
JOB_TIMEOUT = int(os.environ.get("PHASE_SIM_TIMEOUT", "3600"))
PORT = int(os.environ.get("PORT", "8090"))
DIFFICULTIES = ("VeryEasy", "Easy", "Medium", "Hard", "VeryHard")
BIN_LLM = os.environ.get("PHASE_AI_COMMANDER_LLM", "/opt/phase/ai-commander-llm")
CHAT_URL = os.environ.get("PHASE_LLM_CHAT_URL", "").rstrip("/")          # e.g. http://olla...:40114/olla/openai/v1
DECISION_URL = os.environ.get("PHASE_LLM_DECISION_URL", "")             # e.g. http://laya...:8080/v1/systemone
DECISION_KEY = os.environ.get("PHASE_LLM_DECISION_KEY", "")
CHAT_REASONING = os.environ.get("PHASE_LLM_REASONING", "none")
CHAT_MAX_TOKENS = int(os.environ.get("PHASE_LLM_MAX_TOKENS_CAP", "256"))
MAX_LLM_CALLS = int(os.environ.get("PHASE_LLM_MAX_CALLS", "600"))
LOCAL_ONLY = ("127.0.0.1", "::1")
MAX_DECK_ENTRIES = 400

_jobs = {}
_queue = deque()
_cv = threading.Condition()
_running = {"id": None}


# ---------- request -> engine inputs ------------------------------------------------------------------------------------------

class BadRequest(ValueError):
    pass


def validate(req):
    decks = req.get("decks")
    if not isinstance(decks, list) or len(decks) != 4:
        raise BadRequest("decks must be a list of exactly 4 decks (one per seat)")
    feed = []
    for i, d in enumerate(decks):
        if not isinstance(d, dict) or not str(d.get("name") or "").strip():
            raise BadRequest("deck %d needs a name" % i)
        cmd = d.get("commander")
        if isinstance(cmd, str):
            cmd = [cmd]
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c.strip() for c in cmd):
            raise BadRequest("deck %d needs commander: [\"Card Name\"]" % i)
        main = d.get("main")
        if main is None and isinstance(d.get("cards"), list):
            counts = {}
            for n in d["cards"]:
                counts[str(n)] = counts.get(str(n), 0) + 1
            main = [{"name": k, "count": v} for k, v in counts.items()]
        if not isinstance(main, list) or not main or len(main) > MAX_DECK_ENTRIES:
            raise BadRequest("deck %d needs a non-empty main list (at most %d entries)" % (i, MAX_DECK_ENTRIES))
        rows = []
        for e in main:
            try:
                name, count = str(e["name"]), int(e["count"])
            except (KeyError, TypeError, ValueError):
                raise BadRequest("deck %d has a bad main entry: %r" % (i, e))
            if not name.strip() or not 1 <= count <= 99:
                raise BadRequest("deck %d has a bad main entry: %r" % (i, e))
            rows.append({"name": name, "count": count})
        feed.append({"name": str(d["name"]), "commander": [c.strip() for c in cmd], "main": rows})
    try:
        games = int(req.get("games", 1))
        seed = int(req.get("seed", 1))
    except (TypeError, ValueError):
        raise BadRequest("games and seed must be integers")
    if not 1 <= games <= MAX_GAMES:
        raise BadRequest("games must be 1 to %d" % MAX_GAMES)
    difficulty = req.get("difficulty", "Medium")
    if difficulty not in DIFFICULTIES:
        raise BadRequest("difficulty must be one of %s" % ", ".join(DIFFICULTIES))
    seat_diff = {}
    for k, v in (req.get("seat_difficulty") or {}).items():
        if str(k) not in ("0", "1", "2", "3") or v not in DIFFICULTIES:
            raise BadRequest("seat_difficulty keys are seats 0-3, values one of %s" % ", ".join(DIFFICULTIES))
        seat_diff[int(k)] = v
    cap = req.get("action_cap")
    if cap is not None:
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            raise BadRequest("action_cap must be an integer")
        if not 100 <= cap <= 200000:
            raise BadRequest("action_cap must be 100 to 200000")
    llm = None
    seats = req.get("llm_seats") or []
    if seats:
        if not isinstance(seats, list) or not all(isinstance(x, int) and 0 <= x <= 3 for x in seats):
            raise BadRequest("llm_seats must be a list of seats 0-3")
        cfg = req.get("llm") or {}
        kind = cfg.get("kind", "chat")
        if kind not in ("chat", "decision"):
            raise BadRequest("llm.kind must be chat or decision")
        if (kind == "chat" and not CHAT_URL) or (kind == "decision" and not DECISION_URL):
            raise BadRequest("this service has no %s model endpoint configured" % kind)
        if not os.path.isfile(BIN_LLM) and not os.environ.get("PHASE_SIM_ALLOW_MISSING_LLM_BIN"):
            raise BadRequest("this image has no model-seat engine (ai-commander-llm)")
        model = str(cfg.get("model") or "").strip()
        if kind == "chat" and not model:
            raise BadRequest("llm.model is required for a chat model")
        try:
            max_calls = int(cfg.get("max_calls", 300))
        except (TypeError, ValueError):
            raise BadRequest("llm.max_calls must be an integer")
        if not 1 <= max_calls <= MAX_LLM_CALLS:
            raise BadRequest("llm.max_calls must be 1 to %d" % MAX_LLM_CALLS)
        llm = {"seats": sorted(set(seats)), "kind": kind, "model": model or "decision-model", "max_calls": max_calls}
    return {"feed": feed, "games": games, "seed": seed, "difficulty": difficulty, "seat_difficulty": seat_diff, "action_cap": cap, "llm": llm}


def engine_env(spec):
    """The environment for the engine process: model-seat settings, or None (inherit) for the plain runner."""
    llm = spec.get("llm")
    if not llm:
        return None
    env = dict(os.environ)
    local = "http://127.0.0.1:%d" % PORT
    env.update(PHASE_LLM_SEATS=",".join(str(x) for x in llm["seats"]), PHASE_LLM_MODEL=llm["model"], PHASE_LLM_API_KEY="",
               PHASE_LLM_MAX_CALLS=str(llm["max_calls"]), PHASE_LLM_DIFFICULTY=spec["difficulty"])
    if llm["kind"] == "chat":
        env.update(PHASE_LLM_PROVIDER="OpenAiCompatible", PHASE_LLM_BASE_URL=local + "/llm/v1")
    else:
        env.update(PHASE_LLM_PROVIDER="Jev", PHASE_LLM_BASE_URL=local)
    return env


def command(spec, root):
    cmd = [BIN_LLM if spec.get("llm") else BIN, root, "--feed", "feed.json", "--games-file", os.path.join(root, "games.txt")]
    for seat, level in sorted(spec["seat_difficulty"].items()):
        cmd += ["--difficulty-p%d" % seat, level]
    if spec["action_cap"]:
        cmd += ["--action-cap", str(spec["action_cap"])]
    return cmd


# ---------- engine output -> results ------------------------------------------------------------------------------------------

GAME_HEAD = re.compile(r"^--- GAME\b(.*)$")
WINNER = re.compile(r"Game ended cleanly\. Winner: (draw|P(\d))")
TURNS = re.compile(r"^Turns played: (\d+)")
ELIM = re.compile(r"^ELIMINATED: P(\d)\s+turn=(\d+)")
RESOLVED = re.compile(r"^\s+P(\d)\s+main=\s*(\d+)\s+commander=(\d+)")
LLM_STAT = re.compile(r"^LLM-SEAT seat=(\d) asked=(\d+) used=(\d+)")
SKIP = re.compile(r"^\s+SKIP (.+?): commander '(.+)' not in card db")
PRE = re.compile(r"^\s+(.+?)\s+\|\s+commander: (.+?)\s+\|\s+main: (\d+) cards")


def parse(output):
    """Per-game rows and the deck-resolution facts from the engine's stdout. Games that did not finish are kept, labelled, never counted."""
    lines = output.splitlines()
    pre, resolved, skipped, games, cur = [], {}, [], [], None

    def finish():
        if cur is not None:
            games.append(cur)

    def new_game(label=""):
        return {"label": label, "status": "unfinished", "winner_seat": None, "turns": None, "eliminated": []}

    result_line = re.compile(r"Game ended cleanly|^Turns played|^ELIMINATED:|^STALL:|ABORT: hit action cap|PANICKED")
    for ln in lines:
        if cur is None and not games:                       # the preamble: decks listed, then their resolved sizes
            m = PRE.match(ln)
            if m:
                pre.append({"deck": m.group(1), "commander": m.group(2), "listed": int(m.group(3))})
                continue
            m = SKIP.match(ln)
            if m:
                skipped.append({"deck": m.group(1), "commander": m.group(2)})
                continue
            m = RESOLVED.match(ln)
            if m and int(m.group(1)) not in resolved:
                resolved[int(m.group(1))] = {"main": int(m.group(2)), "commanders": int(m.group(3))}
                continue
        if GAME_HEAD.match(ln):
            finish()
            cur = new_game(GAME_HEAD.match(ln).group(1).strip(" -"))
            continue
        if cur is None:
            if not result_line.search(ln):
                continue                                    # banner and preamble lines are not a game
            cur = new_game()                                # a single-game run prints no "--- GAME" header
        m = WINNER.search(ln)
        if m:
            cur["status"] = "draw" if m.group(1) == "draw" else "won"
            cur["winner_seat"] = None if m.group(2) is None else int(m.group(2))
            continue
        m = TURNS.match(ln)
        if m:
            cur["turns"] = int(m.group(1))
            continue
        m = ELIM.match(ln)
        if m:
            cur["eliminated"].append({"seat": int(m.group(1)), "turn": int(m.group(2))})
            continue
        if ln.startswith("STALL:"):
            cur["status"] = "stalled"
            cur.setdefault("detail", ln)
        elif "ABORT: hit action cap" in ln:
            cur["status"] = "action_cap"
        elif re.search(r"GAME .* PANICKED", ln):
            cur["status"] = "panicked"
            cur["detail"] = ln[:300]
    finish()
    games = [g for g in games if g["status"] != "unfinished" or g["turns"] is not None or g["eliminated"] or g["label"]]
    return {"pre": pre, "resolved": resolved, "skipped": skipped, "games": games}


def llm_summary(llm, lines):
    """The model seats' own counters (the engine prints one every 25th question), last value per seat."""
    per_seat = {}
    for ln in lines:
        m = LLM_STAT.match(ln)
        if m:
            per_seat[int(m.group(1))] = {"asked": int(m.group(2)), "used": int(m.group(3))}
    return {"seats": llm["seats"], "kind": llm["kind"], "model": llm["model"], "max_calls": llm["max_calls"], "per_seat": per_seat,
            "note": "counters are printed every 25th question, so the last few may be missing"}


def wilson(wins, n, z=1.96):
    if n == 0:
        return [0.0, 0.0]
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(max(0.0, (c - h) / d), 4), round(min(1.0, (c + h) / d), 4)]


def summarize(spec, parsed, seeds):
    decks = [d["name"] for d in spec["feed"]]
    finished = [g for g in parsed["games"] if g["status"] in ("won", "draw")]
    won = [g for g in finished if g["status"] == "won"]
    by_seat = [sum(1 for g in won if g["winner_seat"] == i) for i in range(4)]
    n = len(finished)
    warnings = []
    for s in parsed["skipped"]:
        warnings.append("deck %r was skipped: commander %r is not in the engine's card database" % (s["deck"], s["commander"]))
    for i, pre in enumerate(parsed["pre"][:4]):
        got = (parsed["resolved"].get(i) or {}).get("main")
        if got is not None and got < pre["listed"]:
            warnings.append("seat %d (%s): %d of %d cards were not recognised by the engine and were left out" % (
                i, pre["deck"], pre["listed"] - got, pre["listed"]))
    bad = [g for g in parsed["games"] if g["status"] not in ("won", "draw")]
    if bad:
        warnings.append("%d game(s) did not finish (%s): not counted as wins or draws" % (
            len(bad), ", ".join(sorted({g["status"] for g in bad}))))
    for i, g in enumerate(parsed["games"]):
        if i < len(seeds):
            g["seed"] = seeds[i]
    return {"games_requested": spec["games"], "games_finished": n, "games_not_finished": len(bad), "draws": n - len(won),
            "wins_by_seat": by_seat,
            "decks": [{"seat": i, "name": decks[i], "wins": by_seat[i], "win_rate": round(by_seat[i] / n, 4) if n else None,
                       "win_rate_95": wilson(by_seat[i], n)} for i in range(4)],
            "avg_turns": round(sum(g["turns"] or 0 for g in finished) / n, 1) if n else None,
            "warnings": warnings, "games": parsed["games"]}


# ---------- jobs ----------------------------------------------------------------------------------------------------------------

def run_job(job):
    spec = job["spec"]
    root = tempfile.mkdtemp(prefix="phase-sim-")
    out_lines = []
    try:
        os.symlink(CARD_DATA, os.path.join(root, "card-data.json"))
        with open(os.path.join(root, "feed.json"), "w", encoding="utf-8") as f:
            json.dump({"decks": spec["feed"]}, f)
        seeds = [spec["seed"] + i for i in range(spec["games"])]
        with open(os.path.join(root, "games.txt"), "w", encoding="utf-8") as f:
            f.write("".join("%d,%s\n" % (s, spec["difficulty"]) for s in seeds))
        job["status"], job["started"] = "running", time.time()
        proc = subprocess.Popen(command(spec, root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", cwd=root,
                                env=engine_env(spec))
        timer = threading.Timer(JOB_TIMEOUT, proc.kill)
        timer.start()
        try:
            for ln in proc.stdout:
                out_lines.append(ln.rstrip("\n"))
                if GAME_HEAD.match(ln):
                    job["games_started"] += 1
                if WINNER.search(ln) or "GAME" in ln and "PANICKED" in ln or ln.startswith("STALL:") or "ABORT: hit action cap" in ln:
                    job["games_done"] += 1
            code = proc.wait()
        finally:
            timer.cancel()
        text = "\n".join(out_lines)
        parsed = parse(text)
        job["result"] = summarize(spec, parsed, seeds)
        if spec.get("llm"):
            job["result"]["llm"] = llm_summary(spec["llm"], out_lines)
            job["result"]["warnings"].append(
                "model seats %s: the model is asked about each non-forced decision, up to %d calls for the whole run; an answer the engine "
                "cannot use, a timeout, or any decision after the cap is played by phase's own AI instead" % (spec["llm"]["seats"], spec["llm"]["max_calls"]))
        job["engine_exit_code"] = code
        job["output_tail"] = out_lines[-40:]
        job["status"] = "done" if parsed["games"] else "error"
        if not parsed["games"]:
            job["error"] = "the engine produced no game results (exit code %s); see output_tail" % code
    except Exception as exc:  # the job fails, the service does not
        job["status"], job["error"] = "error", "%s: %s" % (type(exc).__name__, exc)
        job["output_tail"] = out_lines[-40:]
    finally:
        job["finished"] = time.time()
        try:
            for name in os.listdir(root):
                os.unlink(os.path.join(root, name))
            os.rmdir(root)
        except OSError:
            pass


def worker():
    while True:
        with _cv:
            while not _queue:
                _cv.wait()
            job = _queue.popleft()
            _running["id"] = job["id"]
        run_job(job)
        with _cv:
            _running["id"] = None


def submit(req):
    spec = validate(req)
    job = {"id": uuid.uuid4().hex[:12], "status": "queued", "created": time.time(), "spec": spec, "games_started": 0, "games_done": 0}
    with _cv:
        _jobs[job["id"]] = job
        _queue.append(job)
        for old in sorted(_jobs.values(), key=lambda j: j["created"])[:-50]:   # remember the last 50
            if old["status"] in ("done", "error"):
                _jobs.pop(old["id"], None)
        _cv.notify()
    return job["id"]


def public(job):
    out = {k: job[k] for k in ("id", "status", "games_started", "games_done") if k in job}
    out["games_requested"] = job["spec"]["games"]
    for k in ("result", "error", "output_tail", "engine_exit_code"):
        if k in job:
            out[k] = job[k]
    if "started" in job:
        out["seconds"] = round((job.get("finished") or time.time()) - job["started"], 1)
    return out


def forward(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=dict({"content-type": "application/json"}, **headers))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": "upstream error"}
    except (urllib.error.URLError, OSError, ValueError):
        return 502, {"error": "the model endpoint is not reachable"}


def relay_chat(body, remote):
    """POST /llm/v1/chat/completions -> the OpenAI-style endpoint, with the model's thinking off and its answer capped."""
    if remote not in LOCAL_ONLY or not CHAT_URL:
        return 403, {"error": "not available"}
    try:
        req = json.loads(body)
        req["max_tokens"] = min(int(req.get("max_tokens") or CHAT_MAX_TOKENS), CHAT_MAX_TOKENS)
        req["stream"] = False
        if CHAT_REASONING:
            req["reasoning_effort"] = CHAT_REASONING
        data = json.dumps(req).encode()
    except (ValueError, TypeError):
        return 400, {"error": "bad chat request"}
    return forward(CHAT_URL + "/chat/completions", data, {})


def relay_decision(body, remote):
    """POST /jev/systemone: phase's Jev envelope {"apiKey", "request": {...}} -> a System One server (Laya / Kev); the key comes from the environment."""
    if remote not in LOCAL_ONLY or not DECISION_URL:
        return 403, {"error": "not available"}
    try:
        data = json.dumps(json.loads(body)["request"]).encode()
    except (ValueError, KeyError, TypeError):
        return 400, {"error": "bad decision request"}
    return forward(DECISION_URL, data, {"authorization": "Bearer " + DECISION_KEY} if DECISION_KEY else {})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "phase-sim/1"

    def log_message(self, fmt, *args):
        if os.environ.get("PHASE_SIM_LOG") == "1":
            sys.stderr.write("%s\n" % (fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            with _cv:
                return self._send(200, {"ok": os.path.isfile(BIN) and os.path.isfile(CARD_DATA), "engine": ENGINE,
                                        "queued": len(_queue), "running": _running["id"], "max_games": MAX_GAMES})
        m = re.match(r"^/job/([0-9a-f]+)$", self.path)
        if m:
            with _cv:
                job = _jobs.get(m.group(1))
                return self._send(200, public(job)) if job else self._send(404, {"error": "no such job"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path in ("/llm/v1/chat/completions", "/jev/systemone"):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4_000_000:
                return self._send(413, {"error": "request too large"})
            fn = relay_chat if self.path.startswith("/llm") else relay_decision
            code, obj = fn(self.rfile.read(n), self.client_address[0])
            return self._send(code, obj)
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                return self._send(413, {"error": "request too large"})
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise BadRequest("the body must be a JSON object")
            self._send(202, {"job": submit(req)})
        except (BadRequest, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})


def main():
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
