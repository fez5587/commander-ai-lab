"""Apply the FarmHouse edits to a phase-rs checkout (run in the Docker build, from the repository root).

    python apply_phase_patches.py <phase-checkout> <llm-seat-crate-dir>

1. crates/phase-ai/src/auto_play.rs: a process-wide ACTION_OVERRIDE hook, asked before the built-in AI chooses. It returns Option<GameAction>;
   None (the default, and what every failure returns) leaves phase's own choice untouched. The chosen action still goes through the same
   contract check (`contract.permits`) as before.
2. crates/phase-ai/src/bin/ai_commander.rs: `fn main` becomes `pub fn main` so the new crate can include and call it.
3. The new crate is copied into crates/farmhouse-llm-seat (the workspace takes every crates/* member).

Every anchor must match exactly once or the build stops, so a phase update that moves a line is reported here, not in a compile error."""
import os
import shutil
import sys

root, crate = sys.argv[1], sys.argv[2]


def edit(rel, edits):
    path = os.path.join(root, rel)
    text = open(path, encoding="utf-8", newline="").read()
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            raise SystemExit("%s: anchor found %d times (need 1): %r" % (rel, n, old[:80]))
        text = text.replace(old, new)
    open(path, "w", encoding="utf-8", newline="").write(text)
    print("patched", rel)


HOOK = '''use crate::search::choose_action_with_session;

/// FarmHouse: an optional process-wide override, asked BEFORE the built-in AI chooses for a seat. A host (see crates/farmhouse-llm-seat)
/// installs one to let a model pick among the engine's own options. It returns None to leave the choice to the built-in AI, which is also
/// what it must return on any failure; whatever it returns still has to pass the same decision-contract check below.
pub type ActionOverride = Box<
    dyn Fn(
            &engine::types::game_state::GameState,
            &engine::ai_support::AiDecisionContract,
            engine::types::player::PlayerId,
            engine::types::player::PlayerId,
        ) -> Option<engine::types::actions::GameAction>
        + Send
        + Sync,
>;
static ACTION_OVERRIDE: std::sync::OnceLock<ActionOverride> = std::sync::OnceLock::new();

/// Install the override. Returns false if one is already installed (it can be set once per process).
pub fn install_action_override(hook: ActionOverride) -> bool {
    ACTION_OVERRIDE.set(hook).is_ok()
}
'''

edit("crates/phase-ai/src/auto_play.rs", [
    ("use crate::search::choose_action_with_session;\n", HOOK),
    ("    let action = match choose_action_with_session(state, semantic_owner, config, rng, session) {\n        Some(action) => action,\n        None => {\n            tracing::warn!(player = ?actor, \"choose_action returned None",
     "    // The override's pick must be one the engine will really APPLY, not only one it lists: a model chose both modes of a modal spell whose\n"
     "    // entwine cost it could not pay, the contract listed the option, apply refused it, and the whole game stalled (2026-10-27). Try it on a\n"
     "    // copy of the game first; anything the engine refuses is dropped and the built-in AI plays the move. (Stack-pass rechecks have their\n"
     "    // own verified path and are left alone.)\n"
     "    let farmhouse_choice = ACTION_OVERRIDE\n        .get()\n        .and_then(|hook| hook(&*state, &contract, semantic_owner, actor))\n"
     "        .filter(|candidate| {\n"
     "            verified_ai_stack_pass_player(&*state, candidate).is_some()\n"
     "                || apply_interaction(&mut (*state).clone(), actor, semantic_owner, candidate.clone()).is_ok()\n"
     "        });\n"
     "    let action = match farmhouse_choice\n        .or_else(|| choose_action_with_session(state, semantic_owner, config, rng, session))\n    {\n        Some(action) => action,\n        None => {\n            tracing::warn!(player = ?actor, \"choose_action returned None"),
])
edit("crates/phase-ai/src/bin/ai_commander.rs", [("\nfn main() {\n", "\npub fn main() {\n")])

dest = os.path.join(root, "crates", "farmhouse-llm-seat")
os.makedirs(os.path.join(dest, "src"), exist_ok=True)
shutil.copy(os.path.join(crate, "Cargo.toml"), os.path.join(dest, "Cargo.toml"))
shutil.copy(os.path.join(crate, "src", "main.rs"), os.path.join(dest, "src", "main.rs"))
print("added crates/farmhouse-llm-seat")
