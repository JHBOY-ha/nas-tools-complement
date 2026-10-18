"""Run the installed premium auditor against the explicitly scoped action UI."""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    base = Path.home() / '.codex/plugins/cache/openai-curated-remote/frontend-design-premium'
    matches = sorted(base.glob('*/skills/frontend-design-premium/scripts/audit_project.py'))
    if not matches:
        print('Installed frontend-design-premium auditor is unavailable', file=sys.stderr)
        return 2
    return subprocess.run([sys.executable, str(matches[-1]), str(root), '--mode', 'strict',
                           '--output', '/tmp/nas-b4-premium-audit.json']).returncode


if __name__ == '__main__':
    raise SystemExit(main())
