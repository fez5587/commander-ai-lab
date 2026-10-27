# Laya connections

Laya (and Kev: same `POST /v1/systemone` API) is a **decision model**: typed multiple-choice, yes/no and score questions about a text, answered in
one forward pass, no generated text. This page is where it plugs into the strategic-intelligence work in this repository
([INTELLIGENCE-HANDOFF.md](INTELLIGENCE-HANDOFF.md)), what is measured, and how to make it worth using. Written 2026-10-26.

**Short version.** Every connection below is built, tested offline and **off by default**. Zero-shot, Laya is a poor action chooser
(below the always-guess baseline) but a reasonable card classifier (48% against 17% by chance); it is too slow on a CPU for Forge's
priority path (8 s against a 1.5 s budget). The way forward is fine-tuning on the decisions these connections record.

## What connects where

| Connection | Code | Mode | Notes |
|---|---|---|---|
| Farmhouse macro choice: Laya picks among CSIE's candidate macros, reading the CSIE ranking and ITI objective in its text | `farmhouse_decide.py` (called from `deepseek_brain.py`) | `DECISION_MODE=shadow` records agreement with the LLM; `assist` uses a pick it is `DECISION_MIN_CONF` sure of | existing; only offered candidates are accepted |
| **Forge concrete priority choice**: Laya picks among the same concrete candidate IDs Forge sent to `/api/policy/decide/concrete` | `farmhouse_laya.advise_concrete` / `attach_concrete`, called from `routes/policy.py` | `shadow`: adds `laya` to the response, selection unchanged. `assist`: may replace `selected_id`, never one Forge tagged `stops_immediate_win` / `wins_now`; the original is kept as `csie_selected_id` | candidate described by kind, card, cost, target, tags (never by its opaque id); Forge still executes only an ID it sent |
| **Extra questions in the same call**: "how dangerous are the opponents' boards?" (none/low/moderate/high/lethal) and "is there a threat that must be answered?" | `farmhouse_laya._extra_questions` | whenever Laya is asked | one forward pass answers all questions, so they cost almost nothing; recorded as weak signals for the learned-threat work, **not used to decide** |
| **Capture**: every call is appended to `data/decisions/laya-shadow-<date>.jsonl` (`laya-shadow-v1`) | `farmhouse_laya.capture` | shadow / assist | the text Laya read, the offered options, its answer and extras, and the other deciders' picks (`labels`: `llm`, `csie`, `selected`) |
| **Fine-tuning data**: capture log -> `train.jsonl` / `valid.jsonl` / `manifest.json`, split by game, with the baselines to beat | `scripts/farmhouse_laya_dataset.py` | offline | the manifest reports commonest-action, CSIE-vs-teacher and Laya-zero-shot agreement on the validation side |
| **Card role evidence**: removal, board wipe, ramp, card draw, counterspell, protection, tutor, creature, other, from Oracle text | `farmhouse_laya.classify_roles` (batched, cached, with `source: laya:<model>` provenance); `scripts/farmhouse_laya_roles_eval.py` measures it | on demand | evidence for ITI / Tagger-style use, never a rules fact |

Not built (still the open items in the handoff, none of which Laya can substitute for): compiling and running the Forge controller, the bounded
combo executor, the multi-turn planner, the calibrated learned-threat model. Laya's "threat" and "must answer" extras are only raw material for that last one.

## Where the hooks are
- `src/commander_ai_lab/sim/farmhouse_decide.py` (committed): the macro-choice client gained the extra questions, the state text/options in its
  answer, and `record()` writes the capture line.
- `routes/policy.py` (**working tree only, uncommitted**: the route itself is part of the pending strategic-evaluator work, so the hook was
  left for that commit). It is one block in `decide_concrete`, after the result is computed:
  `if _L.mode() != "off": advice = await run_in_threadpool(_L.advise_concrete, ...); result = _L.attach_concrete(...)`, guarded by `except ImportError`.
- Everything else is new files: `farmhouse_laya.py`, the two scripts, `tests/farmhouse/test_farmhouse_laya*.py`, `farmhouse/laya/`.

## Settings
`DECISION_URL` (e.g. `http://laya.commander-lab.svc.cluster.local:8080`), `DECISION_MODE` (`off` default / `shadow` / `assist`), `DECISION_MODEL`
(a checkpoint such as `typed-decisions`), `DECISION_MIN_CONF` (0.7), `DECISION_TIMEOUT` (5 s), `DECISION_TIMEOUT_FORGE` (**1.5 s**: Forge waits on this),
`DECISION_API_KEY`, `LAYA_CAPTURE_DIR` (`data/decisions`), `LAYA_CAPTURE=0` to stop writing capture lines. Three failures in a row pause asking for a minute.

## Rules the code enforces
1. Text is built from public facts and the acting player's own hand. An opponent's `hand` field is never read, even if a client sends one (tested).
2. Laya's answer must be one of the options that were offered; anything else is discarded.
3. In assist, a pick Forge marked `stops_immediate_win` / `wins_now` is never overridden, and the response says what CSIE chose.
4. A missing, slow or broken server returns "no answer"; nothing raises and the game proceeds as without Laya.
5. Scores and probabilities are relative signals; Laya's probabilities are not calibrated for this game.

## What was measured (2026-10-26, `typed-decisions` checkpoint, 4-thread CPU)
- **Macro action vs the LLM, 149 recorded decisions:** 16.1% (English) and 18.6% (typed-decisions); always guessing the commonest action 27.5%.
- **Forge-shaped priority choice, 3 scenarios, with CSIE's ranking in the text:** agreed with CSIE all three times (expected: the ranking is in the
  text it reads), 42-48% sure, **7.5-8.4 s per call** with the extra questions. That is five times Forge's 1.5 s budget: on this CPU the Forge path
  would time out and fall back; it needs a GPU (33 ms quoted) or a smaller text.
- **Extras:** "threat" said *high* for a lethal stack, a calm board and a behind board alike (useless zero-shot); "must answer" gave 0.48 for the
  lethal stack against 0.25 and 0.29 for the others (right direction, weak).
- **Card roles from Oracle text, 150 unambiguous cards:** 48.0% (16.7% by chance among the six roles present); counterspell 76%, board wipe 64%,
  removal 40%, tutor 40%, ramp 36%, card draw 32%; when 70% sure, 69% right but only 11% of cards reach that. **3.2 s per card** in batches on the CPU:
  fine for the cards in a hand (cached), not for tagging 30,000 cards (about a day).

## How to make it worth using
1. Run games with `DECISION_URL` set and `DECISION_MODE=shadow` (Farmhouse LLM games, and Forge games through `/api/policy/decide/concrete`).
2. `python scripts/farmhouse_laya_dataset.py --teacher llm` (or `csie`, or `selected`). Read `manifest.json`: the **validation baselines** are the bar.
   A student of CSIE can at best equal CSIE, which is free; a student of the LLM is the useful one. Several thousand rows are needed.
3. Fine-tune with Laya's own notebook/script (github.com/NandhaKishorM/laya, `notebooks/`; 2xT4 about 4-5 hours, or Apple silicon). Its worked example
   (browser agent choosing among ~45 concrete candidates) went from 0.10 zero-shot to 0.66, so a concrete-candidate head is the shape that works.
   Adapt the row field names if its loader differs.
4. Serve the result (`LAYA_EXTRA_MODELS` / `DECISION_MODEL`), rerun `scripts/farmhouse_decision_eval.py` and the dataset manifest, and only then
   try `assist`. A GPU host makes the Forge path possible.
5. Keep game result and decision quality separate (handoff boundary 6 / learned-threat contract): a captured label is a teacher's choice, not proof it was right.

Deployment reference copies are in `farmhouse/laya/` (image, manifests, CI); the source of truth is the farmhouse-os GitOps repo.
