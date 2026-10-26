"""Run:  python -I -X utf8 test_farmhouse_fit.py   (no network, no upstream code: a fake card database stands in for Scryfall's)"""
import json
import os
import sqlite3
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:220]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE cards (name TEXT, color_identity TEXT)")
CARDS = {"Sol Ring": [], "Cultivate": ["G"], "Swords to Plowshares": ["W"], "Counterspell": ["U"], "Klothys, God of Destiny": ["R", "G"],
         "Lightning Bolt": ["R"], "Forest": ["G"], "Mountain": ["R"], "Island": ["U"], "Hexproof Charm": ["G"],
         "Fire // Ice": ["R", "U"]}
for n, ci in CARDS.items():
    db.execute("INSERT INTO cards VALUES (?, ?)", (n, json.dumps(ci)))
db.commit()
services = types.ModuleType("services")
bulk = types.ModuleType("services.scryfall_bulk")
bulk.get_bulk_db = lambda: db
sys.modules["services"], sys.modules["services.scryfall_bulk"] = services, bulk
from _load import load  # noqa: E402
F = load('farmhouse_fit', 'src/commander_ai_lab/deck_builder/pipeline/farmhouse_fit.py')

WUBG = {"W", "U", "B", "G"}
w = []
r = F.fit_deck({"synergy": ["Hexproof Charm", "Klothys, God of Destiny", "Lightning Bolt"], "ramp": ["Sol Ring", "Cultivate"],
                "lands": ["Forest", "Mountain", "Island"]}, {"synergy": 25, "ramp": 10, "lands": 36}, WUBG, w)
flat = [n for v in r.values() for n in v]
ck("a red card is dropped from a four-colour deck", "Klothys, God of Destiny" not in flat and "Lightning Bolt" not in flat and "Mountain" not in flat, flat)
ck("colourless and in-identity cards stay", {"Sol Ring", "Cultivate", "Hexproof Charm", "Forest", "Island"} <= set(flat), flat)
ck("the drop is reported, with names", any("outside the commander's colour identity" in x and "Klothys" in x for x in w), w)

# Split card "Fire // Ice" (R/U) is found by its front name and judged by both halves.
r = F.fit_deck({"synergy": ["Fire", "Counterspell"]}, {"synergy": 25}, WUBG, [])
ck("a double-faced / split card is matched by its front name", r["synergy"] == ["Counterspell"], r)

# The 359-card pool: 359 unique names across categories; targets sum to 99.
pool = {"lands": ["Land %d" % i for i in range(120)], "ramp": ["Ramp %d" % i for i in range(60)], "synergy": ["Syn %d" % i for i in range(120)],
        "removal": ["Rem %d" % i for i in range(30)], "card_draw": ["Draw %d" % i for i in range(29)]}
targets = {"lands": 36, "ramp": 10, "card_draw": 10, "removal": 8, "protection": 5, "synergy": 25, "wincon": 3, "uncategorized": 2}
w = []
r = F.fit_deck(pool, targets, WUBG, w)
n = sum(len(v) for v in r.values())
ck("359 candidates become at most 99", n <= 99, n)
ck("each category respects its target", all(len(r[c]) <= targets[c] for c in r), {c: len(r[c]) for c in r})
ck("the best-ranked (first) cards are the ones kept", r["synergy"][0] == "Syn 0" and r["lands"][:2] == ["Land 0", "Land 1"], r["synergy"][:2])

# Over 99 even with every category at target: tail of the largest category goes.
big = {"a": ["A%d" % i for i in range(60)], "b": ["B%d" % i for i in range(60)]}
w = []
r = F.fit_deck(big, {"a": 60, "b": 60}, WUBG, w, total=99)
ck("an over-99 pool is trimmed to exactly 99", sum(len(v) for v in r.values()) == 99 and any("Trimmed" in x for x in w), (sum(len(v) for v in r.values()), w))

# Duplicates across categories are dropped (first claim wins); unknown names are kept.
r = F.fit_deck({"ramp": ["Sol Ring", "Mystery Card"], "synergy": ["Sol Ring", "Cultivate"]}, {"ramp": 10, "synergy": 25}, WUBG, [])
ck("a card in two categories appears once (first wins)", r["ramp"] == ["Sol Ring", "Mystery Card"] and r["synergy"] == ["Cultivate"], r)
ck("a name the database does not know is kept, not dropped", "Mystery Card" in r["ramp"])

# No database at all: nothing is dropped for colour, still capped.
del sys.modules["services.scryfall_bulk"]
bulk2 = types.ModuleType("services.scryfall_bulk")
def boom():
    raise RuntimeError("no db")
bulk2.get_bulk_db = boom
sys.modules["services.scryfall_bulk"] = bulk2
r = F.fit_deck({"synergy": ["Lightning Bolt", "Cultivate"]}, {"synergy": 25}, WUBG, [])
ck("without a card database it keeps everything rather than guessing", r["synergy"] == ["Lightning Bolt", "Cultivate"], r)
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
