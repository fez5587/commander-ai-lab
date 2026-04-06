import re

with open('ml/eval/forge_evaluator.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix 1: >= 2 to >= 3
content = content.replace('deck_files) >= 2', 'deck_files) >= 3')

# Fix 2: replace call to _run_forge_via_subprocess with _parse_batch_json
content = content.replace(
    'return self._run_forge_via_subprocess(service)',
    'return self._parse_batch_json(service)'
)

# Fix 3: Insert _parse_batch_json method before _run_forge_via_subprocess
new_method = '''
    def _parse_batch_json(self, service) -> List[GameRecord]:
        """Parse the batch JSON output from LabCli Java subprocess."""
        batch_path = os.path.join(
            self.cfg.results_dir, f"eval-{self.cfg.run_id}-batch.json"
        )
        if not Path(batch_path).exists():
            logger.warning("[Eval] Batch JSON not found at %s -- falling back to synthetic", batch_path)
            return self._run_synthetic_eval(service)
        with open(batch_path, encoding='utf-8') as f:
            batch = json.load(f)
        games_list = batch.get("games", [])
        if not games_list:
            logger.warning("[Eval] No games in batch JSON -- falling back to synthetic")
            return self._run_synthetic_eval(service)
        records = []
        for idx, g in enumerate(games_list):
            won = g.get("winningSeat", -1) == 0
            turns = g.get("totalTurns", 0)
            pr = g.get("playerResults", [])
            lp = pr[0].get("finalLife", 40) if len(pr) > 0 else 40
            lo = pr[1].get("finalLife", 40) if len(pr) > 1 else 40
            records.append(GameRecord(
                game_index=idx,
                won=won,
                turns=turns,
                life_final_policy=lp,
                life_final_opponent=lo,
                life_delta=lp - lo,
                commander_damage_dealt=0,
                illegal_actions=0,
                entropy_mean=0.0,
                inference_ms_mean=0.0,
                decision_steps=turns,
            ))
            if len(records) >= self.cfg.num_games:
                break
        logger.info("[Eval %s] Parsed %d games from batch JSON", self.cfg.run_id, len(records))
        return records

'''

content = content.replace(
    '    def _run_forge_via_subprocess(',
    new_method + '    def _run_forge_via_subprocess('
)

with open('ml/eval/forge_evaluator.py', 'w', encoding='utf-8') as f:
    f.write(content)

import py_compile
py_compile.compile('ml/eval/forge_evaluator.py', doraise=True)
print('Syntax OK - patch applied')
