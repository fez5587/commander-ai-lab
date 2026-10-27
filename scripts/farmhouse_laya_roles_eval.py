"""How well does a decision model classify a card's role from its Oracle text, zero-shot?

    DECISION_URL=http://laya:8080 python scripts/farmhouse_laya_roles_eval.py [bulk_db] [per_role=25] [model]

Builds a sample from the local Scryfall bulk database using plain text patterns that give a card exactly ONE role (unambiguous cards
only; many real cards have several roles, so this is a floor on how hard the task is, not a full test), classifies them in batches
through farmhouse_laya.classify_roles, and prints accuracy, a confusion table and the time per card. Laya's output here would be
evidence with provenance for ITI / Tagger-style role data, never a rules fact; this tells you whether that evidence is worth having.
"""
import collections
import importlib.util
import os
import random
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("farmhouse_laya", os.path.join(HERE, "..", "src", "commander_ai_lab", "sim", "farmhouse_laya.py"))
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

PATTERNS = {
    "removal": re.compile(r"\b(destroy|exile) target (creature|permanent|nonland permanent|artifact|enchantment|planeswalker)", re.I),
    "board_wipe": re.compile(r"\b(destroy|exile) all (creatures|nonland permanents|permanents)|each creature (gets|is dealt)", re.I),
    "ramp": re.compile(r"search your library for (a|up to \w+) basic land|\badd \{[WUBRGC]\}", re.I),
    "card_draw": re.compile(r"\bdraw (a|two|three|\w+) cards?\b", re.I),
    "counterspell": re.compile(r"\bcounter target (spell|noncreature spell|creature spell|activated or triggered ability)", re.I),
    "tutor": re.compile(r"search your library for (a|an|up to \w+) (?!basic land)(card|creature|artifact|instant|sorcery|enchantment)", re.I),
}


def sample(db, per_role, seed=193):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT name, type_line, oracle_text FROM cards WHERE oracle_text != '' AND type_line NOT LIKE '%Land%'").fetchall()
    random.Random(seed).shuffle(rows)
    out, taken, seen = [], collections.Counter(), set()
    for name, type_line, oracle in rows:
        if name in seen or len(oracle) > 700:
            continue
        hits = [r for r, p in PATTERNS.items() if p.search(oracle)]
        if len(hits) != 1 or taken[hits[0]] >= per_role:
            continue
        seen.add(name)
        taken[hits[0]] += 1
        out.append({"name": name, "type_line": type_line, "oracle": oracle, "truth": hits[0]})
        if all(taken[r] >= per_role for r in PATTERNS):
            break
    return out


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "scryfall_bulk.db")
    per_role = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    if len(sys.argv) > 3:
        os.environ["DECISION_MODEL"] = sys.argv[3]
    if not os.environ.get("DECISION_URL"):
        print("set DECISION_URL")
        return 1
    cards = sample(db, per_role)
    t0 = time.time()
    got = L.classify_roles(cards)
    dt = time.time() - t0
    answered = [(c["truth"], got[c["name"]]["role"], got[c["name"]]["confidence"]) for c in cards if c["name"] in got]
    print("%d cards sampled (%s); %d classified in %.1f s (%.2f s per card)" % (
        len(cards), dict(collections.Counter(c["truth"] for c in cards)), len(answered), dt, dt / max(len(cards), 1)))
    if not answered:
        return 1
    ok = sum(t == p for t, p, _ in answered)
    print("accuracy %.1f%%   (always-one-role baseline %.1f%%; there are %d roles to choose among)" % (
        100.0 * ok / len(answered), 100.0 / len(PATTERNS), len(L.ROLE_HELP)))
    for lo in (0.5, 0.7, 0.85):
        sel = [(t, p) for t, p, c in answered if c >= lo]
        if sel:
            print("  when >= %.2f sure: %d cards (%.0f%%), accuracy %.1f%%" % (lo, len(sel), 100.0 * len(sel) / len(answered),
                                                                           100.0 * sum(t == p for t, p in sel) / len(sel)))
    conf = collections.defaultdict(collections.Counter)
    for t, p, _ in answered:
        conf[t][p] += 1
    print("truth -> what it said:")
    for t in PATTERNS:
        print("  %-13s %s" % (t, dict(conf[t].most_common(4))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
