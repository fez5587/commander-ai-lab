"""Deck LINKS for every deck import (copied into the app as services/farmhouse_deck_links.py by the Dockerfile).

Upstream could import from an Archidekt or EDHREC link on the simulator page only; the Deck Builder import took pasted text and
nothing else, and Moxfield (where most people keep decks) was not accepted anywhere. This reads a Moxfield or Archidekt deck link and
returns what the existing importers already understand: a profile for the simulator import, or plain "[Commander] / [Main]" text for
the Deck Builder's two imports (so a link pasted into the same box works, and the card-by-card Scryfall matching is unchanged).

Moxfield has no official public API; api2.moxfield.com answers server-side requests for PUBLIC decks (tested from the cluster
2026-10-26: HTTP 200, a named User-Agent is enough). A private or deleted deck comes back 401/403/404, and the message says so and
says what to do instead (Moxfield: Export > Copy as text, then paste that). Nothing here logs in to anything.
"""
import json
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

UA = "CommanderAILab/3.0 (self-hosted)"
MOXFIELD_API = "https://api2.moxfield.com/v2/decks/all/"

_MOX = re.compile(r"^https?://(?:www\.)?moxfield\.com/decks/([A-Za-z0-9_-]+)(?:[/?#].*)?$", re.I)
_ARC = re.compile(r"^https?://(?:www\.)?archidekt\.com/decks/(\d+)(?:[/?#].*)?$", re.I)

UNSUPPORTED = ("That link is not one I can read. Moxfield and Archidekt deck links work; for anything else, paste the decklist as text "
               "(Moxfield: Export > Copy as text).")


def link_kind(text):
    """(kind, id, url) when `text` is nothing but one http(s) link, else None. kind: moxfield | archidekt | unsupported."""
    t = (text or "").strip()
    if not t or len(t.split()) != 1 or not re.match(r"^https?://", t, re.I):
        return None
    m = _MOX.match(t)
    if m:
        return "moxfield", m.group(1), t
    m = _ARC.match(t)
    if m:
        return "archidekt", m.group(1), t
    return "unsupported", None, t


def _get_json(url):
    with urlopen(Request(url, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_moxfield(deck_id, attempts=4):
    last = None
    for i in range(attempts):
        try:
            return _get_json(MOXFIELD_API + deck_id)
        except HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise ValueError("Moxfield would not share that deck (it is private, unlisted for sharing, or the link is wrong). "
                                 "Open it in Moxfield, choose Export > Copy as text, and paste that here instead.")
            if exc.code in (429, 500, 502, 503, 504):
                last = exc
                try:
                    wait = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0.0
                time.sleep(max(wait, 2.0) + i * 2.0)
                continue
            raise ValueError("Moxfield answered HTTP %s for that deck." % exc.code)
        except (ValueError, OSError) as exc:
            raise ValueError("Could not read the deck from Moxfield (%s)." % str(exc)[:80])
    raise ValueError("Moxfield is rate-limiting this server right now (HTTP %s). Try again in a minute." % (last.code if last else "429"))


def _board(deck, key):
    out = {}
    for name, entry in (deck.get(key) or {}).items():
        card = (entry or {}).get("card") or {}
        label = card.get("name") or name
        try:
            qty = int((entry or {}).get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        out[label] = out.get(label, 0) + max(qty, 1)
    return out


def moxfield_profile(deck, url):
    """The same profile dict the Archidekt importer builds. Sideboard, maybeboard and companions are left out (the 99 + commander)."""
    commanders, main = _board(deck, "commanders"), _board(deck, "mainboard")
    profile = {
        "name": deck.get("name") or "Moxfield deck",
        "commander": next(iter(commanders), None),
        "source": "Moxfield",
        "sourceUrl": deck.get("publicUrl") or url,
        "commanders": commanders,
        "mainboard": main,
        "colorIdentity": list(deck.get("colorIdentity") or []),
        "totalCards": sum(commanders.values()) + sum(main.values()),
    }
    if profile["commander"] and not deck.get("name"):
        profile["name"] = "%s — Moxfield" % profile["commander"]
    return profile


def fetch_profile(url):
    kind = link_kind(url)
    if not kind or kind[0] == "unsupported":
        raise ValueError(UNSUPPORTED)
    if kind[0] == "moxfield":
        profile = moxfield_profile(fetch_moxfield(kind[1]), kind[2])
    else:
        from services.import_helpers import _fetch_archidekt_deck  # the app's own Archidekt reader
        try:
            profile = _fetch_archidekt_deck(kind[1])
        except HTTPError as exc:
            raise ValueError("Archidekt would not share that deck (HTTP %s). Is it public?" % exc.code)
    if not profile["commanders"] and not profile["mainboard"]:
        raise ValueError("That deck has no cards in it (or no main deck I can read).")
    return profile


def profile_to_text(profile):
    """Plain text in the Deck Builder's own format: [Commander] then [Main], one 'qty name' per line."""
    lines = ["[Commander]"]
    lines += ["%d %s" % (q, n) for n, q in profile["commanders"].items()]
    lines += ["", "[Main]"]
    lines += ["%d %s" % (q, n) for n, q in profile["mainboard"].items()]
    return "\n".join(lines) + "\n"


def local_card(name, conn=None):
    """{'id', 'name'} from the local Scryfall bulk database (113k cards, no network), or None. Matches the exact name, or the front
    face of a double-faced / split card ('Fire' finds 'Fire // Ice'). The Deck Builder's importers only use these two fields, and
    looking each card up on Scryfall's website instead hit its rate limit (HTTP 429) after a few dozen cards of a 100-card list,
    which the app reported as 'not found'."""
    try:
        if conn is None:
            from services.scryfall_bulk import get_bulk_db
            conn = get_bulk_db()
        n = (name or "").strip()
        if not n:
            return None
        like = n.replace("!", "!!").replace("%", "!%").replace("_", "!_") + " // %"
        row = conn.execute("SELECT scryfall_id, name FROM cards WHERE LOWER(name) = LOWER(?) OR LOWER(name) LIKE LOWER(?) ESCAPE '!' "
                           "ORDER BY (LOWER(name) = LOWER(?)) DESC, edhrec_rank ASC LIMIT 1", (n, like, n)).fetchone()
        return {"id": row[0], "name": row[1]} if row else None
    except Exception:
        return None


def expand(text):
    """(text, deck name). A lone Moxfield/Archidekt link becomes the decklist text; anything else comes back unchanged.
    A lone link we cannot read raises ValueError with a message meant for the person."""
    kind = link_kind(text)
    if kind is None:
        return text, None
    profile = fetch_profile(kind[2])
    return profile_to_text(profile), profile["name"]
