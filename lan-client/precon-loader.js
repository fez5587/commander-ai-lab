/**
 * precon-loader.js
 * Handles loading precon .dck files from the backend into the lan-client simulator.
 * Depends on: engine.js (gameState, initGame, addLogEntry, renderPlayerPanels)
 *             ai-bridge.js (AIBridge.deckSource.fetchPreconDeck)
 *             ui.js (setupDeckChoices, setupPlayerCount, setupStartingLife)
 */

'use strict';

// Scryfall collection endpoint — accepts up to 75 card identifiers per call
const SCRYFALL_COLLECTION_URL = 'https://api.scryfall.com/cards/collection';

// ────────────────────────────────────────────────────────────
// Scryfall image cache: cardName -> { imageUrl, typeLine, ... }
// Persisted to localStorage so repeat games don't re-hit Scryfall.
// ────────────────────────────────────────────────────────────
const _SCRYFALL_CACHE_KEY = 'commanderLab.scryfallCache.v1';
const _SCRYFALL_CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000;  // 30 days
const _SCRYFALL_CACHE_MAX_ENTRIES = 20000;

let _scryfallCache = {};
(function _loadScryfallCacheFromLocalStorage() {
  try {
    const raw = localStorage.getItem(_SCRYFALL_CACHE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return;
    const now = Date.now();
    const stamp = parsed.__stamp || 0;
    if (now - stamp > _SCRYFALL_CACHE_TTL_MS) {
      console.log('[scryfall-cache] expired (> 30 days), clearing');
      localStorage.removeItem(_SCRYFALL_CACHE_KEY);
      return;
    }
    delete parsed.__stamp;
    _scryfallCache = parsed;
    const n = Object.keys(_scryfallCache).length;
    console.log('[scryfall-cache] loaded ' + n + ' entries from localStorage');
  } catch (e) {
    console.warn('[scryfall-cache] load failed:', e);
    try { localStorage.removeItem(_SCRYFALL_CACHE_KEY); } catch (_) {}
    _scryfallCache = {};
  }
})();

let _scryfallCacheSaveTimer = null;
function _persistScryfallCache() {
  // Debounced — coalesce rapid writes during batch enrichment
  if (_scryfallCacheSaveTimer) return;
  _scryfallCacheSaveTimer = setTimeout(function () {
    _scryfallCacheSaveTimer = null;
    try {
      // Trim to MAX_ENTRIES if over (drop entries with no imageUrl first)
      const keys = Object.keys(_scryfallCache);
      if (keys.length > _SCRYFALL_CACHE_MAX_ENTRIES) {
        const trimmed = {};
        const sorted = keys.sort((a, b) =>
          (_scryfallCache[b].imageUrl ? 1 : 0) - (_scryfallCache[a].imageUrl ? 1 : 0)
        ).slice(0, _SCRYFALL_CACHE_MAX_ENTRIES);
        for (const k of sorted) trimmed[k] = _scryfallCache[k];
        _scryfallCache = trimmed;
      }
      const payload = Object.assign({ __stamp: Date.now() }, _scryfallCache);
      localStorage.setItem(_SCRYFALL_CACHE_KEY, JSON.stringify(payload));
    } catch (e) {
      // Quota exceeded — clear and skip. Cache will rebuild next session.
      console.warn('[scryfall-cache] persist failed (quota?):', e);
      try { localStorage.removeItem(_SCRYFALL_CACHE_KEY); } catch (_) {}
    }
  }, 750);
}

function clearScryfallCache() {
  _scryfallCache = {};
  try { localStorage.removeItem(_SCRYFALL_CACHE_KEY); } catch (_) {}
  console.log('[scryfall-cache] cleared');
}

// ============================================================
// startGameWithDecks
// Called by ui.js startGame() when players have selected precon decks.
// ============================================================
async function startGameWithDecks() {
  const playerNames = [];
  for (let i = 0; i < (setupPlayerCount || 4); i++) {
    const inp = document.getElementById('player-name-' + i);
    playerNames.push(inp && inp.value.trim() ? inp.value.trim() : 'Player ' + (i + 1));
  }

  initGame(setupPlayerCount || 4, playerNames, setupStartingLife || 40);

  const choices = typeof setupDeckChoices !== 'undefined' ? setupDeckChoices : {};
  const loadPromises = Object.entries(choices).map(async ([idxStr, choice]) => {
    const playerIdx = parseInt(idxStr, 10);
    if (choice.type !== 'precon' || !choice.preconId) return;
    try {
      const fileName = choice.preconId.endsWith('.dck') ? choice.preconId : choice.preconId + '.dck';
      const data = await AIBridge.deckSource.fetchPreconDeck(fileName);
      if (!data || !data.decklist) {
        addLogEntry('\u26A0 Could not load deck for Player ' + (playerIdx + 1) + ' (no decklist returned)');
        return;
      }
      loadPreconDeckFromDck(playerIdx, data.decklist, choice.preconId);
    } catch (e) {
      console.warn('[precon-loader] Failed to load deck for player', playerIdx, e);
      addLogEntry('\u26A0 Deck load failed for Player ' + (playerIdx + 1) + ': ' + e.message);
    }
  });

  await Promise.all(loadPromises);

  if (typeof renderPlayerPanels === 'function') renderPlayerPanels();
  if (typeof updateActivePlayerHighlight === 'function') updateActivePlayerHighlight();

  // Block on Scryfall enrichment BEFORE the game becomes interactive. This is
  // critical: the opening 7 cards are dealt via player.zones.hand.push(lib.shift())
  // directly (bypassing drawCard()), so they don't get _enrichCardFromCache'd at
  // draw time. Without awaiting here, the AI's first turn fires with empty
  // typeLine/cmc/oracleText/isRamp/isRemoval flags and can only play lands.
  addLogEntry('\u23F3 Loading card data from Scryfall...');
  try {
    await enrichAllZonesWithScryfall();
  } catch (e) {
    console.warn('[precon-loader] Enrichment failed, continuing anyway:', e);
  }
  addLogEntry('\u2705 Card data loaded. Game ready.');
}


// ============================================================
// loadPreconDeckFromDck
// Parses a Forge-format .dck file string and populates a player's zones.
// ============================================================
function loadPreconDeckFromDck(playerIdx, dckContent, deckLabel) {
  if (!gameState) return;
  const player = gameState.players[playerIdx];
  if (!player) return;

  let commander = '';
  let section = '';
  const mainCards = [];

  for (const rawLine of dckContent.split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('//')) continue;

    if (line.startsWith('[') && line.endsWith(']')) {
      section = line.slice(1, -1);
      continue;
    }
    if (line.includes('=') && !line.match(/^\d/)) continue;

    const m = line.match(/^(\d+)\s+(.+?)(?:\|.*)?$/);
    if (!m) continue;

    const qty = parseInt(m[1], 10);
    const name = m[2].trim();

    if (section === 'Commander') {
      commander = name;
    } else if (section === 'Main') {
      for (let q = 0; q < qty; q++) mainCards.push(name);
    }
  }

  player.zones.command   = [];
  player.zones.library   = [];
  player.zones.hand      = [];
  player.zones.graveyard = [];
  player.zones.exile     = [];

  if (commander) {
    player.zones.command.push({
      id: 'cmd-' + playerIdx + '-' + Date.now(),
      name: commander,
      imageUrl: '',
      typeLine: 'Legendary Creature',
      oracleText: '',
      manaCost: '',
    });
    player.commanderCardName = commander;
  }

  for (const cardName of mainCards) {
    player.zones.library.push({
      id: 'lib-' + playerIdx + '-' + cardName + '-' + Math.random().toString(36).slice(2, 6),
      name: cardName,
      imageUrl: '',
      typeLine: '',
      oracleText: '',
      manaCost: '',
    });
  }

  // Fisher-Yates shuffle
  const lib = player.zones.library;
  for (let i = lib.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [lib[i], lib[j]] = [lib[j], lib[i]];
  }

  // Draw opening hand
  for (let d = 0; d < 7 && lib.length > 0; d++) {
    player.zones.hand.push(lib.shift());
  }

  if (typeof updateZoneCountsForPlayer === 'function') updateZoneCountsForPlayer(playerIdx);
  if (typeof renderCommanderZone === 'function') renderCommanderZone(playerIdx);
  if (typeof renderHandZone === 'function') renderHandZone(playerIdx);

  const label = deckLabel ? deckLabel.replace('.dck', '') : 'Unknown Deck';
  addLogEntry(
    '\uD83C\uDCCF <strong>' + player.name + '</strong> loaded: <em>' + label + '</em>' +
    (commander ? ' \u2014 Commander: <strong>' + commander + '</strong>' : '') +
    ' (' + lib.length + ' in library, 7 in hand)'
  );
}


// ============================================================
// SCRYFALL IMAGE ENRICHMENT
// After zones are populated with card names, batch-fetch card
// data from Scryfall and backfill imageUrl on every card object.
// Uses the /cards/collection endpoint (75 cards per request).
// Results are cached in _scryfallCache so re-renders are instant.
// ============================================================

async function enrichAllZonesWithScryfall() {
  if (!gameState) return;

  // Gather all unique card names across all players and all zones
  const nameSet = new Set();
  let totalCards = 0;
  let cacheHits = 0;
  for (const player of gameState.players) {
    for (const zone of Object.values(player.zones)) {
      for (const card of zone) {
        if (!card.name) continue;
        totalCards++;
        if (_scryfallCache[card.name]) {
          cacheHits++;
        } else {
          nameSet.add(card.name);
        }
      }
    }
  }

  if (nameSet.size === 0) {
    // Everything was in cache — just apply and bail, no network
    addLogEntry('\u2705 Card images loaded from cache (' + cacheHits + ' cards).');
    _applyScryfallDataToAllCards();
    return;
  }

  const names = Array.from(nameSet);
  const hitRatio = totalCards > 0 ? Math.round((cacheHits / totalCards) * 100) : 0;
  addLogEntry('\uD83C\uDF0D Fetching ' + names.length + ' new cards from Scryfall (' + hitRatio + '% cache hit rate)...');

  // Split into batches of 75 (Scryfall max)
  const BATCH = 75;
  for (let i = 0; i < names.length; i += BATCH) {
    const batch = names.slice(i, i + BATCH);
    try {
      await _fetchScryfallBatch(batch);
    } catch (e) {
      console.warn('[precon-loader] Scryfall batch failed', e);
    }
    // Polite delay between batches to respect Scryfall rate limit (50-100ms)
    if (i + BATCH < names.length) await _sleep(100);
  }

  // Apply cached data to every card object in every zone
  _applyScryfallDataToAllCards();

  addLogEntry('\u2705 Card images loaded.');
}

async function _fetchScryfallBatch(names) {
  const identifiers = names.map(name => ({ name }));
  const resp = await fetch(SCRYFALL_COLLECTION_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifiers }),
  });
  if (!resp.ok) throw new Error('Scryfall HTTP ' + resp.status);
  const json = await resp.json();

  for (const card of (json.data || [])) {
    const name = card.name;
    // Pick the best image: normal > large > small, handle double-faced
    let imageUrl = '';
    if (card.image_uris) {
      imageUrl = card.image_uris.normal || card.image_uris.large || card.image_uris.small || '';
    } else if (card.card_faces && card.card_faces[0] && card.card_faces[0].image_uris) {
      imageUrl = card.card_faces[0].image_uris.normal || card.card_faces[0].image_uris.large || '';
    }
    _scryfallCache[name] = {
      imageUrl,
      typeLine:   card.type_line   || '',
      oracleText: card.oracle_text || (card.card_faces && card.card_faces[0] ? card.card_faces[0].oracle_text : '') || '',
      manaCost:   card.mana_cost   || (card.card_faces && card.card_faces[0] ? card.card_faces[0].mana_cost : '') || '',
      cmc:        card.cmc         || 0,
      power:      card.power       || '',
      toughness:  card.toughness   || '',
    };
  }

  // Mark not-found cards so we don't re-fetch them on next enrichment
  for (const name of names) {
    if (!_scryfallCache[name]) _scryfallCache[name] = { imageUrl: '', typeLine: '', oracleText: '', manaCost: '', cmc: 0, power: '', toughness: '' };
  }

  _persistScryfallCache();
}

function _applyScryfallDataToAllCards() {
  if (!gameState) return;
  for (const player of gameState.players) {
    for (const zone of Object.values(player.zones)) {
      for (const card of zone) {
        const data = _scryfallCache[card.name];
        if (data) {
          card.imageUrl   = data.imageUrl   || card.imageUrl;
          card.typeLine   = data.typeLine   || card.typeLine;
          card.oracleText = data.oracleText || card.oracleText;
          card.manaCost   = data.manaCost   || card.manaCost;
          card.cmc        = data.cmc        || card.cmc || 0;
          if (data.power)     card.power     = data.power;
          if (data.toughness) card.toughness = data.toughness;

          // Tag roles from oracle text so aiCategorizeHand works
          const ot = (card.oracleText || '').toLowerCase();
          if (!card.isRamp)      card.isRamp      = /search your library for a.*land|add \{|adds? one mana/i.test(ot);
          if (!card.isRemoval)   card.isRemoval   = /destroy target|exile target|counter target spell/i.test(ot);
          if (!card.isBoardWipe) card.isBoardWipe = /destroy all|exile all|all creatures get -/i.test(ot);
        }
      }
    }
    // Also enrich battlefield cards owned by this player
    if (gameState.battlefieldCards) {
      for (const card of gameState.battlefieldCards) {
        if (card.ownerIndex !== player.id) continue;
        const data = _scryfallCache[card.name];
        if (data) {
          if (!card.imageUrl) card.imageUrl = data.imageUrl || '';
          card.typeLine   = data.typeLine   || card.typeLine;
          card.oracleText = data.oracleText || card.oracleText;
          card.manaCost   = data.manaCost   || card.manaCost;
          card.cmc        = data.cmc        || card.cmc || 0;

          const ot = (card.oracleText || '').toLowerCase();
          if (!card.isRamp)      card.isRamp      = /search your library for a.*land|add \{|adds? one mana/i.test(ot);
          if (!card.isRemoval)   card.isRemoval   = /destroy target|exile target|counter target spell/i.test(ot);
          if (!card.isBoardWipe) card.isBoardWipe = /destroy all|exile all|all creatures get -/i.test(ot);
        }
      }
    }
  }

  // Trigger UI refresh so images appear without a full reload
  if (typeof renderPlayerPanels === 'function') renderPlayerPanels();
  if (typeof renderCommanderZone === 'function') {
    for (let i = 0; i < gameState.players.length; i++) renderCommanderZone(i);
  }
  if (typeof renderHandZone === 'function') {
    for (let i = 0; i < gameState.players.length; i++) renderHandZone(i);
  }
  if (typeof updateZoneCounts === 'function') updateZoneCounts();
}

/**
 * Synchronously enrich a single card from the Scryfall cache.
 * Called at draw time so cards have typeLine/cmc/oracle data
 * before the AI's first turn, even if async enrichment hasn't completed.
 */
function _enrichCardFromCache(card) {
  if (!card || !card.name) return;
  const data = _scryfallCache[card.name];
  if (!data) return;
  card.imageUrl   = data.imageUrl   || card.imageUrl   || '';
  card.typeLine   = data.typeLine   || card.typeLine   || '';
  card.oracleText = data.oracleText || card.oracleText || '';
  card.manaCost   = data.manaCost   || card.manaCost   || '';
  card.cmc        = data.cmc        || card.cmc        || 0;
  if (data.power)     card.power     = data.power;
  if (data.toughness) card.toughness = data.toughness;

  // Tag oracle roles
  const ot = (card.oracleText || '').toLowerCase();
  if (!card.isRamp)      card.isRamp      = /search your library for a.*land|add \{|adds? one mana/i.test(ot);
  if (!card.isRemoval)   card.isRemoval   = /destroy target|exile target|counter target spell/i.test(ot);
  if (!card.isBoardWipe) card.isBoardWipe = /destroy all|exile all|all creatures get -/i.test(ot);
}

/**
 * Enrich a single card object on-demand (useful for cards drawn or played after initial load).
 * Returns a Promise that resolves when the card's imageUrl is set.
 */
async function enrichCardWithScryfall(card) {
  if (!card || !card.name) return;
  if (card.imageUrl) return; // already has image

  const cached = _scryfallCache[card.name];
  if (cached) {
    card.imageUrl   = cached.imageUrl   || '';
    card.typeLine   = cached.typeLine   || card.typeLine;
    card.oracleText = cached.oracleText || card.oracleText;
    card.manaCost   = cached.manaCost   || card.manaCost;
    return;
  }

  await _fetchScryfallBatch([card.name]);
  const data = _scryfallCache[card.name];
  if (data) {
    card.imageUrl   = data.imageUrl   || '';
    card.typeLine   = data.typeLine   || card.typeLine;
    card.oracleText = data.oracleText || card.oracleText;
    card.manaCost   = data.manaCost   || card.manaCost;
  }
}

function _sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
