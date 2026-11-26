import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mixharness.workspace import run_check


ROOT = Path(__file__).resolve().parents[1]


class CheckEnvironmentTests(unittest.TestCase):
    def test_git_in_snapshot_cannot_read_calling_repository_or_inline_config(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            caller = root / 'caller'
            source = root / 'source'
            source.mkdir()
            subprocess.run(['git', 'init', '--quiet', str(caller)], check=True)
            key = 'mixharness.validationpath'
            subprocess.run(['git', '-C', str(caller), 'config', '--local', key, str(caller)], check=True)
            overrides = {
                'GIT_DIR': str(caller / '.git'), 'GIT_WORK_TREE': str(caller),
                'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': key, 'GIT_CONFIG_VALUE_0': str(root),
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                'GIT_CEILING_DIRECTORIES': str(root),
            }
            previous = {name: os.environ.get(name) for name in overrides}
            query = ['git', 'config', '--default', str(source), '--get', key]
            try:
                os.environ.update(overrides)
                inherited = subprocess.run(query, cwd=source, capture_output=True, text=True, check=True)
                self.assertEqual(inherited.stdout.strip(), str(root))
                receipt = asyncio.run(run_check(source, root / 'runs', query))
                self.assertEqual(receipt['exit_code'], 0, receipt['output'])
                self.assertEqual(receipt['output'].strip(), str(source))
                self.assertEqual(os.environ['GIT_DIR'], str(caller / '.git'))
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
            saved = subprocess.run(['git', '-C', str(caller), 'config', '--local', '--get', key],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(saved.stdout.strip(), str(caller))
