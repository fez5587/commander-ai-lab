"""
Legal Move Generator
====================
Generates the set of legal moves a player can take from a
:class:`CommanderGameState`. This replaces the "pass-only" stub in
``CommanderGameState.get_legal_moves`` so the turn_manager/policy-server
path can drive real games instead of just passing priority every turn.

The emitted moves are intentionally coarse (macro-actions: play_land,
cast_creature, cast_removal, cast_ramp, cast_draw, cast_commander,
attack, block, pass_priority) so that both the neural policy and
heuristic/LLM brains can pick from the same list.

Long-term, when the Forge IPC path is fully wired, Forge's own legal
action list will replace the output of this module.  Until then, this
is the authoritative move source for the Python rules layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from commander_ai_lab.sim.models import Card, Phase

if TYPE_CHECKING:
    from commander_ai_lab.sim.game_state import CommanderGameState


# Map from card enrichment flags to macro-action categories the AI brain
# recognizes.  Order matters: the first True flag wins.
_SPELL_FLAG_TO_CATEGORY: tuple[tuple[str, str], ...] = (
    ("is_ramp",        "cast_ramp"),
    ("is_removal",     "cast_removal"),
    ("is_board_wipe",  "cast_removal"),
)


def _classify_spell(card: Card) -> str:
    """Map a Card to a macro-action category string.

    Matches the 8-action macro-action space used by the policy network
    (``scope.py``): cast_creature, cast_removal, cast_draw, cast_ramp,
    cast_commander, attack_opponent, hold_mana, pass.  Cards that don't
    fit any flag fall back to ``cast_spell`` so downstream code can
    still execute them.
    """
    for flag, category in _SPELL_FLAG_TO_CATEGORY:
        if getattr(card, flag, False):
            return category

    # Heuristic: draw spells often mention "draw" in oracle text
    ot = (getattr(card, "oracle_text", "") or "").lower()
    if "draw" in ot and ("card" in ot or "cards" in ot):
        return "cast_draw"

    if card.is_creature():
        return "cast_creature"
    return "cast_spell"


def _count_available_mana(game_state: "CommanderGameState", seat: int) -> int:
    """Rough available mana = floating pool + untapped lands on battlefield.

    This is intentionally permissive.  A production rules engine would
    also consider mana dorks, mana rocks, and color requirements, but
    for macro-action gating CMC-vs-total-mana is sufficient.
    """
    player = game_state.players[seat]
    mana = 0

    # Floating mana pool (if ManaPool has ``total``)
    pool = getattr(player, "mana_pool", None)
    if pool is not None:
        total_fn = getattr(pool, "total", None)
        if callable(total_fn):
            try:
                mana += int(total_fn())
            except Exception:
                pass

    # Untapped lands on battlefield (each produces ~1 mana)
    for card in game_state.sim_state.get_battlefield(seat):
        if card.is_land() and not getattr(card, "tapped", False):
            mana += 1

    return mana


def _phase_is(current: object, target: Phase) -> bool:
    """Safe phase comparison that works with Phase enum or raw string."""
    if current == target:
        return True
    if isinstance(current, str) and current == target.value:
        return True
    return False


def generate_legal_moves(game_state: "CommanderGameState", seat: int) -> list[dict]:
    """Produce the list of legal moves for ``seat`` given ``game_state``.

    Always includes a pass-priority move (id 0) as a safe fallback.
    Additional moves are emitted based on phase, turn ownership, and
    available resources.
    """
    moves: list[dict] = [
        {"id": 0, "category": "pass_priority", "description": "Pass"}
    ]

    if not (0 <= seat < len(game_state.players)):
        return moves

    player = game_state.players[seat]
    if getattr(player, "eliminated", False):
        return moves

    current_phase = game_state.current_phase
    is_active = (seat == game_state.active_player_seat)
    move_id = 1

    # ── Sorcery-speed actions (main phases, active player only) ──
    if is_active and (
        _phase_is(current_phase, Phase.MAIN1)
        or _phase_is(current_phase, Phase.MAIN2)
    ):
        # Land drop — respect one-land-per-turn unless modified
        lands_played = getattr(player, "lands_played_this_turn", 0)
        land_limit = getattr(player, "land_plays_allowed", 1)
        if lands_played < land_limit:
            for card in player.hand:
                if card.is_land():
                    moves.append({
                        "id": move_id,
                        "category": "play_land",
                        "card_name": card.name,
                        "description": f"Play {card.name}",
                    })
                    move_id += 1

        # Castable spells from hand
        available_mana = _count_available_mana(game_state, seat)
        for card in player.hand:
            if card.is_land():
                continue
            cost = int(getattr(card, "cmc", 0) or 0)
            if cost <= available_mana:
                category = _classify_spell(card)
                moves.append({
                    "id": move_id,
                    "category": category,
                    "card_name": card.name,
                    "cmc": cost,
                    "description": f"Cast {card.name} ({cost} mana)",
                })
                move_id += 1

        # Commander from the command zone (respect commander tax)
        tax = 0
        if hasattr(player, "commander_tax"):
            try:
                tax = int(player.commander_tax())
            except Exception:
                tax = 0
        for cmd in getattr(player, "commander_zone", []):
            base_cost = int(getattr(cmd, "cmc", 0) or 0)
            total_cost = base_cost + tax
            if total_cost <= available_mana:
                moves.append({
                    "id": move_id,
                    "category": "cast_commander",
                    "card_name": cmd.name,
                    "cmc": total_cost,
                    "description": (
                        f"Cast {cmd.name} from command zone "
                        f"(base={base_cost}, tax={tax})"
                    ),
                })
                move_id += 1

    # ── Declare attackers — one attack move per legal attacker ──
    if is_active and _phase_is(current_phase, Phase.DECLARE_ATTACKERS):
        bf = game_state.sim_state.get_battlefield(seat)
        for creature in bf:
            if not creature.is_creature():
                continue
            if getattr(creature, "tapped", False):
                continue
            # Summoning sickness: played this turn and no haste
            turn_played = getattr(creature, "turn_played", -1)
            has_haste = creature.has_keyword("haste") if hasattr(creature, "has_keyword") else False
            if turn_played == game_state.turn and not has_haste:
                continue
            moves.append({
                "id": move_id,
                "category": "attack",
                "attacker_id": creature.id,
                "card_name": creature.name,
                "description": f"Attack with {creature.name}",
            })
            move_id += 1

    # ── Declare blockers — emit block pairings for defending player ──
    if _phase_is(current_phase, Phase.DECLARE_BLOCKERS) and not is_active:
        combat = game_state.sim_state.combat
        if combat and getattr(combat, "active", False):
            defender_creatures = [
                c for c in game_state.sim_state.get_battlefield(seat)
                if c.is_creature() and not getattr(c, "tapped", False)
            ]
            for atk_id in getattr(combat, "attackers", []):
                atk = None
                for bf in game_state.sim_state.battlefields:
                    for c in bf:
                        if c.id == atk_id:
                            atk = c
                            break
                    if atk is not None:
                        break
                if atk is None:
                    continue
                for blocker in defender_creatures:
                    moves.append({
                        "id": move_id,
                        "category": "block",
                        "blocker_id": blocker.id,
                        "attacker_id": atk_id,
                        "description": f"Block {atk.name} with {blocker.name}",
                    })
                    move_id += 1

    return moves
