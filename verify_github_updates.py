"""Offline boundary checks for this fork's GitHub release selection."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock
import requests

ROOT = Path(__file__).resolve().parent
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='Seraphine-github-test-')
sys.path.insert(0, str(ROOT / 'Seraphine'))
from app.common.util import Github
from app.common.config import VERSION


def response(status=200, data=None):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


client = Github()
assert client.user == 'OldFarmCoder-JJ' and client.repositories == 'lol-record-assistant'
assert client.githubApi.startswith('https://')
client.sess = Mock()
release = {'tag_name': 'v9.0.0', 'assets': [{'name': 'Seraphine.7z'}, {'name': 'SHA256SUMS.txt'}]}
sums = response()
sums.text = 'a' * 64 + '  Seraphine.7z\n'
client.sess.get.side_effect = [response(data=release), sums, response(404)]
asset = client.checkUpdate()['assets'][0]
assert asset['browser_download_url'].startswith('https://gitee.com/jiagyuanxun/lol-record-assistant/')
assert asset['digest'] == 'sha256:' + 'a' * 64 and 'github.com' in asset['fallback_url']
client.sess.get.side_effect = [response(data=dict(release, tag_name='v' + VERSION))]
assert client.checkUpdate() is None
client.sess.get.side_effect = [response(data=dict(release, assets=[])), response(404)]
assert client.checkUpdate() is None and 'api.github.com' in client.sess.get.call_args.args[0]
client._giteeUpdate = Mock(side_effect=requests.ConnectionError('offline'))
client.sess.get.side_effect = None
client.sess.get.return_value = response(404)
assert client.checkUpdate() is None
client.sess.get.return_value = response(data={'tag_name': 'v9.0.0', 'assets': []})
assert client.checkUpdate() is None
for tag in ('v' + VERSION, 'v0.1.0', 'vbroken'):
    client.sess.get.return_value = response(data={'tag_name': tag, 'assets': [{'name': 'Seraphine.7z'}]})
    assert client.checkUpdate() is None
meta = base64.b64encode(json.dumps({VERSION: {'forbidden': False}}).encode()).decode()
client.sess.get.side_effect = [response(data={
    'tag_name': 'v9.0.0', 'assets': [{'name': 'checksums.txt'},
                                  {'name': 'Seraphine.7z', 'browser_download_url': 'https://github.com/OldFarmCoder-JJ/lol-record-assistant/releases/download/v9.0.0/Seraphine.7z'}]}),
    response(data={'content': meta})]
update = client.checkUpdate()
assert update['assets'][0]['name'] == 'Seraphine.7z' and len(update['assets']) == 1
assert update['forbidden'] is False
assert '/contents/Seraphine/document/ver.json' in client.sess.get.call_args.args[0]
client.sess.get.side_effect = [response(data={'tag_name': 'v9.0.0', 'assets': [{'name': 'Seraphine.7z'}]}), response(404)]
assert client.checkUpdate()['forbidden'] is False
print('PASS: own HTTPS update source, no release/package, same/older/invalid versions, correct 7z asset and optional metadata.')
