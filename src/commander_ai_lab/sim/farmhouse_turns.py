"""A DeepSeek-engine turn that asks the model more than once (copied to commander_ai_lab/sim/farmhouse_turns.py by the Dockerfile).

Upstream asked the model ONE question per turn and applied the answer as a single action, then filled in with a heuristic. That is
one model call per turn for a turn that really has several decisions (land, a spell, another spell, attack). This keeps asking, up
to BRAIN_STEPS times (default 4), re-reading the board after each action, and stops when the model attacks or holds, when an action
changes nothing (so it cannot loop), or at the limit. Whatever is left uses the engine's own heuristic exactly as before.

Everything it touches is an existing engine method, so the rules enforcement (one land a turn, mana, the two-spell cap per call) is
the engine's own.
"""
import os

CAST = ("cast_creature", "cast_removal", "cast_board_wipe", "cast_ramp", "cast_spell")
ATTACK = ("attack_all", "attack_safe")


def steps():
    try:
        return max(1, min(int(os.environ.get("BRAIN_STEPS", "4")), 8))
    except ValueError:
        return 4


def play_turn(engine, sim, pi, turn, events, deck_context=None):
    """Same contract as DeepSeekGameEngine._play_turn_deepseek: plays the turn, returns the ML action type of the first decision."""
    p = sim.players[pi]
    first_action = None
    first_mana = engine._count_untapped_lands(sim, pi)
    combat_done = False

    for step in range(steps()):
        mana = engine._count_untapped_lands(sim, pi)
        decision = engine.brain.choose_action(sim, pi, turn, mana, deck_context=deck_context)
        action = decision.get("action", "hold")
        target = decision.get("target_card", "") or ""
        if first_action is None:
            first_action = action
        if engine.record_log:
            events.append("[AI Brain: %s] Step %d: %s%s (%s)" % (decision.get("source", "?"), step + 1, action,
                                                                  (" → " + target) if target else "", decision.get("reasoning", "")))
        if step == 0:                                   # the land drop, as upstream always does
            engine._play_land(sim, pi, target if action == "play_land" else None, events)
            mana = engine._count_untapped_lands(sim, pi)

        if action in CAST:
            before = (len(p.hand), engine._count_untapped_lands(sim, pi))
            engine._play_spells_targeted(sim, pi, mana, action, target, events)
            if (len(p.hand), engine._count_untapped_lands(sim, pi)) == before:
                break                                   # nothing castable: stop asking, fall through to combat
            continue
        if action in ATTACK:
            engine._resolve_combat_deepseek(sim, pi, turn, action, events)
            combat_done = True
            break
        if action == "hold":
            if engine.record_log:
                events.append("Holds — no attack")
            combat_done = True
            break
        # play_land or anything unknown: try a heuristic spell once, then combat
        engine._play_spells_heuristic(sim, pi, engine._count_untapped_lands(sim, pi), events)
        break

    if not combat_done:
        engine._resolve_combat_heuristic(sim, pi, turn, events)
    return engine._map_action_to_ml_type(first_action or "hold", first_mana)
