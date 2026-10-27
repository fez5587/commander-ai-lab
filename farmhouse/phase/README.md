# phase-sim: rules-accurate Commander games with phase-rs

Reference copies of what runs in the cluster (source of truth: `infrastructure/apps/commander-lab/` in the farmhouse-os repo). Background and the assessment
that led here: `docs/PHASE-AND-PLAYTESTER.md`.

| File | What |
|---|---|
| `phase_sim.py` | the service: stdlib HTTP, one job at a time. `POST /run` four decks -> `GET /job/<id>` per-game results, wins by seat, 95% interval, warnings. Optional model seats and two loopback-only relays (`/llm/v1/chat/completions`, `/jev/systemone`) |
| `Dockerfile` | builds phase-rs `v0.118.0` (`ai-commander`, `ai-duel`) and takes the card database that release was published with (SHA-256 checked) |
| `Dockerfile.llm` | the same plus `ai-commander-llm`: model-driven seats |
| `llm-seat/` | the Rust crate and the two anchored edits to phase's source that make model seats possible |
| `phase_smoke.py`, `test_phase_sim.py` | CI: real games through the service; 49 offline checks against a stub engine |
| `deployment-phase.yaml` | Deployment, Service and a NetworkPolicy (in: the app only; out: DNS, Olla and Laya only) |

The app side is `routes/farmhouse_phase.py` (four saved decks -> the service) and `ui/phase-sim.html`; both are real source in this repository, registered by
`lab_api.py` and linked from the home page.

## Model seats: how they work and why they are safe
phase's headless runner has no model support; the model seats exist only in phase's browser client. `phase-llm` (the crate) deliberately does no networking:
it renders the engine's option list into a prompt, builds the HTTP request, and parses the reply, and the host performs the call. `llm-seat/` is that host
for the headless runner:

1. `apply_phase_patches.py` adds a process-wide `ACTION_OVERRIDE` hook in `phase-ai/src/auto_play.rs` (asked before the built-in AI chooses) and makes the runner's `main` `pub`.
2. The crate `farmhouse-llm-seat` includes the runner's source as a module (`#[path]`) and installs the hook. `phase-llm` depends on `phase-ai`, so the hook cannot
   live inside phase-ai's own binary.
3. For each decision on a model seat: the engine issues its option list (`AiDecisionContract`); phase-llm builds the prompt (a forced move with one option is never asked);
   this crate makes the HTTP call (blocking, no TLS, 20 s limit); phase-llm turns the reply into one of the engine's options; the choice is checked against the contract
   here and again by `run_ai_action`. **Any** problem returns "no opinion" and phase's own AI plays the move. A cap on model calls per run (default 300) bounds the cost.
4. The service supplies the endpoints through its own loopback-only relays: the chat relay switches the model's thinking off (twin-turbo otherwise thinks for tens of
   seconds a move) and caps the answer; the decision relay unwraps phase's `Jev` envelope and forwards the System One request to Laya or Kev.

The model never authors an action and never sees hidden cards beyond what phase-llm renders for that seat.

## Status
Written 2026-10-27. The service logic is tested offline; the Rust crate has not compiled outside CI when this was written (no Rust toolchain on the workstation), so
its first CI run is the first real test. Check `.github/workflows/build-farmhouse-phase-sim.yml` results in the farmhouse-os repo.
