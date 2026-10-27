# Laya as the simulator's "which action?" model: evaluation, 2026-10-26

**Result: not usable as shipped.** Both checkpoints agreed with the LLM less often than a model that always answers the commonest action.

## What was measured
The simulator's LLM brain ("twin-turbo") picks one of nine actions about 25 times a game. A decision model (Laya, or Kev; both serve
`POST /v1/systemone`) could answer that kind of question in one forward pass instead of a 7-second generation. To find out whether it
could stand in, every LLM decision the app had recorded (`/app/logs/decisions/*.jsonl`: the game state and the action the LLM chose)
was replayed through Laya with the same question, and the answers compared. The LLM is the *teacher*, not the truth: this measures
whether a fast model can imitate the slow one, not which plays better.

Script: `image/farmhouse_decision_eval.py` (in the app image as `/app/scripts/farmhouse_decision_eval.py`):
`DECISION_URL=http://laya:8080 python scripts/farmhouse_decision_eval.py /app/logs/decisions english typed-decisions`

## Numbers (149 recorded decisions)
LLM's choices: play_land 41, hold 31, attack_all 28, cast_creature 20, cast_spell 13, attack_safe 7, cast_removal 5, cast_ramp 4.

| | agrees with the LLM | answers | time per answer | what it picked |
|---|---|---|---|---|
| always "play_land" (baseline) | **27.5%** | 149 | 0 | |
| `english` (ModernBERT-large, base) | 16.1% | 149 | 1.9 s | cast_creature 105, attack_all 26; never hold, almost never play_land |
| `typed-decisions` (fine-tuned) | 18.6% | 140 (9 timed out at 5 s) | ~4 s (shared CPU) | cast_creature 99, attack_all 26 |

It could say "cast_creature" and "attack_all" and little else; it never recognised "hold" (31 of the LLM's choices) or a removal spell.

## Why, and caveats
- The shipped checkpoints are trained on generic ticket-triage style decisions. Laya's own README says fine-tuning "on decisions from your
  own domain is where accuracy jumps" (0.766 against 0.362 for the base on its own benchmark). This game's decisions were never in its training.
- Speed on this cluster's CPU (4 threads) was 2-4 s per answer, not the ~33 ms quoted for a GPU. Against the LLM's ~7.5 s that is not the
  speed-up that was hoped for either, without a GPU.
- The question wording (the nine action descriptions) and the compact text it reads (`farmhouse_decide.compact_state`) are mine; a
  better prompt might help a little, not from 18% to usable.
- Part of the recorded data predates the card-data fix (v10), so some states had thinner hands than they would now.
- The LLM being the "truth" also caps the result: where the LLM is wrong, agreeing with it is not a win.

## Where this leaves it
- Laya is **staged but scaled to 0 replicas** (`deployment-laya.yaml`: `replicas: 0`; set to 1 to run it). The image
  (`ghcr.io/fez5587/farmhouse-laya:v1`, offline, weights baked in), the service and the network policy remain.
- The app is wired to use it (`DECISION_URL`, `DECISION_MODE`) and `DECISION_MODE` is `off`. `shadow` records agreement without
  affecting games; `assist` uses it when it is `DECISION_MIN_CONF` sure. Kev speaks the same API, so swapping is a URL.
- To make it worth using: fine-tune on this game's decisions (Laya ships a Kaggle 2xT4 notebook, about 4 hours, and an Apple/CPU
  script). The labels would be the LLM's recorded decisions; a few thousand are needed, so generate them first (LLM games at ~7 s a
  decision, or label states with the engine's own heuristic for the cheap, abundant version). Then re-run this evaluation.
- Node memory: it needed about 2 GB (3.6 GB with both checkpoints, which pushed the control-plane node to 92%; it is pinned to the other node).

## Later the same day: the new connections (see docs/LAYA-CONNECTIONS.md in the fork)
- **Forge-shaped priority choice** (CSIE's ranking in the text, three scenarios): agreed with CSIE each time, 42-48% sure, 7.5-8.4 s per call on the CPU against Forge's
  1.5 s budget. Not usable on a CPU; the code falls back silently.
- **Card roles from Oracle text** (150 unambiguous cards, 6 roles present, 9 offered): 48% against 17% by chance (counterspell 76%, board wipe 64%, removal 40%, tutor 40%,
  ramp 36%, card draw 32%), 3.2 s per card in batches. Much better than the action choice; useful as on-demand evidence for the cards in a hand, too slow to tag the whole pool.
- **Extra questions** in the same call: "threat" said *high* for everything; "must answer" gave 0.48 for a lethal stack against 0.25-0.29 otherwise (right direction, weak).
- Capture, dataset export and the rest are built and off by default; fine-tuning on the captured decisions is the path to making it useful.
