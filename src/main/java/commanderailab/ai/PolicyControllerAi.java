package commanderailab.ai;

import commanderailab.ml.MacroActionExecutor;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import forge.LobbyPlayer;
import forge.ai.AiController;
import forge.ai.ComputerUtilAbility;
import forge.ai.ComputerUtilMana;
import forge.ai.PlayerControllerAi;
import forge.game.Game;
import forge.game.card.Card;
import forge.game.card.CardCollection;
import forge.game.card.CardCollectionView;
import forge.game.phase.PhaseHandler;
import forge.game.phase.PhaseType;
import forge.game.player.Player;
import forge.game.spellability.SpellAbility;
import forge.game.spellability.SpellAbilityStackInstance;
import forge.game.zone.ZoneType;

import java.io.File;
import java.io.FileWriter;
import java.io.IOException;
import java.io.PrintWriter;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;

/**
 * PolicyControllerAi — Phase 2 live-game controller that routes AI decisions
 * through the Python policy server instead of Forge's built-in heuristics.
 *
 * <h2>Architecture</h2>
 * <pre>
 *   Forge game loop
 *       └─ player.getController()            → PolicyControllerAi
 *               └─ brains (PolicyAiController extends AiController)
 *                       └─ chooseSpellAbilityToPlay()
 *                               ├─ buildGameStateJson()
 *                               ├─ policyClient.decide(stateJson)   → macro-action
 *                               ├─ mapMacroActionToSpellAbility()   → SpellAbility
 *                               └─ super.chooseSpellAbilityToPlay() (fallback)
 * </pre>
 *
 * <h2>Two-class design</h2>
 * <ul>
 *   <li>{@link PolicyAiController} — inner subclass of {@link AiController}
 *       that overrides {@code chooseSpellAbilityToPlay()}.  This is the
 *       intercept point closest to the game loop.</li>
 *   <li>{@link PolicyControllerAi} — subclass of {@link PlayerControllerAi}
 *       that swaps Forge's default {@code AiController} for a
 *       {@code PolicyAiController} and exposes lifecycle helpers
 *       (game-end reward submission, statistics).</li>
 * </ul>
 *
 * <h2>Decision flow</h2>
 * <ol>
 *   <li>Build a rich {@link JsonObject} snapshot of the current game state
 *       (per-player life/mana/zones, stack, phase, legal actions).</li>
 *   <li>POST the snapshot to {@code /api/policy/decide} via
 *       {@link PolicyClient#decide(JsonObject)}.</li>
 *   <li>Translate the returned macro-action name (e.g. {@code "cast_creature"})
 *       to a concrete {@link SpellAbility} from the legal action list using
 *       {@link MacroActionExecutor}'s card-role databases.</li>
 *   <li>If the server is unreachable or no mapping succeeds, fall back to
 *       {@link AiController#chooseSpellAbilityToPlay()} transparently.</li>
 *   <li>Log every decision (policy or fallback) for training data collection.</li>
 * </ol>
 *
 * <h2>Fallback guarantee</h2>
 * Every code path in {@link PolicyAiController#chooseSpellAbilityToPlay()}
 * is wrapped in a broad {@code try/catch}.  Any unexpected exception causes
 * an immediate delegate to the parent Forge AI so the game continues safely.
 *
 * <h2>Usage</h2>
 * <pre>{@code
 * PolicyClient client = new PolicyClient("http://localhost:8080");
 * client.connect();
 *
 * // When Forge constructs the AI player's controller:
 * PolicyControllerAi controller = new PolicyControllerAi(game, player, lobbyPlayer, client);
 *
 * // After the game ends:
 * controller.onGameEnd(won ? 1.0 : -1.0);
 * }</pre>
 *
 * @see PolicyClient
 * @see PolicyAiController
 * @see MacroActionExecutor
 * @since Phase 2.1 — Issue #83
 */
public class PolicyControllerAi extends PlayerControllerAi {

    // ─────────────────────────────────────────────────────────────────────────
    // Constants
    // ─────────────────────────────────────────────────────────────────────────

    private static final Logger LOG = Logger.getLogger(PolicyControllerAi.class.getName());

    /**
     * Directory for training-data decision logs.
     * One JSON-lines file per game session is written here.
     */
    private static final String LOG_DIR = "data/decisions";

    // ─────────────────────────────────────────────────────────────────────────
    // Instance state
    // ─────────────────────────────────────────────────────────────────────────

    /** Unique identifier for this game session (used in reward submission). */
    private final String gameId = UUID.randomUUID().toString();

    /** The policy client used by the embedded {@link PolicyAiController}. */
    private final PolicyClient policyClient;

    /**
     * Log writer for training-data collection.
     * Lazily opened on the first decision; {@code null} if log dir is unavailable.
     */
    private PrintWriter decisionLog;

    /** Running count of decisions in this game (policy + fallback). */
    private final AtomicInteger decisionCount = new AtomicInteger(0);

    // ─────────────────────────────────────────────────────────────────────────
    // Construction
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Construct a policy-driven AI controller.
     *
     * <p>The standard Forge {@link AiController} created by the parent class is
     * immediately replaced by a {@link PolicyAiController} that holds a
     * reference to {@code client}.  All other Forge AI infrastructure
     * (combat, simulation, sideboard, etc.) is inherited unchanged.</p>
     *
     * @param game         the active Forge {@link Game}
     * @param player       the {@link Player} this controller drives
     * @param lobbyPlayer  the underlying lobby seat
     * @param client       live {@link PolicyClient}; must not be {@code null}
     */
    public PolicyControllerAi(Game game, Player player, LobbyPlayer lobbyPlayer,
                               PolicyClient client) {
        // Parent creates its own AiController; we replace it below via reflection.
        super(game, player, lobbyPlayer);
        this.policyClient = client;

        // Swap the brains field to our policy-aware subclass.
        injectPolicyAiController(game, player);
    }

    /**
     * Convenience constructor that builds a {@link PolicyClient} from a URL.
     *
     * <p>The client's {@link PolicyClient#connect()} is called automatically;
     * if the server is unavailable the controller will silently fall back to
     * Forge's built-in AI for all decisions.</p>
     *
     * @param game            the active Forge {@link Game}
     * @param player          the {@link Player} this controller drives
     * @param lobbyPlayer     the underlying lobby seat
     * @param policyServerUrl base URL of the policy server, e.g.
     *                        {@code "http://localhost:8080"}
     */
    public PolicyControllerAi(Game game, Player player, LobbyPlayer lobbyPlayer,
                               String policyServerUrl) {
        this(game, player, lobbyPlayer, buildAndConnect(policyServerUrl));
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Lifecycle helpers
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Signal the end of a game so the policy client can submit the terminal
     * reward and flush any buffered training tuples.
     *
     * <p>Call this from your game-runner once Forge reports a winner.</p>
     *
     * @param reward terminal reward signal: {@code +1.0} for a win,
     *               {@code -1.0} for a loss, or an intermediate value
     */
    public void onGameEnd(double reward) {
        try {
            policyClient.submitReward(gameId, reward);
            policyClient.flushCollectedTuples();
        } catch (Exception e) {
            LOG.warning("[PolicyControllerAi] onGameEnd error: " + e.getMessage());
        } finally {
            closeDecisionLog();
        }
        LOG.info(String.format(
            "[PolicyControllerAi] game=%s reward=%.2f decisions=%d stats=%s",
            gameId, reward, decisionCount.get(), policyClient.getStatsSummary()));
    }

    /**
     * Returns the unique game identifier used in reward submissions.
     *
     * @return UUID string
     */
    public String getGameId() {
        return gameId;
    }

    /**
     * Returns a snapshot of policy statistics for this game session.
     *
     * @return human-readable stats string from the underlying {@link PolicyClient}
     */
    public String getStatsSummary() {
        return policyClient.getStatsSummary();
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Private helpers
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Uses reflection to replace the {@code brains} field (an {@link AiController})
     * that the parent constructor already created with a new
     * {@link PolicyAiController}.
     *
     * <p>This is the only use of reflection in this class.  It is necessary
     * because {@code PlayerControllerAi} declares {@code brains} as
     * {@code private final} and constructs it in its own constructor body,
     * before we get a chance to supply a subclass.</p>
     *
     * <p>If reflection fails for any reason (e.g. a future Forge version adds
     * a security manager), the controller falls back to the original
     * {@code AiController} that the parent already created.  A warning is
     * logged so the issue is easy to diagnose.</p>
     *
     * @param game   the active game
     * @param player the AI player
     */
    private void injectPolicyAiController(Game game, Player player) {
        try {
            java.lang.reflect.Field brains =
                PlayerControllerAi.class.getDeclaredField("brains");
            brains.setAccessible(true);
            PolicyAiController policyBrains =
                new PolicyAiController(player, game, policyClient, this);
            brains.set(this, policyBrains);
            LOG.info("[PolicyControllerAi] PolicyAiController injected for player="
                + player.getName() + " game=" + gameId);
        } catch (Exception e) {
            LOG.warning("[PolicyControllerAi] Could not inject PolicyAiController; "
                + "falling back to vanilla Forge AI. Cause: " + e.getMessage());
        }
    }

    /** Build a {@link PolicyClient} and attempt to connect. */
    private static PolicyClient buildAndConnect(String serverUrl) {
        PolicyClient client = new PolicyClient(serverUrl);
        client.connect();
        return client;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Decision logging
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Write a single JSON-lines decision record to the per-game log file.
     * Creates the log directory and file on first call.
     *
     * @param record the JSON object to append
     */
    void logDecision(JsonObject record) {
        decisionCount.incrementAndGet();
        try {
            if (decisionLog == null) {
                File dir = new File(LOG_DIR);
                //noinspection ResultOfMethodCallIgnored
                dir.mkdirs();
                File logFile = new File(dir, "decisions-" + gameId + ".jsonl");
                decisionLog = new PrintWriter(new FileWriter(logFile, true));
            }
            decisionLog.println(record.toString());
            decisionLog.flush();
        } catch (IOException e) {
            // Non-fatal: the game continues even if we can't log.
            LOG.warning("[PolicyControllerAi] Could not write decision log: "
                + e.getMessage());
        }
    }

    /** Close the decision log file handle. */
    private void closeDecisionLog() {
        if (decisionLog != null) {
            decisionLog.close();
            decisionLog = null;
        }
    }

    // ═════════════════════════════════════════════════════════════════════════
    // Inner class: PolicyAiController
    // ═════════════════════════════════════════════════════════════════════════

    /**
     * PolicyAiController — intercepts {@link AiController#chooseSpellAbilityToPlay()}
     * and routes the decision through the Python policy server.
     *
     * <h2>Decision flow</h2>
     * <ol>
     *   <li>Enumerate all legal actions (lands + spells/abilities) using
     *       {@link ComputerUtilAbility} — the same utilities Forge uses
     *       internally — so the legal-action list is always authoritative.</li>
     *   <li>Build a {@link JsonObject} describing the full game state,
     *       including per-player life/mana/zones/stack/phase and a
     *       {@code legal_actions} array.</li>
     *   <li>POST to {@code /api/policy/decide} via {@link PolicyClient#decide(JsonObject)}.
     *       The server returns a macro-action name such as {@code "cast_creature"}.</li>
     *   <li>Translate the macro-action to a concrete {@link SpellAbility} by
     *       scanning the legal-action list with
     *       {@link #mapMacroActionToSpellAbility}.</li>
     *   <li>On any failure, delegate to the parent
     *       {@link AiController#chooseSpellAbilityToPlay()} so the game never stalls.</li>
     * </ol>
     *
     * <h2>Thread safety</h2>
     * All mutable state is confined to the single game thread that Forge uses
     * for AI decisions.  The {@link PolicyClient} methods called here are
     * themselves thread-safe.
     */
    static final class PolicyAiController extends AiController {

        /** Maximum age (ms) of a decision before it is considered stale. */
        private static final long DECISION_TIMEOUT_MS = 5_000;

        private final PolicyClient policyClient;
        private final PolicyControllerAi outer;

        PolicyAiController(Player player, Game game,
                           PolicyClient policyClient,
                           PolicyControllerAi outer) {
            super(player, game);
            this.policyClient = policyClient;
            this.outer = outer;
        }

        // ─── Main intercept ───────────────────────────────────────────────

        /**
         * Override of the core AI decision method.
         *
         * <p>This is called by the Forge game loop whenever the AI player has
         * priority and needs to decide whether/what to play.  We intercept
         * here to route the decision to the policy server.</p>
         *
         * @return list of {@link SpellAbility} objects for Forge to execute,
         *         or {@code null} / empty list to pass priority
         */
        @Override
        public List<SpellAbility> chooseSpellAbilityToPlay() {
            long t0 = System.currentTimeMillis();

            try {
                Player player = getPlayer();
                Game game = getGame();

                // 1. Enumerate legal actions
                List<SpellAbility> legalActions = gatherLegalActions(game, player);

                // If there are no legal actions, pass immediately — no server
                // round-trip required.
                if (legalActions.isEmpty()) {
                    logAndReturn(null, "pass", "no_legal_actions", t0, legalActions);
                    return super.chooseSpellAbilityToPlay();
                }

                // 2. Build game-state JSON with legal actions embedded
                JsonObject stateJson = buildGameStateJson(game, player, legalActions);

                // 3. Ask the policy server for a macro-action
                String macroAction = policyClient.decide(stateJson);

                // 4. Map macro-action → concrete SpellAbility
                SpellAbility chosen = mapMacroActionToSpellAbility(
                    macroAction, legalActions, player);

                if (chosen != null) {
                    // Policy succeeded
                    List<SpellAbility> result = Collections.singletonList(chosen);
                    logAndReturn(stateJson, macroAction, "policy", t0, result);
                    return result;
                }

                // macro-action mapped to "pass" or was unresolvable but not an error
                if (MacroActionExecutor.PASS.equals(macroAction)
                        || MacroActionExecutor.HOLD_MANA.equals(macroAction)) {
                    logAndReturn(stateJson, macroAction, "policy_pass", t0, null);
                    // Return super so Forge can decide whether to pass or play a land
                    return super.chooseSpellAbilityToPlay();
                }

                // Macro-action was not mappable to anything concrete; fall back.
                logAndReturn(stateJson, macroAction, "fallback_no_map", t0, null);
                return super.chooseSpellAbilityToPlay();

            } catch (Exception e) {
                // Broad catch to guarantee the game never stalls
                LOG.warning("[PolicyAiController] Unexpected error: " + e.getMessage());
                return super.chooseSpellAbilityToPlay();
            }
        }

        // ─── Legal action enumeration ─────────────────────────────────────

        /**
         * Gather all legal actions available to {@code player} in the current
         * game state, using Forge's own utility methods.
         *
         * <p>The result combines:</p>
         * <ul>
         *   <li>Land-play abilities (from hand or other sources Forge allows)</li>
         *   <li>All playable spell/activated abilities from hand and battlefield</li>
         * </ul>
         *
         * @param game   the active game
         * @param player the acting player
         * @return mutable list of legal {@link SpellAbility} objects; never {@code null}
         */
        private List<SpellAbility> gatherLegalActions(Game game, Player player) {
            List<SpellAbility> actions = new ArrayList<>();

            // Land plays — only available during main phases with empty stack
            CardCollection lands = ComputerUtilAbility.getAvailableLandsToPlay(game, player);
            if (lands != null) {
                for (Card land : lands) {
                    List<SpellAbility> landAbilities =
                        land.getAllPossibleAbilities(player, true);
                    landAbilities.removeIf(sa -> !sa.isLandAbility());
                    actions.addAll(landAbilities);
                }
            }

            // Spells and activated abilities from all available card sources
            CardCollection available = ComputerUtilAbility.getAvailableCards(game, player);
            if (available != null && !available.isEmpty()) {
                List<SpellAbility> spells =
                    ComputerUtilAbility.getSpellAbilities(available, player);
                actions.addAll(spells);
            }

            return actions;
        }

        // ─── Snapshot construction ────────────────────────────────────────

        /**
         * Build a comprehensive game-state {@link JsonObject} suitable for
         * POST to {@code /api/policy/decide}.
         *
         * <p>The JSON schema mirrors the Python-side state encoder:</p>
         * <pre>
         * {
         *   "game_id":       string,
         *   "turn":          int,
         *   "phase":         string,   // e.g. "MAIN1"
         *   "phase_is_main": bool,
         *   "stack_size":    int,
         *   "stack":         [ { "card": string, "controller": string }, … ],
         *   "active_seat":   int,
         *   "priority_seat": int,
         *   "players": [
         *     {
         *       "seat":               int,
         *       "name":               string,
         *       "life":               int,
         *       "mana_available":     int,
         *       "commander_damage":   int,
         *       "commander_casts":    int,
         *       "cards_in_hand":      int,
         *       "hand":               [ string, … ],
         *       "battlefield":        [ string, … ],
         *       "graveyard":          [ string, … ],
         *       "command_zone":       [ string, … ],
         *       "creatures_on_field": int,
         *       "land_count":         int,
         *       "total_power":        int,
         *       "total_toughness":    int
         *     }, …
         *   ],
         *   "legal_actions": [
         *     {
         *       "index":      int,
         *       "macro":      string,   // inferred macro-action
         *       "card_name":  string,
         *       "is_land":    bool,
         *       "mana_cost":  string
         *     }, …
         *   ]
         * }
         * </pre>
         *
         * @param game         the active game
         * @param player       the acting player (seat 0 in the players array)
         * @param legalActions the legal actions list produced by
         *                     {@link #gatherLegalActions}
         * @return the populated JSON object
         */
        private JsonObject buildGameStateJson(Game game, Player player,
                                              List<SpellAbility> legalActions) {
            JsonObject root = new JsonObject();
            root.addProperty("game_id", outer.gameId);

            // ── Phase / turn info ─────────────────────────────────────────
            PhaseHandler ph = game.getPhaseHandler();
            PhaseType phase = ph.getPhase();
            root.addProperty("turn", ph.getTurn());
            root.addProperty("phase", phase == null ? "UNKNOWN" : phase.toString());
            root.addProperty("phase_is_main",
                phase != null && phase.isMain());

            // ── Stack info ────────────────────────────────────────────────
            int stackSize = game.getStack().size();
            root.addProperty("stack_size", stackSize);
            JsonArray stackArr = new JsonArray();
            for (SpellAbilityStackInstance si : game.getStack()) {
                JsonObject se = new JsonObject();
                se.addProperty("card", si.getSourceCard() != null
                    ? si.getSourceCard().getName() : "unknown");
                se.addProperty("controller", si.getActivatingPlayer() != null
                    ? si.getActivatingPlayer().getName() : "unknown");
                stackArr.add(se);
            }
            root.add("stack", stackArr);

            // Seat of the current active (turn) player
            Player activePlayer = ph.getPlayerTurn();
            int activeSeat = seatIndex(game, activePlayer);
            int prioritySeat = seatIndex(game, player);
            root.addProperty("active_seat", activeSeat);
            root.addProperty("priority_seat", prioritySeat);

            // ── Per-player state ──────────────────────────────────────────
            JsonArray playersArr = new JsonArray();
            List<Player> allPlayers = new ArrayList<>(game.getPlayers());
            for (int seat = 0; seat < allPlayers.size(); seat++) {
                Player p = allPlayers.get(seat);
                playersArr.add(buildPlayerJson(seat, p, game));
            }
            root.add("players", playersArr);

            // ── Legal actions ─────────────────────────────────────────────
            JsonArray actionsArr = new JsonArray();
            for (int i = 0; i < legalActions.size(); i++) {
                SpellAbility sa = legalActions.get(i);
                JsonObject ae = new JsonObject();
                ae.addProperty("index", i);
                ae.addProperty("card_name", sa.getHostCard() != null
                    ? sa.getHostCard().getName() : "unknown");
                ae.addProperty("is_land", sa.isLandAbility());
                ae.addProperty("mana_cost", sa.getPayCosts() != null
                    ? sa.getPayCosts().getTotalMana().toString() : "");
                ae.addProperty("macro", inferMacro(sa));
                actionsArr.add(ae);
            }
            root.add("legal_actions", actionsArr);

            return root;
        }

        /**
         * Serialize a single player's observable state into a {@link JsonObject}.
         *
         * @param seat   0-based seat index in {@code game.getPlayers()}
         * @param p      the player to serialize
         * @param game   the active game (for stack/zone queries)
         * @return populated player JSON
         */
        private JsonObject buildPlayerJson(int seat, Player p, Game game) {
            JsonObject pj = new JsonObject();
            pj.addProperty("seat", seat);
            pj.addProperty("name", p.getName());
            pj.addProperty("life", p.getLife());
            pj.addProperty("mana_available",
                ComputerUtilMana.getAvailableManaEstimate(p));

            // Commander damage (from any commander source)
            int cdrDamage = 0;
            for (Map.Entry<Card, Integer> entry : p.getCommanderDamage()) {
                cdrDamage += entry.getValue();
            }
            pj.addProperty("commander_damage", cdrDamage);

            // Commander casts (aggregate across all commanders)
            int cdrCasts = 0;
            for (Card cdr : p.getCardsIn(ZoneType.Command)) {
                cdrCasts += p.getCommanderCast(cdr);
            }
            pj.addProperty("commander_casts", cdrCasts);

            // Zone lists
            CardCollectionView hand = p.getCardsIn(ZoneType.Hand);
            pj.addProperty("cards_in_hand", hand.size());
            pj.add("hand", cardNamesArray(hand));
            pj.add("battlefield", cardNamesArray(p.getCardsIn(ZoneType.Battlefield)));
            pj.add("graveyard", cardNamesArray(p.getCardsIn(ZoneType.Graveyard)));
            pj.add("command_zone", cardNamesArray(p.getCardsIn(ZoneType.Command)));

            // Board summary stats
            CardCollectionView bf = p.getCardsIn(ZoneType.Battlefield);
            int creatures = 0, lands = 0, power = 0, toughness = 0;
            for (Card c : bf) {
                if (c.isCreature()) {
                    creatures++;
                    power += c.getNetPower();
                    toughness += c.getNetToughness();
                }
                if (c.isLand()) lands++;
            }
            pj.addProperty("creatures_on_field", creatures);
            pj.addProperty("land_count", lands);
            pj.addProperty("total_power", power);
            pj.addProperty("total_toughness", toughness);

            return pj;
        }

        // ─── Macro-action mapping ─────────────────────────────────────────

        /**
         * Map a policy macro-action string back to a concrete {@link SpellAbility}
         * from the legal-action list.
         *
         * <p>The algorithm scans the legal actions list and picks the first
         * {@link SpellAbility} whose source card belongs to the category implied
         * by the macro-action, using the same card-role databases that
         * {@link MacroActionExecutor} uses.  This keeps Java and Python in sync.</p>
         *
         * <p>For {@code CAST_COMMANDER}, the command zone is checked first.</p>
         *
         * @param macroAction  the macro-action returned by the policy server
         * @param legalActions all legal actions in the current state
         * @param player       the acting player (for commander zone check)
         * @return the best matching {@link SpellAbility}, or {@code null} if
         *         no match is found or the macro-action is pass/hold
         */
        private SpellAbility mapMacroActionToSpellAbility(
                String macroAction,
                List<SpellAbility> legalActions,
                Player player) {

            switch (macroAction) {
                // ── Lands ────────────────────────────────────────────────
                case MacroActionExecutor.CAST_RAMP: {
                    // Prefer ramp spells; if none, fall through to a land play
                    SpellAbility ramp = findFirstMatching(legalActions, sa ->
                        !sa.isLandAbility()
                        && sa.getHostCard() != null
                        && MacroActionExecutor.isRampCard(sa.getHostCard().getName()));
                    if (ramp != null) return ramp;
                    // Land drop counts as ramp when no ramp spell is available
                    return findFirstMatching(legalActions, SpellAbility::isLandAbility);
                }

                // ── Creatures ────────────────────────────────────────────
                case MacroActionExecutor.CAST_CREATURE: {
                    return findFirstMatching(legalActions, sa ->
                        !sa.isLandAbility()
                        && sa.getHostCard() != null
                        && sa.getHostCard().isCreature()
                        && !MacroActionExecutor.isRemovalCard(sa.getHostCard().getName())
                        && !MacroActionExecutor.isDrawCard(sa.getHostCard().getName())
                        && !MacroActionExecutor.isRampCard(sa.getHostCard().getName()));
                }

                // ── Removal / interaction ─────────────────────────────────
                case MacroActionExecutor.CAST_REMOVAL: {
                    return findFirstMatching(legalActions, sa ->
                        !sa.isLandAbility()
                        && sa.getHostCard() != null
                        && MacroActionExecutor.isRemovalCard(sa.getHostCard().getName()));
                }

                // ── Card draw ────────────────────────────────────────────
                case MacroActionExecutor.CAST_DRAW: {
                    return findFirstMatching(legalActions, sa ->
                        !sa.isLandAbility()
                        && sa.getHostCard() != null
                        && MacroActionExecutor.isDrawCard(sa.getHostCard().getName()));
                }

                // ── Commander ────────────────────────────────────────────
                case MacroActionExecutor.CAST_COMMANDER: {
                    // Commander spells appear as regular spells in legalActions
                    // but their source card is in the command zone.
                    return findFirstMatching(legalActions, sa ->
                        !sa.isLandAbility()
                        && sa.getHostCard() != null
                        && sa.getHostCard().isCommander()
                        && player.getCardsIn(ZoneType.Command)
                                 .contains(sa.getHostCard()));
                }

                // ── Pass / hold ──────────────────────────────────────────
                case MacroActionExecutor.PASS:
                case MacroActionExecutor.HOLD_MANA:
                case MacroActionExecutor.ATTACK_OPPONENT:
                    // Attacks are declared in a different decision point;
                    // returning null here passes priority gracefully.
                    return null;

                default:
                    LOG.warning("[PolicyAiController] Unknown macro-action: " + macroAction);
                    return null;
            }
        }

        // ─── Infer macro from SA (for the legal_actions array) ───────────

        /**
         * Infer which macro-action category a {@link SpellAbility} belongs to.
         * Used when populating the {@code legal_actions[].macro} field in the
         * state JSON so the policy server can cross-check.
         *
         * @param sa the spell ability to classify
         * @return macro-action name constant from {@link MacroActionExecutor}
         */
        private String inferMacro(SpellAbility sa) {
            if (sa.isLandAbility()) return MacroActionExecutor.CAST_RAMP;
            Card card = sa.getHostCard();
            if (card == null) return MacroActionExecutor.PASS;
            String name = card.getName();
            if (MacroActionExecutor.isRemovalCard(name)) return MacroActionExecutor.CAST_REMOVAL;
            if (MacroActionExecutor.isDrawCard(name))    return MacroActionExecutor.CAST_DRAW;
            if (MacroActionExecutor.isRampCard(name))    return MacroActionExecutor.CAST_RAMP;
            if (card.isCommander())                      return MacroActionExecutor.CAST_COMMANDER;
            if (card.isCreature())                       return MacroActionExecutor.CAST_CREATURE;
            return MacroActionExecutor.PASS;
        }

        // ─── Utility helpers ──────────────────────────────────────────────

        /**
         * Return the first element of {@code actions} that matches
         * {@code predicate}, or {@code null} if none match.
         *
         * @param actions   the list to scan
         * @param predicate the filter predicate
         * @return first match or {@code null}
         */
        private SpellAbility findFirstMatching(
                List<SpellAbility> actions,
                java.util.function.Predicate<SpellAbility> predicate) {
            for (SpellAbility sa : actions) {
                if (predicate.test(sa)) return sa;
            }
            return null;
        }

        /**
         * Return the 0-based seat index of {@code target} in
         * {@code game.getPlayers()}, or {@code -1} if not found.
         *
         * @param game   the active game
         * @param target the player to look up
         * @return seat index
         */
        private int seatIndex(Game game, Player target) {
            if (target == null) return -1;
            List<Player> players = new ArrayList<>(game.getPlayers());
            for (int i = 0; i < players.size(); i++) {
                if (players.get(i).equals(target)) return i;
            }
            return -1;
        }

        /**
         * Build a {@link JsonArray} of card names from a zone view,
         * suitable for embedding in the state JSON.
         *
         * @param cards the zone contents
         * @return JSON array of name strings
         */
        private JsonArray cardNamesArray(CardCollectionView cards) {
            JsonArray arr = new JsonArray();
            if (cards != null) {
                for (Card c : cards) {
                    arr.add(c.getName());
                }
            }
            return arr;
        }

        /**
         * Write a training-data record and emit a log line for every decision,
         * whether it was routed through the policy server or fell back to Forge.
         *
         * @param stateJson  full game-state JSON, or {@code null} for early exits
         * @param macro      the macro-action chosen
         * @param source     decision source tag: {@code "policy"}, {@code "fallback_*"}, etc.
         * @param t0         timestamp (ms) when this decision cycle started
         * @param result     the final list of abilities returned (may be {@code null})
         */
        private void logAndReturn(JsonObject stateJson, String macro,
                                  String source, long t0,
                                  List<SpellAbility> result) {
            long elapsed = System.currentTimeMillis() - t0;

            // Build a compact log record for training data
            JsonObject record = new JsonObject();
            record.addProperty("game_id", outer.gameId);
            record.addProperty("ts", Instant.now().toString());
            record.addProperty("macro", macro);
            record.addProperty("source", source);
            record.addProperty("latency_ms", elapsed);
            if (result != null && !result.isEmpty()) {
                SpellAbility sa = result.get(0);
                record.addProperty("card",
                    sa.getHostCard() != null ? sa.getHostCard().getName() : "land");
            }
            if (stateJson != null && stateJson.has("turn")) {
                record.addProperty("turn", stateJson.get("turn").getAsInt());
                record.addProperty("phase", stateJson.get("phase").getAsString());
            }

            outer.logDecision(record);

            LOG.fine(String.format(
                "[PolicyAiController] %s → %s (source=%s, %dms)",
                macro,
                result != null && !result.isEmpty() && result.get(0).getHostCard() != null
                    ? result.get(0).getHostCard().getName() : "pass",
                source, elapsed));
        }
    }
    // ── end PolicyAiController ─────────────────────────────────────────────
}
