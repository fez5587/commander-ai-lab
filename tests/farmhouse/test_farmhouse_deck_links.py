"""Run:  python -I -X utf8 test_farmhouse_deck_links.py   (offline: the Moxfield reply is a recorded shape, no network)"""
import io
import os
import sys
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load  # noqa: E402
L = load('farmhouse_deck_links', 'services/farmhouse_deck_links.py')

FAILS = 0


def ck(name, cond, detail=""):
    global FAILS
    print("  %s  %s%s" % ("ok  " if cond else "FAIL", name, ("  -- " + str(detail)[:220]) if detail and not cond else ""))
    if not cond:
        FAILS += 1


# --- which text is a link we handle
ck("a plain moxfield link", L.link_kind("https://moxfield.com/decks/oEWXWHM5eEGMmopExLWRCA") == ("moxfield", "oEWXWHM5eEGMmopExLWRCA", "https://moxfield.com/decks/oEWXWHM5eEGMmopExLWRCA"))
ck("www, trailing slash, query and a /primer path", L.link_kind("  https://www.moxfield.com/decks/aB-c_9/primer?x=1  ")[:2] == ("moxfield", "aB-c_9"))
ck("an archidekt link", L.link_kind("https://archidekt.com/decks/12345/some-name")[:2] == ("archidekt", "12345"))
ck("a decklist is not a link", L.link_kind("1 Sol Ring\n1 Cultivate") is None)
ck("a link inside text is not treated as a link", L.link_kind("see https://moxfield.com/decks/abc for the deck") is None)
ck("an unknown site is flagged, not guessed", L.link_kind("https://example.com/mydeck")[0] == "unsupported")
ck("empty", L.link_kind("") is None and L.link_kind(None) is None)

# --- the Moxfield reply shape (recorded from a public deck, trimmed)
DECK = {"name": "Atraxa Counters", "publicUrl": "https://moxfield.com/decks/abc123", "colorIdentity": ["W", "U", "B", "G"],
        "commanders": {"Atraxa, Praetors' Voice": {"quantity": 1, "card": {"name": "Atraxa, Praetors' Voice"}}},
        "mainboard": {"Sol Ring": {"quantity": 1, "card": {"name": "Sol Ring"}}, "Forest": {"quantity": 7, "card": {"name": "Forest"}},
                      "Fire // Ice": {"quantity": 1, "card": {"name": "Fire // Ice"}}},
        "sideboard": {"Swords to Plowshares": {"quantity": 1, "card": {"name": "Swords to Plowshares"}}},
        "maybeboard": {"Lightning Bolt": {"quantity": 1, "card": {"name": "Lightning Bolt"}}}, "companions": {}}
p = L.moxfield_profile(DECK, "https://moxfield.com/decks/abc123")
ck("profile has the commander and 99-style main", p["commander"] == "Atraxa, Praetors' Voice" and p["mainboard"]["Forest"] == 7 and p["totalCards"] == 10, p)
ck("sideboard and maybeboard are left out", "Swords to Plowshares" not in p["mainboard"] and "Lightning Bolt" not in p["mainboard"])
ck("profile shape matches the Archidekt one's keys", {"name", "commander", "source", "sourceUrl", "commanders", "mainboard", "colorIdentity", "totalCards"} <= set(p))
ck("source says Moxfield", p["source"] == "Moxfield" and p["colorIdentity"] == ["W", "U", "B", "G"])
t = L.profile_to_text(p)
ck("text uses the Deck Builder's own sections", t.startswith("[Commander]\n1 Atraxa, Praetors' Voice\n\n[Main]\n") and "7 Forest" in t and "1 Fire // Ice" in t, t)
ck("quantities and names are on one 'qty name' line each", all(l.split(" ", 1)[0].isdigit() for l in t.splitlines() if l and not l.startswith("[")))

# --- expand(): lone links are fetched; everything else passes through
calls = []
L._get_json = lambda url: (calls.append(url), DECK)[1]
txt, name = L.expand("https://moxfield.com/decks/abc123")
ck("a link becomes the decklist text and a deck name", "[Main]" in txt and name == "Atraxa Counters" and calls == [L.MOXFIELD_API + "abc123"], (name, calls))
ck("pasted text passes through untouched", L.expand("1 Sol Ring\n1 Forest\n") == ("1 Sol Ring\n1 Forest\n", None))
try:
    L.expand("https://example.com/deck")
    ck("an unknown lone link raises a person-readable error", False)
except ValueError as exc:
    ck("an unknown lone link raises a person-readable error", "Moxfield and Archidekt" in str(exc), exc)

# --- failures
def http(code, retry_after=None):
    def f(url):
        raise HTTPError(url, code, "x", {"Retry-After": retry_after} if retry_after else {}, io.BytesIO(b""))
    return f

for code in (401, 403, 404):
    L._get_json = http(code)
    try:
        L.fetch_moxfield("abc")
        ck("HTTP %d says private/wrong and what to do" % code, False)
    except ValueError as exc:
        ck("HTTP %d says private/wrong and what to do" % code, "Copy as text" in str(exc), exc)

n = {"c": 0}
def flaky(url):
    n["c"] += 1
    if n["c"] < 3:
        raise HTTPError(url, 429, "slow down", {"Retry-After": "0"}, io.BytesIO(b""))
    return DECK
L._get_json = flaky
L.time.sleep = lambda s: None
ck("a 429 is retried and then succeeds", L.fetch_moxfield("abc")["name"] == "Atraxa Counters" and n["c"] == 3, n)
L._get_json = http(429, "0")
try:
    L.fetch_moxfield("abc")
    ck("a stuck 429 gives up with a clear message", False)
except ValueError as exc:
    ck("a stuck 429 gives up with a clear message", "rate-limiting" in str(exc), exc)

empty = dict(DECK, commanders={}, mainboard={})
L._get_json = lambda url: empty
try:
    L.fetch_profile("https://moxfield.com/decks/abc")
    ck("an empty deck is refused", False)
except ValueError as exc:
    ck("an empty deck is refused", "no cards" in str(exc), exc)
# --- local card lookup (an in-memory copy of the bulk table's relevant columns)
import sqlite3
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE cards (scryfall_id TEXT, name TEXT, edhrec_rank INTEGER)")
db.executemany("INSERT INTO cards VALUES (?,?,?)", [("a1", "Sol Ring", 1), ("a2", "Sol Ring", 5), ("b1", "Fire // Ice", 9),
                                                    ("c1", "Fire", 2), ("d1", "Forest", None)])
ck("exact name, any case", L.local_card("sol ring", db) == {"id": "a1", "name": "Sol Ring"})
ck("front face of a split card", L.local_card("Ice", db) is None and L.local_card("Fire // Ice", db)["id"] == "b1")
ck("an exact name beats a split-card prefix match", L.local_card("Fire", db)["id"] == "c1")
ck("unknown, empty and wildcard characters find nothing", L.local_card("Nope", db) is None and L.local_card("", db) is None and L.local_card("%", db) is None)
ck("a broken database yields None, not an exception", L.local_card("Sol Ring", object()) is None)
print("\nFAILURES: %d" % FAILS)
sys.exit(1 if FAILS else 0)
