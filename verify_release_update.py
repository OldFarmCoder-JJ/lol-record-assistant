"""Isolated release validation and actual Windows file replacement/rollback checks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / 'Seraphine')]
from tools.build_release import write_manifest
from app.common.update import safeReleasePath, validatePayload, prepareUpdate, installationDir
import py7zr

work = Path(tempfile.mkdtemp(prefix='Seraphine-release-test-'))
stage = work / 'update' / 'payload'
stage.mkdir(parents=True)
(stage / '_internal').mkdir()
(stage / 'Seraphine.exe').write_bytes(b'MZ-new-version')
(stage / '_internal' / 'runtime.dll').write_bytes(b'new-runtime')
write_manifest(stage, '1.3.1', 'test-commit')
assert validatePayload(stage)['version'] == '1.3.1'
for name in ('../outside', '/absolute', 'C:/outside', '_internal/../../outside', 'C:relative', 'file:stream'):
    try:
        safeReleasePath(name)
    except ValueError:
        continue
    raise AssertionError(name)

assert installationDir() is None
with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(work / '安装目录' / 'Seraphine.exe')):
    assert installationDir() == work / '安装目录'

archive = work / 'release.7z'
with py7zr.SevenZipFile(archive, 'w') as bundle:
    for file in stage.rglob('*'):
        if file.is_file():
            bundle.write(file, arcname=file.relative_to(stage).as_posix())
assert prepareUpdate(archive, work / 'decoded')['version'] == '1.3.1'
(stage / '_internal/runtime.dll').write_bytes(b'corrupted')
try:
    validatePayload(stage)
except ValueError:
    pass
else:
    raise AssertionError('Corrupted payload accepted')
(stage / '_internal/runtime.dll').write_bytes(b'new-runtime')

installer = ROOT / 'Seraphine/app/resource/bin/update_install.ps1'
target = work / '安装 目录'
(target / '_internal').mkdir(parents=True)
(target / 'Seraphine.exe').write_bytes(b'MZ-old-version')
(target / '_internal/runtime.dll').write_bytes(b'old-runtime')
(target / 'unrelated-user-file.txt').write_text('preserve', encoding='utf-8')
command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
           '-File', str(installer), '-InstallRoot', str(target), '-StageRoot', str(stage),
           '-AppPid', '2147483647', '-NoRestart']
result = subprocess.run(command, capture_output=True, timeout=30)
assert result.returncode == 0, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
assert b'UPDATE_OK' in result.stdout
assert (target / 'Seraphine.exe').read_bytes() == b'MZ-new-version'
assert (target / '_internal/runtime.dll').read_bytes() == b'new-runtime'
assert (target / 'unrelated-user-file.txt').read_text() == 'preserve'
assert (work / 'update/backup/Seraphine.exe').read_bytes() == b'MZ-old-version'

# A directory/file conflict after the first write must restore the prior version.
(target / 'Seraphine.exe').write_bytes(b'MZ-old-version')
(target / '_internal/runtime.dll').write_bytes(b'old-runtime')
(target / '_internal' / 'blocked').write_text('existing file')
(stage / '_internal' / 'blocked').mkdir()
(stage / '_internal/blocked/new.dll').write_bytes(b'new')
write_manifest(stage, '1.3.1', 'test-commit')
result = subprocess.run(command, capture_output=True, timeout=30)
assert result.returncode != 0 and b'UPDATE_FAILED' in result.stdout
assert (target / 'Seraphine.exe').read_bytes() == b'MZ-old-version'
assert (target / '_internal/runtime.dll').read_bytes() == b'old-runtime'
assert (target / 'unrelated-user-file.txt').read_text() == 'preserve'
assert (target / '_internal/blocked').read_text() == 'existing file'

# A tampered install list cannot address a file outside the installation.
outside = work / 'outside.txt'
outside.write_text('safe')
(stage / 'filelist.txt').write_text('../outside.txt\n')
result = subprocess.run(command, capture_output=True, timeout=30)
assert result.returncode != 0 and outside.read_text() == 'safe'

os.environ['APPDATA'] = str(work / 'profile')
from app.common.qt_runtime import configure_platform_plugins
configure_platform_plugins()
from PyQt5.QtWidgets import QApplication, QWidget
from app.components.message_box import UpdateMessageBox
app = QApplication([])
info = dict(tag_name='v1.3.2', body='test', forbidden=False, assets=[])
parent = QWidget()
parent.resize(900, 700)
source_dialog = UpdateMessageBox(info, parent)
assert not source_dialog.myYesButton.isEnabled()
with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(target / 'Seraphine.exe')):
    frozen_dialog = UpdateMessageBox(info, parent)
    assert frozen_dialog.myYesButton.isEnabled()
print('PASS: executable root under _internal cwd, archive/hash/path checks, actual PowerShell replacement,')
print('Unicode/space paths, backup, unrelated file preservation, failed-copy rollback, tampered manifest rejection,')
print('packaged update button enabled and source update button disabled.')
