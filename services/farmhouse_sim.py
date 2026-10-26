"""Card data for the simulators (copied into the app as services/farmhouse_sim.py by the Dockerfile).

Found 2026-10-26 from a real run: every card of every saved deck reached the simulator with no type line and no rules text (the
loaders join the user's *collection* and a *card_records* table, both empty here; the Forge card cache that was meant to fill the gap
is not installed). The simulator could not tell a land from a creature, so a whole 8-game batch cast 0 spells, dealt 0 damage and lost
every game. The batch loader also did not repeat cards by quantity, so a 100-card deck loaded as 66-84 cards with ONE Island.

fill_card_data fills the gaps from the local 113k-card Scryfall bulk database; expand_quantities repeats a row `quantity` times.
"""


def _name(card):
    return (card.get("card_name") or card.get("name") or "").strip()


def _lookup(conn, name, cache):
    key = name.lower()
    if key in cache:
        return cache[key]
    like = name.replace("!", "!!").replace("%", "!%").replace("_", "!_") + " // %"
    row = conn.execute(
        "SELECT type_line, oracle_text, mana_cost, cmc, power, toughness, keywords, color_identity FROM cards "
        "WHERE LOWER(name) = LOWER(?) OR LOWER(name) LIKE LOWER(?) ESCAPE '!' "
        "ORDER BY (LOWER(name) = LOWER(?)) DESC, edhrec_rank ASC LIMIT 1", (name, like, name)).fetchone()
    cache[key] = row
    return row


def fill_card_data(cards, conn=None):
    """Fill type_line / oracle_text / mana_cost / cmc / power / toughness / keywords / color_identity where a card dict lacks them.
    Never overwrites a value that is already there. Returns the same list."""
    try:
        if conn is None:
            from services.scryfall_bulk import get_bulk_db
            conn = get_bulk_db()
        cache = {}
        for card in cards:
            n = _name(card)
            if not n or card.get("type_line"):
                continue
            row = _lookup(conn, n, cache)
            if not row:
                continue
            for field, value in zip(("type_line", "oracle_text", "mana_cost", "cmc", "power", "toughness", "keywords", "color_identity"), row):
                if value not in (None, "") and not card.get(field):
                    card[field] = value
    except Exception:
        pass
    return cards


def expand_quantities(cards):
    """One dict per physical card: a row with quantity 23 becomes 23 rows (each quantity 1)."""
    out = []
    for card in cards:
        try:
            q = int(card.get("quantity") or 1)
        except (TypeError, ValueError):
            q = 1
        for _ in range(max(q, 1)):
            copy = dict(card)
            copy["quantity"] = 1
            out.append(copy)
    return out


def prepare(cards):
    """Rows as the batch loader returns them -> one filled-in dict per physical card."""
    return fill_card_data(expand_quantities(cards))
