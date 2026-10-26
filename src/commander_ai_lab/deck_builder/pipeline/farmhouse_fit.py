"""Deterministic guards for the deck build, copied into the app by the Dockerfile (pipeline/farmhouse_fit.py).

Upstream's step 6 asks the LLM to trim the candidate pool to 99 cards by category and step 5 promises a colour-identity filter that
is not in the code. With a small local model that gave decks with an off-colour card (a red card in a four-colour deck) and, with
a larger one, 359 cards instead of 99; either made the app reject its own result ("Value error ... outside commander identity" /
"Deck must contain exactly 99 cards"). This does both jobs in plain code, using the Scryfall card database the app already keeps
(services.scryfall_bulk, 100,000+ cards): drop every card outside the commander's colour identity, drop duplicates, keep each
category to its target count (candidates arrive best-first: EDHREC, then Scryfall, then the model), and never exceed 99.
Anything the database does not know is kept, not dropped. The existing back-fill adds basic lands if the total is short.
"""
import json
import logging

logger = logging.getLogger(__name__)


def _identity_checker(allowed):
    try:
        from services.scryfall_bulk import get_bulk_db
        conn = get_bulk_db()
    except Exception as exc:  # no database: do not guess, keep everything
        logger.warning("farmhouse_fit: Scryfall card database unavailable (%s); colour identity not checked", exc)
        return lambda name: True

    def ok(name):
        try:
            row = conn.execute("SELECT color_identity FROM cards WHERE name = ? OR name LIKE ? LIMIT 1",
                               (name, name + " // %")).fetchone()
            if not row:
                return True
            return set(json.loads(row[0] or "[]")) <= allowed
        except Exception:
            return True
    return ok


def fit_deck(adjusted, target_ratios, commander_ci, warnings, total=99):
    """Return {category: [names]} that is colour-legal, duplicate-free, within each category's target, and at most `total` cards."""
    allowed = set(commander_ci or [])
    ok = _identity_checker(allowed)
    out, seen, off_colour = {}, set(), []
    for cat, names in adjusted.items():
        keep = []
        for name in names:
            if name in seen:
                continue
            if not ok(name):
                off_colour.append(name)
                continue
            seen.add(name)
            keep.append(name)
        limit = target_ratios.get(cat)
        out[cat] = keep[:limit] if isinstance(limit, int) and limit >= 0 else keep
    if off_colour:
        warnings.append("Dropped %d card(s) outside the commander's colour identity: %s" % (len(off_colour), ", ".join(off_colour[:8])))
    count = sum(len(v) for v in out.values())
    if count > total:
        # Over: trim from the tail of the largest category (the model's late picks), one card at a time.
        excess = count - total
        while excess > 0:
            cat = max(out, key=lambda c: len(out[c]))
            if not out[cat]:
                break
            out[cat].pop()
            excess -= 1
        warnings.append("Trimmed the candidate list from %d to %d cards." % (count, total))
    return out
