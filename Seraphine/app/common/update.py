"""Validate release payloads and update the executable's own installation directory."""
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

import py7zr


def installationDir():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else None


def safeReleasePath(value):
    value = str(value).replace('\\', '/')
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ntpath.splitdrive(value)[0]
            or '..' in path.parts or ':' in value or not path.parts):
        raise ValueError('发布包包含无效文件路径。')
    return path.as_posix()


def fileDigest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validatePayload(folder):
    root = Path(folder).resolve()
    manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
    hashes = manifest['files']
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('发布包文件清单为空。')
    declared = {safeReleasePath(name) for name in hashes}
    if len(declared) != len(hashes):
        raise ValueError('发布包文件路径重复。')
    actual = set()
    for file in root.rglob('*'):
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            raise ValueError('发布包不能包含链接。')
        if file.is_file():
            actual.add(file.relative_to(root).as_posix())
    if actual != declared | {'release-manifest.json'}:
        raise ValueError('发布包内容与校验清单不一致。')
    listed = (root / 'filelist.txt').read_text(encoding='utf-8-sig').splitlines()
    if len(listed) != len(set(listed)) or {safeReleasePath(name) for name in listed} != actual:
        raise ValueError('发布包安装清单不一致。')
    for name, expected in hashes.items():
        path = root.joinpath(*PurePosixPath(safeReleasePath(name)).parts)
        if fileDigest(path).lower() != str(expected).lower():
            raise ValueError('发布包文件校验失败：' + name)
    if not (root / 'Seraphine.exe').is_file() or not (root / '_internal').is_dir():
        raise ValueError('发布包缺少主程序或运行库。')
    return manifest


def prepareUpdate(archive_path, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('更新暂存目录已存在。')
    with py7zr.SevenZipFile(archive_path, 'r') as archive:
        normalized = [safeReleasePath(name) for name in archive.getnames()]
        if len(normalized) != len(set(name.casefold() for name in normalized)):
            raise ValueError('发布包存在重复路径。')
        if any(getattr(info, 'is_symlink', False) for info in archive.list()):
            raise ValueError('发布包不能包含链接。')
        destination.mkdir(parents=True)
        archive.extractall(destination)
    return validatePayload(destination)


def runUpdater(staging_dir):
    root = installationDir()
    if root is None:
        raise ValueError('源码运行无需覆盖安装包。')
    staging = Path(staging_dir).resolve()
    validatePayload(staging)
    script_source = Path(__file__).resolve().parents[1] / 'resource' / 'bin' / 'update_install.ps1'
    script = staging.parent / 'install.ps1'
    shutil.copyfile(script_source, script)
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-WindowStyle', 'Hidden', '-File', str(script), '-InstallRoot', str(root),
               '-StageRoot', str(staging), '-AppPid', str(os.getpid())]
    with (staging.parent / 'update.log').open('ab') as output:
        return subprocess.Popen(command, cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)

