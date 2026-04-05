#!/usr/bin/env python3
"""
Phase 1 patch: no-op mana pool display functions in lan-client/ui.js
Run from anywhere — edit UI_JS_PATH to point at your file.
"""

import re

UI_JS_PATH = r"D:\ForgeCommander\commander-ai-lab\lan-client\ui.js"

with open(UI_JS_PATH, "r", encoding="utf-8") as f:
    content = f.read()

original_size = len(content)
changes = []

# ── Change 1: no-op renderManaPool ───────────────────────────────────────────
old1 = re.search(
    r'function renderManaPool\(playerIdx\) \{[^}]*?display\.style\.display[^}]*?\}',
    content, re.DOTALL
)
if old1:
    content = content[:old1.start()] + \
        "function renderManaPool(playerIdx) {\n  // mana pool display removed (Phase 1)\n}" + \
        content[old1.end():]
    changes.append("renderManaPool -> stub")
else:
    print("WARNING: renderManaPool pattern not matched — skipping")

# ── Change 2: no-op showManaPool ─────────────────────────────────────────────
old2 = re.search(
    r'function showManaPool\(\) \{.*?\}',
    content, re.DOTALL
)
if old2:
    content = content[:old2.start()] + \
        "function showManaPool() {\n  // mana pool display removed (Phase 1)\n}" + \
        content[old2.end():]
    changes.append("showManaPool -> stub")
else:
    print("WARNING: showManaPool pattern not matched — skipping")

# ── Change 3: remove Mana Auto-Clear row from renderSettingsPanel ─────────────
mana_row = (
    " +\n    '<div class=\"settings-row\"><span>Mana Auto-Clear</span>'"
    " + renderToggle('manaAutoClear', gameState.manaAutoClear) + '</div>';"
)
if mana_row in content:
    content = content.replace(mana_row, ";", 1)
    changes.append("Mana Auto-Clear row removed from renderSettingsPanel")
else:
    print("WARNING: Mana Auto-Clear row not found — skipping")

# ── Write ─────────────────────────────────────────────────────────────────────
if changes:
    with open(UI_JS_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Done. {original_size} -> {len(content)} chars ({len(content)-original_size:+d})")
    for c in changes:
        print(f"  ✓ {c}")
else:
    print("No changes applied.")
