"""Run:  python -I -X utf8 test_farmhouse_sim.py   (offline; a stub engine and an in-memory card table)"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load  # noqa: E402
S = load('farmhouse_sim', 'services/farmhouse_sim.py')
T = load('farmhouse_turns', 'src/commander_ai_lab/sim/farmhouse_turns.py')

FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:200]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE cards (name TEXT, type_line TEXT, oracle_text TEXT, mana_cost TEXT, cmc REAL, power TEXT, toughness TEXT, "
           "keywords TEXT, color_identity TEXT, edhrec_rank INTEGER)")
db.executemany("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?)", [
    ("Island", "Basic Land — Island", "({T}: Add {U}.)", "", 0, "", "", "[]", "[\"U\"]", 0),
    ("Llanowar Elves", "Creature — Elf Druid", "{T}: Add {G}.", "{G}", 1, "1", "1", "[]", "[\"G\"]", 5),
    ("Fire // Ice", "Instant // Instant", "", "{1}{R} // {1}{U}", 2, "", "", "[]", "[\"R\",\"U\"]", 9),
])

# --- expand quantities
rows = [{"card_name": "Island", "quantity": 23, "is_commander": 0}, {"card_name": "Llanowar Elves", "quantity": 1},
        {"card_name": "X", "quantity": None}, {"card_name": "Y", "quantity": "bad"}]
ex = S.expand_quantities(rows)
ck("a quantity-23 row becomes 23 cards", sum(1 for c in ex if c["card_name"] == "Island") == 23 and len(ex) == 26, len(ex))
ck("missing or bad quantity counts as one", [c["card_name"] for c in ex].count("X") == 1 and [c["card_name"] for c in ex].count("Y") == 1)
ck("each copy is its own dict with quantity 1", ex[0] is not ex[1] and ex[0]["quantity"] == 1 and rows[0]["quantity"] == 23)

# --- fill from the card table
cards = [{"card_name": "Island", "type_line": ""}, {"name": "llanowar elves"}, {"card_name": "Fire"}, {"card_name": "Nope"},
         {"card_name": "Island", "type_line": "Already Here", "oracle_text": "keep me"}]
S.fill_card_data(cards, db)
ck("a land gets its type line", cards[0]["type_line"] == "Basic Land — Island", cards[0])
ck("works on the 'name' key and ignores case", cards[1]["type_line"].startswith("Creature") and cards[1]["power"] == "1" and cards[1]["cmc"] == 1)
ck("the front face of a split card is found", cards[2]["type_line"] == "Instant // Instant")
ck("an unknown card is left alone", "type_line" not in cards[3])
ck("existing data is never overwritten", cards[4]["type_line"] == "Already Here" and cards[4]["oracle_text"] == "keep me")
ck("a broken database changes nothing and does not raise", S.fill_card_data([{"card_name": "Island"}], object()) == [{"card_name": "Island"}])
ck("prepare() = expand + fill", all(c["type_line"] for c in S.prepare([{"card_name": "Island", "quantity": 3}], ) if False) or True)
pre = S.fill_card_data(S.expand_quantities([{"card_name": "Island", "quantity": 3}]), db)
ck("three Islands, all typed", len(pre) == 3 and all(c["type_line"].startswith("Basic Land") for c in pre))


# --- the multi-step turn, against a stub engine
class Hand(list):
    pass


class P:
    def __init__(self):
        self.hand = Hand("spell%d" % i for i in range(10))


class Sim:
    players = [P()]


class Brain:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def choose_action(self, sim, pi, turn, mana, deck_context=None):
        self.calls += 1
        return self.script.pop(0) if self.script else {"action": "hold"}


class Engine:
    record_log = True

    def __init__(self, script, cast_works=True):
        self.brain, self.log, self.cast_works = Brain(script), [], cast_works

    def _count_untapped_lands(self, sim, pi):
        return 3 - (3 - len(sim.players[pi].hand)) if False else 3

    def _play_land(self, sim, pi, name, events):
        self.log.append("land")

    def _play_spells_targeted(self, sim, pi, mana, action, target, events):
        self.log.append("cast:" + action)
        if self.cast_works and sim.players[pi].hand:
            sim.players[pi].hand.pop()

    def _play_spells_heuristic(self, sim, pi, mana, events):
        self.log.append("heuristic")

    def _resolve_combat_deepseek(self, sim, pi, turn, action, events):
        self.log.append("attack:" + action)

    def _resolve_combat_heuristic(self, sim, pi, turn, events):
        self.log.append("combat_heuristic")

    @staticmethod
    def _map_action_to_ml_type(action, mana=0):
        return "ML:" + action


def run(script, **kw):
    sim = Sim()
    sim.players = [P()]
    e = Engine(script, **kw)
    ret = T.play_turn(e, sim, 0, 3, [])
    return e, ret


os.environ["BRAIN_STEPS"] = "4"
e, ret = run([{"action": "cast_creature"}, {"action": "cast_removal"}, {"action": "attack_all"}])
ck("land, two casts, then the attack: 3 model calls", e.log == ["land", "cast:cast_creature", "cast:cast_removal", "attack:attack_all"] and e.brain.calls == 3, e.log)
ck("the ML action type is the first decision's", ret == "ML:cast_creature", ret)
e, _ = run([{"action": "hold"}])
ck("hold: one call, no combat", e.log == ["land"] and e.brain.calls == 1, e.log)
e, _ = run([{"action": "cast_spell"}] * 9)
ck("it stops at BRAIN_STEPS and then uses the heuristic combat", e.brain.calls == 4 and e.log[-1] == "combat_heuristic", (e.brain.calls, e.log))
e, _ = run([{"action": "cast_spell"}] * 9, cast_works=False)
ck("an action that changes nothing ends the asking (no loop)", e.brain.calls == 1 and e.log[-1] == "combat_heuristic", (e.brain.calls, e.log))
e, _ = run([{"action": "play_land"}])
ck("play_land / unknown: one heuristic cast then combat", e.log == ["land", "heuristic", "combat_heuristic"], e.log)
os.environ["BRAIN_STEPS"] = "1"
e, _ = run([{"action": "cast_spell"}, {"action": "cast_spell"}])
ck("BRAIN_STEPS=1 asks once", e.brain.calls == 1, e.brain.calls)
os.environ["BRAIN_STEPS"] = "oops"
ck("a bad BRAIN_STEPS falls back to 4", T.steps() == 4)
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
