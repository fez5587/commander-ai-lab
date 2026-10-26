# FarmHouse changes

This branch (`farmhouse`) is `KoalaTrapLord/commander-ai-lab` at commit `54cc5e9` plus the changes below. They were made to run the app
**Python-only** (no Forge/Java, no PyTorch) in a Kubernetes namespace, with a local LLM behind an OpenAI-compatible gateway instead of
Anthropic/DeepSeek, and they were each found by actually running the app, not by reading it. Written 2026-10-26.

Every change started life as an anchored text patch (`patch_app.py` in the farmhouse-os repo, which still builds the deployed image from
the upstream commit); here they are ordinary source edits. Lines carry a `# FARMHOUSE_...` marker where the patch left one.

## What was wrong, and what changed

### The simulator played nothing (the big one)
| Problem | Change |
|---|---|
| Every card reached the simulator with **no type line and no rules text**. The loaders join the user's *collection* and a *card_records* table (both empty on a fresh install) and the Forge card cache that was meant to fill the gap is not installed. A land was indistinguishable from a creature: an 8-game batch cast 0 spells, dealt 0 damage and lost every game. | `services/farmhouse_sim.py` `fill_card_data` fills type, text, mana cost, cmc, P/T, keywords and colour identity from the 113k-card Scryfall bulk database. Wired into `services/forge_runner.py` (batch) and `routes/deepseek.py` (`_fetch_deck_card_data`, which the Monte Carlo and single-deck routes use). |
| The batch loader **did not repeat cards by quantity**: a 100-card deck loaded as 66-84 cards with *one* Island. | `expand_quantities` (same module). |
| The LLM engine asked the model **once per turn** and applied the answer as one action. | `src/commander_ai_lab/sim/farmhouse_turns.py`: up to `BRAIN_STEPS` (default 4) questions per turn (land, spell, spell, attack), re-reading the board after each; stops on attack/hold/no change. `BRAIN_STEPS=1` restores the original. |
| A model that "thinks" spent ~46 s on a one-line move. | `deepseek_brain.py` sends `reasoning_effort` from `LLM_REASONING` (46 s -> 1.8 s per move). |
| The engine dropdown defaulted to **Forge**, which cannot run without Java. | `ui/ai-lab.js`: only "LLM opponents" is offered. |

After the fixes: a 40-game Monte Carlo takes 0.4 s with real numbers (10 spells and 12.7 damage per game); two LLM-piloted games took
212 s with 49 model calls and no fallbacks.

### Deck import
| Problem | Change |
|---|---|
| **Moxfield links were accepted nowhere**, and the Deck Builder import took only pasted text. | `services/farmhouse_deck_links.py`: Moxfield (public decks, `api2.moxfield.com`) and Archidekt links in the simulator URL import and in both Deck Builder imports (a lone link in the paste box). A private deck says to use Moxfield's Export > Copy as text. |
| The Deck Builder import looked **every card up on scryfall.com**; after a few dozen it was rate-limited (HTTP 429) and the app reported the rest as "not found" (250-card deck: 55 matched; even plain `Sol Ring` failed). | `local_card()`: names resolve from the local bulk database first; the website is only the fallback. |

### Deck generation
- `deck_builder/pipeline/farmhouse_fit.py`: the model's list is **filtered to the commander's colour identity** (from the bulk DB), de-duplicated,
  fitted to per-category targets and trimmed to 99 + commander. Before this the model returned off-colour cards and 359-card lists.
- Scryfall bulk format changed (`jsonl_download_uri`, gzip JSONL; no `download_uri`).
- Scryfall answers **HTTP 400 to the default `python-requests` User-Agent** (every lookup became "Commander not found"): a named UA is sent everywhere.
- Scryfall 429s: requests back off on `Retry-After` and the pace is 0.25 s.
- `LLM_REASONING` / `LLM_MAX_TOKENS` are sent by the deck builder and coach.

### Coach / startup
- The coach was only initialised in `main()`, not when the app starts under uvicorn; `check_connection_sync()` was called but never defined;
  the coach used the first model the gateway listed rather than the configured one. All fixed.
- Status label says "Language model" instead of the hardcoded "Claude Opus".
- `RAG_AUTOBUILD` (default off in our deployment) controls whether the card-search index is built at startup (113k embeddings; about an hour).

## Settings (environment variables)
| Variable | Meaning |
|---|---|
| `LLM_URL`, `LLM_MODEL` | OpenAI-compatible endpoint (`.../v1`) and model for the coach and deck builder |
| `LLM_REASONING` (`none`), `LLM_MAX_TOKENS`, `LLM_TIMEOUT` | keep requests lean: reasoning off, cap on answer length |
| `COACH_PROVIDER`, `DECK_GEN_PROVIDER` = `local`, `DECK_GEN_MODEL` | use the local model, not Anthropic |
| `OLLAMA_BASE_URL`, `RAG_EMBEDDING_MODEL`, `RAG_AUTOBUILD` | card-search embeddings (one host only: vectors differ between hosts) |
| `BRAIN_API_BASE` (**no** `/v1`; the client appends it), `BRAIN_MODEL`, `BRAIN_MAX_TOKENS`, `BRAIN_STEPS` | the simulator's LLM opponents |

## Tests
`tests/farmhouse/` (standard library only, no network): `python -I -X utf8 tests/farmhouse/test_farmhouse_deck_links.py`, `..._sim.py`, `..._fit.py`.
Run them with `-I` and from outside the package folders; do not put `services/` on `sys.path` (`services/logging.py` shadows the standard library).

## Things still worth doing
- **Make the LLM-opponent turns faster** (a piloted move averages ~7.5 s: about 2.2k tokens in, ~30 out, so reading the prompt dominates).
  These are ideas, not measured; none is implemented:
  - *Shared prefix:* about 1.2k tokens of every prompt (the system prompt) are identical each time; a llama.cpp server can reuse them
    if the calls share the prefix, so only the board state is read each move.
  - *Smaller prompt:* send a compact board state instead of the indented JSON the engine builds now (`json.dumps(snapshot, indent=2)`
    in `deepseek_brain.py`).
  - *Parallel games:* run several games at once, which would also fill the GPU while one game waits on a move. Caveat: for long prompts
    two requests at once gave no extra throughput on a V100 (reading the prompt is compute-bound), so measure before relying on it.
- Opponents are the engine's heuristics; only the piloted seat uses the model.
- Build the card-search index and measure it.
- The upstream `LICENSE` is missing (`pyproject.toml` says MIT). Ask the author to add one before publishing a derivative widely.
- Forge-based features (the Java simulator, training) remain unavailable by design.
