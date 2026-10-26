"""How often does a decision model (Laya / Kev) pick the same action as the LLM did, on game states the app has already recorded?

    DECISION_URL=http://laya:8080 python scripts/farmhouse_decision_eval.py [decisions_dir] [model ...]

Reads the brain's own decision logs (logs/decisions/*.jsonl: the snapshot and the LLM's answer), asks the decision model the same
question for each, and prints agreement overall, at several confidence levels, per action, the "always the most common action"
baseline, and the time per answer. The LLM is the teacher here, not the truth: this measures how well a fast model can stand in
for the slow one, not how well either plays.
"""
import collections
import glob
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("farmhouse_decide", os.path.join(HERE, "..", "src", "commander_ai_lab", "sim", "farmhouse_decide.py"))
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)


def load(dirpath):
    rows = []
    for f in sorted(glob.glob(os.path.join(dirpath, "*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("decision") or {}).get("source") == "llm" and r.get("game_state") and r["decision"].get("action") in D.ACTION_HELP:
                rows.append(r)
    return rows


def main():
    dirpath = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "logs", "decisions")
    models = sys.argv[2:] or [os.environ.get("DECISION_MODEL", "")]
    rows = load(dirpath)
    if not rows:
        print("no recorded LLM decisions in", dirpath)
        return 1
    truth = [r["decision"]["action"] for r in rows]
    common, n_common = collections.Counter(truth).most_common(1)[0]
    print("%d recorded decisions; LLM action mix: %s" % (len(rows), dict(collections.Counter(truth))))
    print("baseline 'always %s': %.1f%%\n" % (common, 100.0 * n_common / len(rows)))
    for model in models:
        os.environ["DECISION_MODEL"] = model
        D.STATS.update({"asked": 0, "agreed": 0, "answered_conf": 0, "errors": 0, "ms_total": 0.0, "by_action": {}})
        got = []
        for r in rows:
            a = D.ask(r["game_state"])
            got.append(a)
            D.record(a, r["decision"]["action"])
        ok = [(g, t) for g, t in zip(got, truth) if g]
        print("== model %r: answered %d of %d, %.0f ms each" % (model or "auto", len(ok), len(rows), D.stats()["avg_ms"] or 0))
        if not ok:
            continue
        print("   agrees with the LLM on %.1f%%  (baseline %.1f%%)" % (100.0 * sum(g["choice"] == t for g, t in ok) / len(ok), 100.0 * n_common / len(rows)))
        for lo in (0.5, 0.7, 0.85):
            sel = [(g, t) for g, t in ok if g["confidence"] >= lo]
            if sel:
                print("   when it is >= %.2f sure (%d answers, %.0f%%): agrees %.1f%%" % (
                    lo, len(sel), 100.0 * len(sel) / len(ok), 100.0 * sum(g["choice"] == t for g, t in sel) / len(sel)))
        by = collections.defaultdict(lambda: [0, 0])
        for g, t in ok:
            by[t][0] += 1
            by[t][1] += g["choice"] == t
        print("   per LLM action:", {k: "%d/%d" % (v[1], v[0]) for k, v in sorted(by.items())})
        print("   it picked:", dict(collections.Counter(g["choice"] for g, t in ok)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
