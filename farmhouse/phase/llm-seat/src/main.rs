//! ai-commander with model-driven seats (FarmHouse).
//!
//! phase's headless runner plays four-player Commander games with the built-in AI on every seat. This binary is that same runner (its
//! source is included below, unchanged but for `fn main` being `pub`) plus a hook, installed before the games start, that asks a
//! language model or a decision model (Laya / Kev, over the System One protocol) to choose for the seats named in `PHASE_LLM_SEATS`.
//!
//! The model never authors an action. For each decision the engine issues its own option list (`AiDecisionContract`), phase-llm renders
//! it and builds the HTTP request, this file performs the HTTP call (the one thing phase-llm leaves to the host), and phase-llm turns the
//! reply into one of the engine's options. The pick is then checked against the contract a second time here, and again by
//! `run_ai_action`. ANY problem (forced move, timeout, an unreadable or stale answer, the call cap) returns `None` and phase's own AI
//! plays the move, so a model that is down, slow or wrong cannot stall or corrupt a game.
//!
//! Settings (environment): PHASE_LLM_SEATS "1,3" (seats 0-3; empty = none, the plain runner), PHASE_LLM_PROVIDER (OpenAiCompatible | Jev),
//! PHASE_LLM_BASE_URL, PHASE_LLM_MODEL, PHASE_LLM_API_KEY (leave empty for http:// endpoints), PHASE_LLM_MAX_TOKENS (256),
//! PHASE_LLM_TEMPERATURE (0.2), PHASE_LLM_TIMEOUT_S (20), PHASE_LLM_MAX_CALLS (total model calls for the whole process, default 300),
//! PHASE_LLM_DIFFICULTY (Medium: the strength hint in the prompt), PHASE_LLM_MAX_FAILS (4: model failures IN A ROW before every model seat
//! switches itself off for the rest of the process).

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use engine::ai_support::AiDecisionContract;
use engine::database::CardDatabase;
use engine::types::actions::GameAction;
use engine::types::game_state::GameState;
use engine::types::player::PlayerId;
use phase_ai::auto_play::install_action_override;
use phase_ai::config::AiDifficulty;
use phase_llm::{LlmEndpointConfig, LlmProvider, LlmReply};

// The unchanged runner, with `fn main` made `pub` by apply_phase_patches.py.
#[path = "../../phase-ai/src/bin/ai_commander.rs"]
#[allow(dead_code, unused_imports, unused_variables)]
mod commander;

struct Seats {
    seats: HashSet<u8>,
    endpoint: LlmEndpointConfig,
    difficulty: AiDifficulty,
    agent: ureq::Agent,
    cards_path: PathBuf,
    db: OnceLock<Option<CardDatabase>>,
    max_calls: u64,
    asked: [AtomicU64; 4],
    used: [AtomicU64; 4],
    fell_back: [AtomicU64; 4],
    total: AtomicU64,
    // Cost visibility and the circuit-breaker. A late-game prompt is thousands of tokens and a big-model server needs longer than the
    // per-call limit to read it; every such call used to burn the whole limit (a 9-minute game, 25 questions, 1 answer used). After
    // `max_fails` failures IN A ROW the model seats switch themselves off for the rest of the process and phase's AI plays on.
    timeouts: AtomicU64,
    consecutive_fails: AtomicU64,
    max_fails: u64,
    disabled: AtomicBool,
    millis: AtomicU64,
    answered: AtomicU64,
    prompt_chars: AtomicU64,
}

impl Seats {
    fn database(&self) -> Option<&CardDatabase> {
        self.db
            .get_or_init(|| CardDatabase::from_export(&self.cards_path.join("card-data.json")).ok())
            .as_ref()
    }

    /// The model's pick for `actor`, or None to let phase's AI play. Never panics on a bad answer.
    /// One failed model call (timeout, error status, unusable answer). Returns None so callers can `return self.failed(..)`.
    fn failed(&self, timeout: bool) -> Option<GameAction> {
        if timeout {
            self.timeouts.fetch_add(1, Ordering::Relaxed);
        }
        let in_a_row = self.consecutive_fails.fetch_add(1, Ordering::Relaxed) + 1;
        if in_a_row >= self.max_fails && !self.disabled.swap(true, Ordering::Relaxed) {
            println!(
                "LLM-SEAT-DISABLED after {in_a_row} consecutive failures ({} timeouts of {} questions): phase's own AI plays the rest",
                self.timeouts.load(Ordering::Relaxed),
                self.total.load(Ordering::Relaxed)
            );
        }
        None
    }

    fn decide(&self, state: &GameState, contract: &AiDecisionContract, actor: PlayerId) -> Option<GameAction> {
        if self.disabled.load(Ordering::Relaxed) {
            return None;
        }
        let seat = (actor.0 as usize).min(3);
        // A forced move (one legal option) is never put to a model: build_game_decision_prompt refuses it.
        let request = phase_llm::build_game_decision_prompt(
            state,
            contract,
            self.difficulty,
            self.database(),
            &Vec::new(),
        )
        .ok()?;
        if self.total.fetch_add(1, Ordering::Relaxed) >= self.max_calls {
            return None; // the call cap: every later decision is phase's AI
        }
        self.asked[seat].fetch_add(1, Ordering::Relaxed);
        self.prompt_chars.fetch_add(request.prompt.char_count() as u64, Ordering::Relaxed);
        let http = phase_llm::build_chat_request(&self.endpoint, &request.prompt).ok()?;
        let mut call = self.agent.request(http.method, &http.url);
        for header in &http.headers {
            call = call.set(&header.name, &header.value);
        }
        let started = Instant::now();
        let (status, body) = match call.send_string(&http.body) {
            Ok(response) => {
                let status = response.status();
                match response.into_string() {
                    Ok(text) => (status, text),
                    Err(_) => return self.failed(true),
                }
            }
            // A 4xx/5xx still carries a body phase-llm knows how to classify.
            Err(ureq::Error::Status(code, response)) => (code, response.into_string().unwrap_or_default()),
            Err(_) => return self.failed(true), // transport error: a timeout, or the endpoint is down
        };
        self.millis.fetch_add(started.elapsed().as_millis() as u64, Ordering::Relaxed);
        self.answered.fetch_add(1, Ordering::Relaxed);
        let selection = match phase_llm::select_action_from_response(
            state,
            contract,
            &request.fingerprint,
            LlmReply { provider: self.endpoint.provider, status, body: &body },
        ) {
            Ok(selection) => selection,
            Err(_) => return self.failed(false),
        };
        if !contract.contains_action(state, &selection.action) {
            return self.failed(false);
        }
        self.consecutive_fails.store(0, Ordering::Relaxed);
        self.used[seat].fetch_add(1, Ordering::Relaxed);
        Some(selection.action)
    }

    fn report(&self, seat: usize) {
        let asked = self.asked[seat].load(Ordering::Relaxed);
        if asked > 0 && asked % 25 == 0 {
            let answered = self.answered.load(Ordering::Relaxed).max(1);
            let questions = self.total.load(Ordering::Relaxed).max(1);
            println!(
                "LLM-SEAT seat={seat} asked={asked} used={} total_calls={} timeouts={} avg_ms={} avg_prompt_chars={}",
                self.used[seat].load(Ordering::Relaxed),
                self.total.load(Ordering::Relaxed),
                self.timeouts.load(Ordering::Relaxed),
                self.millis.load(Ordering::Relaxed) / answered,
                self.prompt_chars.load(Ordering::Relaxed) / questions
            );
        }
    }
}

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).ok().filter(|v| !v.trim().is_empty()).unwrap_or_else(|| default.to_string())
}

fn install_model_seats() {
    let seats: HashSet<u8> = env_or("PHASE_LLM_SEATS", "")
        .split(',')
        .filter_map(|s| s.trim().parse::<u8>().ok())
        .filter(|s| *s < 4)
        .collect();
    if seats.is_empty() {
        println!("LLM seats: none (plain ai-commander)");
        return;
    }
    let provider = LlmProvider::from_label(&env_or("PHASE_LLM_PROVIDER", "OpenAiCompatible"));
    let endpoint = LlmEndpointConfig {
        provider,
        base_url: std::env::var("PHASE_LLM_BASE_URL").ok().filter(|v| !v.trim().is_empty()),
        api_key: env_or("PHASE_LLM_API_KEY", ""),
        model: env_or("PHASE_LLM_MODEL", "model"),
        max_output_tokens: env_or("PHASE_LLM_MAX_TOKENS", "256").parse().ok(),
        temperature: env_or("PHASE_LLM_TEMPERATURE", "0.2").parse().ok(),
    };
    let timeout = env_or("PHASE_LLM_TIMEOUT_S", "20").parse::<u64>().unwrap_or(20);
    // The runner's first positional argument is the data folder (card-data.json lives there).
    let cards_path = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| ".".to_string()));
    let state = Seats {
        seats: seats.clone(),
        endpoint,
        difficulty: AiDifficulty::from_label(&env_or("PHASE_LLM_DIFFICULTY", "Medium")),
        agent: ureq::AgentBuilder::new().timeout(Duration::from_secs(timeout)).redirects(0).build(),
        cards_path,
        db: OnceLock::new(),
        max_calls: env_or("PHASE_LLM_MAX_CALLS", "300").parse().unwrap_or(300),
        asked: Default::default(),
        used: Default::default(),
        fell_back: Default::default(),
        total: AtomicU64::new(0),
        timeouts: AtomicU64::new(0),
        consecutive_fails: AtomicU64::new(0),
        max_fails: env_or("PHASE_LLM_MAX_FAILS", "4").parse().unwrap_or(4),
        disabled: AtomicBool::new(false),
        millis: AtomicU64::new(0),
        answered: AtomicU64::new(0),
        prompt_chars: AtomicU64::new(0),
    };
    println!(
        "LLM seats: {:?} provider={} model={} max_calls={}",
        {
            let mut v: Vec<u8> = seats.iter().copied().collect();
            v.sort();
            v
        },
        provider.label(),
        state.endpoint.model,
        state.max_calls
    );
    let installed = install_action_override(Box::new(move |game, contract, _owner, actor| {
        if !state.seats.contains(&actor.0) {
            return None;
        }
        let seat = (actor.0 as usize).min(3);
        let pick = state.decide(game, contract, actor);
        if pick.is_none() {
            state.fell_back[seat].fetch_add(1, Ordering::Relaxed);
        }
        state.report(seat);
        pick
    }));
    if !installed {
        eprintln!("LLM seats: an action override was already installed; model seats are off");
    }
}

fn main() {
    install_model_seats();
    commander::main();
}
