"""Turn the laya-shadow-v1 capture log into fine-tuning data for Laya (or Kev), plus the numbers a fine-tuned model must beat.

    python scripts/farmhouse_laya_dataset.py [--in data/decisions] [--out data/laya-train] [--teacher llm|csie|selected]
                                             [--source all|farmhouse|forge] [--holdout 0.2] [--seed 193]

Reads every `laya-shadow-*.jsonl` (written when DECISION_MODE is shadow or assist: one line per call, with the text Laya read, the
offered options, Laya's own zero-shot answer and the other deciders' picks), keeps the lines whose chosen teacher label is one of the
offered options, drops exact duplicates, and splits BY GAME so no game appears on both sides (lines with no game id are split by
the hash of their text, which is the same guarantee for identical states). Writes:

    train.jsonl, valid.jsonl   one /v1/systemone-shaped example per line: state, questions (with the offered options), answer, meta
    manifest.json              counts, teacher, class balance, and the baselines on the VALIDATION side:
                               - always the commonest action
                               - CSIE's own pick vs the teacher (how much a fine-tuned model must add over the free heuristic)
                               - Laya zero-shot vs the teacher (what was captured before any training)

Which teacher: `llm` (the slow model; the point is to make a fast student of it), `csie` (the deterministic heuristic; cheap and
unlimited but a student can at best equal it), `selected` (what Forge executed). The labels are the teacher's choices, not proof they
were right: a student trained on the LLM inherits its mistakes. Laya's own fine-tuning notebook/script (github.com/NandhaKishorM/laya,
notebooks/) trains on this kind of typed-decision data; adapt the field names there if its loader differs.
"""
import argparse
import collections
import glob
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("farmhouse_laya", os.path.join(HERE, "..", "src", "commander_ai_lab", "sim", "farmhouse_laya.py"))
LAYA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LAYA)


def read_records(directory):
    for path in sorted(glob.glob(os.path.join(directory, "laya-shadow-*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("schema") == LAYA.CAPTURE_SCHEMA:
                yield rec


def bucket(rec, seed):
    key = rec.get("game_id") or rec.get("state_text") or ""
    return int(hashlib.sha256(("%s|%s" % (seed, key)).encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def build(records, teacher="llm", source="all", holdout=0.2, seed=193):
    train, valid, seen = [], [], set()
    for rec in records:
        if source != "all" and rec.get("source") != source:
            continue
        row = LAYA.training_row(rec, teacher)
        if not row:
            continue
        key = (rec["state_text"], row["answer"]["action"], json.dumps(row["questions"]["action"]["criteria"], sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        (valid if bucket(rec, seed) < holdout else train).append((row, rec))
    return train, valid


def baselines(pairs, teacher):
    """Agreement with the teacher on `pairs` of (row, record): commonest-action, CSIE, and Laya zero-shot."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    targets = [r["answer"]["action"] for r, _ in pairs]
    common, k = collections.Counter(t.rsplit(":", 1)[-1] for t in targets).most_common(1)[0]
    out = {"n": n, "commonest_action": common, "commonest_agreement": round(k / n, 4)}
    csie = [(rec.get("labels") or {}).get("csie") for _, rec in pairs]
    if teacher != "csie":
        have = [(c, t) for c, t in zip(csie, targets) if c]
        if have:
            out["csie_vs_teacher"] = {"n": len(have), "agreement": round(sum(c == t for c, t in have) / len(have), 4)}
    laya = [((rec.get("laya") or {}).get("choice"), t) for (_, rec), t in zip(pairs, targets)]
    have = [(c, t) for c, t in laya if c]
    if have:
        out["laya_zero_shot_vs_teacher"] = {"n": len(have), "agreement": round(sum(c == t for c, t in have) / len(have), 4)}
    return out


def write(out_dir, train, valid, teacher, source, holdout, seed):
    os.makedirs(out_dir, exist_ok=True)
    for name, rows in (("train.jsonl", train), ("valid.jsonl", valid)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as f:
            for row, _ in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    balance = collections.Counter(r["answer"]["action"].rsplit(":", 1)[-1] for r, _ in train)
    manifest = {"schema": "laya-train-manifest-v1", "teacher": teacher, "source": source, "holdout": holdout, "seed": seed,
                "train": len(train), "valid": len(valid), "train_class_balance": dict(balance.most_common()),
                "validation_baselines": baselines(valid, teacher),
                "note": "Labels are the teacher's choices, not proof they were right. Split by game id (or text hash)."}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--in", dest="inp", default=os.path.join("data", "decisions"))
    p.add_argument("--out", default=os.path.join("data", "laya-train"))
    p.add_argument("--teacher", choices=("llm", "csie", "selected"), default="llm")
    p.add_argument("--source", choices=("all", "farmhouse", "forge"), default="all")
    p.add_argument("--holdout", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=193)
    a = p.parse_args(argv)
    train, valid = build(read_records(a.inp), a.teacher, a.source, a.holdout, a.seed)
    if not train and not valid:
        print("no usable records in %s for teacher %r (turn on DECISION_MODE=shadow and play some games first)" % (a.inp, a.teacher))
        return 1
    m = write(a.out, train, valid, a.teacher, a.source, a.holdout, a.seed)
    print(json.dumps(m, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
