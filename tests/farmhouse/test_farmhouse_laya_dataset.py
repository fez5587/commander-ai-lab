"""Run:  python -I -X utf8 tests/farmhouse/test_farmhouse_laya_dataset.py   (offline, temp directories only)"""
import importlib.util
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT  # noqa: E402

spec = importlib.util.spec_from_file_location("ds", os.path.join(ROOT, "scripts", "farmhouse_laya_dataset.py"))
DS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DS)
FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:240]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


OPTS = {"play_land": "land", "cast_creature": "creature", "hold": "pass"}


def rec(i, game, llm, csie, laya, source="farmhouse", text=None):
    return {"schema": "laya-shadow-v1", "ts": 1.0 + i, "source": source, "game_id": game, "state_text": text or "state %d" % i,
            "options": OPTS, "laya": {"choice": laya}, "labels": {"llm": llm, "csie": csie}}


recs = []
for g in range(40):                                   # 40 games x 5 decisions
    for k in range(5):
        i = g * 5 + k
        llm = ["play_land", "cast_creature", "hold", "cast_creature", "hold"][k]
        recs.append(rec(i, "g%d" % g, llm, "hold" if k % 2 else llm, "cast_creature"))
recs.append(rec(900, "g0", "hold", "hold", "hold", text="state 0"))          # same text+label as an existing one? different label -> kept
recs.append(rec(901, "g1", "hold", "hold", "hold", text="state 5"))
recs.append(dict(rec(902, "g2", "NOT_AN_OPTION", "hold", "hold")))           # label outside the options -> dropped
recs.append(dict(rec(903, "g3", "hold", "hold", "hold"), schema="something-else"))  # wrong schema -> ignored by the reader
recs.append(rec(904, "g4", "hold", "hold", "hold", source="forge"))

d = tempfile.mkdtemp()
with open(os.path.join(d, "laya-shadow-20261026.jsonl"), "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r) + "\n")
    f.write("not json at all\n")
back = list(DS.read_records(d))
ck("the reader keeps only laya-shadow-v1 lines and skips junk", len(back) == len(recs) - 1, len(back))

train, valid = DS.build(back, "llm", "all", 0.25, 193)
ck("rows outside the options are dropped, everything else becomes a row", len(train) + len(valid) == len(recs) - 2 - 0, (len(train), len(valid)))
tg = {r["meta"]["game_id"] for r, _ in train}
vg = {r["meta"]["game_id"] for r, _ in valid}
ck("no game is on both sides of the split", not (tg & vg) and tg and vg, (len(tg), len(vg)))
t2, v2 = DS.build(back, "llm", "all", 0.25, 193)
ck("the split is reproducible", [r["meta"]["ts"] for r, _ in valid] == [r["meta"]["ts"] for r, _ in v2])
ck("a different seed moves games", {r["meta"]["game_id"] for r, _ in DS.build(back, "llm", "all", 0.25, 7)[1]} != vg)
only_forge_t, only_forge_v = DS.build(back, "llm", "forge", 0.25, 193)
ck("--source filters", len(only_forge_t) + len(only_forge_v) == 1)
dup = DS.build(back + [rec(1, "g0", "play_land", "play_land", "hold", text="state 0")], "llm", "all", 0.25, 193)
ck("an exact duplicate (same text, label and options) is dropped", len(dup[0]) + len(dup[1]) == len(train) + len(valid))

row = train[0][0]
ck("a row is /v1/systemone-shaped with options, the answer and provenance", set(row) == {"state", "questions", "answer", "meta"}
   and row["questions"]["action"]["criteria"] == OPTS and row["meta"]["teacher"] == "llm" and row["meta"]["schema"] == "laya-train-v1")

b = DS.baselines(valid, "llm")
ck("baselines report the commonest action, CSIE and Laya zero-shot against the teacher",
   b["n"] == len(valid) and 0 < b["commonest_agreement"] <= 1 and b["csie_vs_teacher"]["n"] == len(valid)
   and "laya_zero_shot_vs_teacher" in b, b)
ck("CSIE as the teacher has no csie-vs-teacher line", "csie_vs_teacher" not in DS.baselines(DS.build(back, "csie", "all", .25, 193)[1], "csie"))

out = os.path.join(d, "out")
m = DS.write(out, train, valid, "llm", "all", 0.25, 193)
files = sorted(os.listdir(out))
lines = sum(1 for _ in open(os.path.join(out, "train.jsonl"), encoding="utf-8"))
ck("train, valid and the manifest are written", files == ["manifest.json", "train.jsonl", "valid.jsonl"] and lines == len(train)
   and m["train"] == len(train) and m["valid"] == len(valid) and "validation_baselines" in m, files)
ck("main() on an empty directory says so and fails", DS.main(["--in", tempfile.mkdtemp(), "--out", out]) == 1)
ck("main() on the log succeeds", DS.main(["--in", d, "--out", os.path.join(d, "o2"), "--teacher", "csie"]) == 0)
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
