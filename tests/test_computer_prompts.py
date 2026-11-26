import re
import unittest

from mixharness.catalog import MODELS
from mixharness.clients import client_prompt
from mixharness.providers import declarations, instructions
from mixharness.store import CHAT_JOBS, PROVIDERS, pack


class ComputerPromptTests(unittest.TestCase):
    def test_prompt_names_only_tools_available_for_the_selected_scope(self):
        scopes = (('window', 'window_capture'), ('desktop', 'computer_list'))
        for kind in (*CHAT_JOBS.values(), 'engineering'):
            for scope, first_tool in scopes:
                for access in ('view', 'control'):
                    with self.subTest(kind=kind, scope=scope, access=access):
                        payload = {'window_scope': scope, 'window_access': access}
                        job = {'kind': kind, 'model': MODELS[PROVIDERS[kind]][0], 'payload': payload}
                        available = {tool['name'] for tool in declarations(kind, payload)}
                        client, _ = client_prompt(job, pack({}))
                        client_header = client.partition('\n输出格式\n')[0]
                        for prompt in (instructions(job), client_header):
                            mentioned = set(re.findall(r'(?:window|computer)_(?:[a-z_]+|\*)', prompt))
                            self.assertIn(first_tool, mentioned)
                            self.assertEqual(mentioned - available, set())
                        if scope == 'desktop':
                            self.assertNotIn('用户所选的窗口', client_header)
