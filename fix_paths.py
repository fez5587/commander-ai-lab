with open('ml/eval/forge_evaluator.py', 'rb') as f:
    data = f.read()

# Fix 1: absolute JAR path in subprocess call
data = data.replace(
    b'"--forge-jar", self.cfg.forge_jar,',
    b'"--forge-jar", str(Path(self.cfg.forge_jar).resolve()),'
)

# Fix 2: resolve forge_work_dir to absolute path too
data = data.replace(
    b'"--forge-dir", self.cfg.forge_work_dir,',
    b'"--forge-dir", str(Path(self.cfg.forge_work_dir).resolve()),'
)

# Fix 3: change default forge_work_dir
data = data.replace(
    b'forge_work_dir: str = "forge"',
    b'forge_work_dir: str = "D:/ForgeCommander/forge-repo/forge-gui"'
)

with open('ml/eval/forge_evaluator.py', 'wb') as f:
    f.write(data)

import py_compile
py_compile.compile('ml/eval/forge_evaluator.py', doraise=True)
print('forge_evaluator.py - Syntax OK')

# Fix eval_policy.py too
with open('scripts/eval_policy.py', 'rb') as f:
    data = f.read()

data = data.replace(
    b'"--forge-dir", default="forge"',
    b'"--forge-dir", default="D:/ForgeCommander/forge-repo/forge-gui"'
)

with open('scripts/eval_policy.py', 'wb') as f:
    f.write(data)

py_compile.compile('scripts/eval_policy.py', doraise=True)
print('eval_policy.py - Syntax OK')
