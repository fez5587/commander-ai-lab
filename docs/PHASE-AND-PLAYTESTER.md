# phase-rs and commander-playtester: assessment and plan

Researched 2026-10-26 from the public repositories and data files only. **Nothing from either project has been run, vendored or deployed.**
Facts marked (verified) were read from the source; (unverified) are inferences to check before building on them.

## phase-rs/phase: a rules engine

A Rust rules engine (turns, priority, the stack, combat, layers, triggers, replacement effects) that compiles to native and WebAssembly, with a game
client, a WebSocket server and its own AI. Dual licensed Apache-2.0 / MIT (`LICENSE-APACHE`, `LICENSE-MIT`; GitHub's licence field shows "none" only
because there are two files). Card data from MTGJSON (MIT); card images come from Scryfall at runtime; the repo says it bundles no card art or
Comprehensive Rules text and has a DMCA/fan-content policy. One maintainer, young (weeks to months), fast-moving (latest engine tag v0.100.0).

Why it matters here: our Python simulator has no rules stack (the handoff says so: `stack_supported: false`) and Forge needs a JDK and Maven that
are not in the image. Phase is a rules-accurate engine that can run headless.

### Coverage of the decks in this app (verified, from phase's published `coverage-data.json`)
Phase reports 88.9% of 36,037 cards implemented overall and 92.5% for Commander. For the five saved decks, distinct cards supported:
Empress V8 72/77, Landfall 65/66, God and Anime 69/71, Im not dead yet 82/84, Pew Pew 88/93: **376 of 391 (96.2%)**. The 15 unsupported cards have
one or two parsing gaps each (e.g. Gale Waterdeep Prodigy, Hall of Echoes, Precursor Golem, Invigorate, Mavinda, Drana and Linvala, Tibalt's Trickery).
"Supported" means every ability parsed; it is not a guarantee that each card plays correctly. How the engine treats an unsupported card in a game
(blank, no-op, refused) is **unverified**; test it before trusting a result that involves one.

### The parts that fit
| Part | What it gives us | Notes |
|---|---|---|
| `ai-duel` (crate `phase-ai`) | headless AI-vs-AI games from the command line: seeded, deterministic "measurement mode", batches, JSON reports, a 4-player Commander suite, paired-seed comparison | decks are plain JSON `{"name", "main":[{"name","count"}]}` (verified). **No prebuilt binary is released**: it needs a Rust build (in CI or a container, not on the workstation). How a custom Commander deck and commander are supplied to `--commander-suite` is unverified (that suite reads a feed file) |
| `phase-server` | a WebSocket game server (lobby, hidden information, spectators) | a prebuilt image is published (`ghcr.io/phase-rs/phase-server`, tags `latest`, `preview`, `sha-...`). A Python client could play seats and record every decision with real rules (protocol documentation not yet read) |
| `phase-llm` | **model-driven AI seats**: the engine hands the model its legal option list, the model returns an index, the engine re-validates it, and any failure falls back to the heuristic AI | same principle as `farmhouse_laya.py`. Speaks OpenAI-style chat (so Olla / twin-turbo) **and the System One protocol Laya and Kev serve** (verified: `WireProtocol::SystemOneRelay`, question id `pick`, 2 to 255 options) |
| `POST /jev/systemone` relay in `phase-server` | forwards the System One request to the URL in `TYPESAFE_API_URL` (verified, env var, `http` allowed for local setups) | point it at `http://laya...:8080/v1/systemone` to put Laya or Kev in a seat. The relay sends the key as `Authorization: Bearer` and does not follow redirects |

The attraction for the Laya work: a captured decision from phase is an **engine-issued option list plus the pick**, in rules-accurate games. That
is a better fine-tuning source than this repository's approximate simulator, and the exporter (`scripts/farmhouse_laya_dataset.py`) could take it with a small adapter.

## RafaelAugustScherer/commander-playtester: a browser app on top of phase

TypeScript/React single-page app, no backend. Runs phase's engine as WebAssembly in a Web Worker. Deck library with Moxfield / Archidekt / plain-text
import, a goldfishing Monte Carlo (mulligan rate, screw and flood, land drops, ramp by turn 3, curve), real engine matches (watch, or pilot one seat),
and 1-50 match head-to-head runs with a win rate and 95% confidence interval. Pins the phase engine in `engine-manifest.json` (v0.100.0; its README
still says v0.57.0) and fetches the WASM and card data with SHA-256 checks. Zero stars, last pushed 2026-10-24.

**It has no LICENSE file** (verified: the repository metadata shows none). Without a licence the code is all rights reserved: it can be read and used
as a reference, but not copied into this repository or redistributed. Ask the author to add a licence (MIT would do) before reusing any of it. What is
worth taking as ideas, not code: the goldfishing metrics, the head-to-head confidence-interval report, the name-only deck payload for the engine, and
the engine-smoke gate that boots the real engine and plays a match to completion after every upgrade.

## Recommended plan, smallest useful step first
1. **Prove it on one deck.** Build `ai-duel` from the pinned phase tag in CI (Rust, container), convert one saved deck to the JSON format, run a seeded
   batch against a baseline deck, and read the result. Answers: does it run a Commander game end to end, what does it do with an unsupported card, how
   long is a game. This replaces the guesswork about the Python simulator's win rates.
2. **A `phase-sim` service** in the commander-lab namespace (no egress, called only by the app) with an endpoint "run N rules-accurate games for deck X
   against Y", returning win rate, confidence interval, wins by seat and the game log; the app's results panel reads it. The Python simulator stays for
   instant, rough Monte Carlo.
3. **Model seats.** Run `phase-server` with `TYPESAFE_API_URL` at Laya (or at a fine-tuned one later) and the OpenAI-compatible seat at Olla; compare
   heuristic-only, LLM and Laya seats. Record every decision for the Laya dataset.
4. **Only then** consider the playtester's UI ideas, after the author adds a licence or as a clean reimplementation.

## Risks and open questions
- Phase is a one-person, fast-moving project: pin tags, keep a smoke test that plays a full game, expect breaking changes.
- Rust builds are heavy; keep them in CI/containers. Resource and image size are unmeasured.
- Card coverage is parse-level; correctness per card is unmeasured. Unsupported cards must be flagged in any result.
- The phase server and its AI use CPU; its measured game speed is unknown (the README says a game is thousands of AI decisions).
- Legal: phase is Apache/MIT with Wizards fan-content wording; the playtester has no licence. The card data is MTGJSON (MIT).
- Not yet read: phase's WebSocket protocol, how `--commander-suite` accepts custom decks, and whether the server can run unattended self-play.
