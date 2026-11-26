import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
LISTENING = re.compile(rb'Uvicorn running on (http://127[.]0[.]0[.]1:[0-9]+)\b')


def request(url, method='GET', data=None):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    req = Request(url, data=body, method=method, headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=4) as response:
        if response.status == 204:
            return None
        return json.load(response)


@contextmanager
def service(project):
    project = Path(project).resolve(strict=True)
    env = {k: v for k, v in os.environ.items() if k not in (
        'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENAI_API_KEY', 'MIXHARNESS_WORKSPACE')}
    env.update(TMPDIR=str(project), TEMP=str(project), TMP=str(project), NO_COLOR='1')
    path = project / ('service-' + uuid4().hex + '.log')
    args = [sys.executable, '-m', 'mixharness', '--web', '--project', str(project),
            '--data-dir', str(project / '.mixharness'), '--port', '0']
    with path.open('wb') as log:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                output = path.read_bytes()
                if process.poll() is not None:
                    raise RuntimeError(output.decode(errors='replace'))
                match = LISTENING.search(output)
                if match:
                    url = match[1].decode('ascii') + '/api'
                    if request(url + '/config')['project'] != str(project):
                        raise RuntimeError('测试服务返回的会话库路径与本次临时目录不符')
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('测试服务未公布监听地址\n' + output.decode(errors='replace'))
                time.sleep(0.05)
            yield url
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


class HttpTests(unittest.TestCase):
    def test_concurrent_services_use_separate_libraries_and_ports(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix='http-isolation-') as directory:
            first, second = Path(directory) / 'first', Path(directory) / 'second'
            first.mkdir()
            second.mkdir()
            with service(first) as left, service(second) as right:
                self.assertNotEqual(left, right)
                sid = request(left + '/sessions', 'POST', {})['id']
                self.assertEqual(request(left + '/sessions/' + sid)['session']['id'], sid)
                with self.assertRaises(HTTPError) as missing:
                    request(right + '/sessions/' + sid)
                self.assertEqual(missing.exception.code, 404)
                missing.exception.close()
                db = Path('.mixharness/sessions.sqlite3')
                self.assertFalse((first / db).samefile(second / db))
            self.assertFalse((first / '.mixharness/control.json').exists())
            self.assertFalse((second / '.mixharness/control.json').exists())

    def test_manage_project_and_remove_one_conversation(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            root = Path(tmp)
            with service(root) as api:
                p = request(api + '/projects', 'POST', {'name': '编译测试', 'folders': [str(ROOT / 'mixharness')]})
                ids = [request(api + '/sessions', 'POST', {'project_id': p['id']})['id'] for _ in range(2)]
                path = api + '/sessions/' + ids[0]
                request(path + '/messages', 'POST', {'text': '核对验证命令。', 'client_id': uuid4().hex})
                request(path, 'PATCH', {'title': '验证流程', 'pinned': True, 'archived': True})
                self.assertEqual([r['id'] for r in request(api + '/sessions')], ids[1:])
                self.assertEqual(request(api + '/sessions?archived=true')[0]['title'], '验证流程')
                request(path, 'PATCH', {'archived': False})
                request(path, 'DELETE')
                with self.assertRaises(HTTPError) as missing:
                    request(path)
                self.assertEqual(missing.exception.code, 404)
                missing.exception.close()
            with service(root) as api:
                self.assertEqual([r['id'] for r in request(api + '/sessions')], ids[1:])
                self.assertEqual(request(api + '/projects')[0]['id'], p['id'])

    def test_session_survives_service_restart(self):
        scratch = ROOT / '.work' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            project = Path(tmp)
            with service(project) as api:
                config = request(api + '/config')
                self.assertFalse(any(config['keys'].values()))
                for provider, model in (('gemini', 'gemini-4-carbon'), ('openai', 'gpt-6-luna')):
                    catalog = request(api + '/models/' + provider + '/refresh', 'POST')
                    self.assertEqual(catalog['source'], 'reference')
                    self.assertIn(model, catalog['models'])
                root = request(api + '/projects', 'POST', {'directory': str(ROOT)})
                self.assertEqual(root['id'], request(api + '/projects', 'POST', {'directory': str(ROOT)})['id'])
                linked = request(api + '/sessions', 'POST', {'project_id': root['id']})
                self.assertEqual(linked['workspace'], str(ROOT))
                sid = request(api + '/sessions', 'POST', {})['id']
                path = '/sessions/' + sid
                model = config['models']['openai'][1]
                request(api + path + '/models', 'PATCH', {'provider': 'openai', 'model': model, 'effort': 'high'})
                with self.assertRaises(HTTPError) as invalid:
                    request(api + path + '/models', 'PATCH', {'provider': 'openai', 'model': model, 'effort': 'none'})
                self.assertEqual(invalid.exception.code, 422)
                invalid.exception.close()
                message = {'text': '请为这个项目写一份验证报告。', 'client_id': uuid4().hex}
                original = request(api + path + '/messages', 'POST', message)
            with service(project) as api:
                repeated = request(api + path + '/messages', 'POST', message)
                self.assertEqual(repeated, original)
                state = request(api + path)
                self.assertEqual(state['session']['revision'], 1)
                request(api + path + '/messages', 'POST', {'text': '报告读者调整为产品经理。', 'client_id': uuid4().hex})
                state = request(api + path)
                self.assertEqual(state['session']['revision'], 2)
                self.assertEqual(state['session']['openai'], model)
                self.assertEqual(state['session']['efforts'], {'gemini': '', 'openai': 'high'})
                self.assertEqual(len([e for e in state['events'] if e['actor'] == 'user']), 2)
                jid = state['tasks'][-1]['id']
                self.assertEqual(request(api + path + '/stop', 'POST'), {'stopped': 1})
                self.assertEqual(request(api + path + '/continue', 'POST'), {'continued': 1})
                guide = {'intent': 'steer', 'text': '请用中文列出验证步骤。', 'client_id': uuid4().hex}
                receipt = request(api + path + '/messages', 'POST', guide)
                state = request(api + path)
                self.assertEqual(state['session']['revision'], 2)
                self.assertEqual(state['tasks'][-1]['id'], jid)
                self.assertEqual(state['tasks'][-1]['guide_seq'], receipt['seq'])
