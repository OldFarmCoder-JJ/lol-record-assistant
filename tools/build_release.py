"""Build a Windows release only when explicitly invoked."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def write_manifest(folder, version, commit):
    folder = Path(folder)
    names = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
    names |= {'filelist.txt', 'release-manifest.json'}
    (folder / 'filelist.txt').write_text('\n'.join(sorted(names)) + '\n', encoding='utf-8')
    manifest = {'version': version, 'commit': commit, 'files': {
        name: digest(folder / name) for name in sorted(names) if name != 'release-manifest.json'}}
    (folder / 'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    if sys.platform != 'win32':
        raise SystemExit('Release builds require Windows.')
    source = ROOT / 'Seraphine'
    version = re.search(r'^VERSION = "([^"]+)"', (source / 'app/common/config.py').read_text(encoding='utf-8'), re.M)[1]
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', 'Seraphine', 'tools/build_release.py'], cwd=ROOT).returncode:
        raise SystemExit('Commit source changes before creating a release.')
    output = (ROOT / 'release' / ('v' + version)).resolve()
    work = (ROOT / '.build' / ('v' + version)).resolve()
    if not output.is_relative_to(ROOT) or not work.is_relative_to(ROOT):
        raise SystemExit('Invalid output path.')
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--windowed', '--name', 'Seraphine',
               '--distpath', str(output), '--workpath', str(work / 'build'), '--specpath', str(work),
               '--paths', str(source), '--icon', str(source / 'app/resource/images/logo.ico')]
    for name in ('images', 'qss', 'i18n', 'icons', 'bin'):
        command += ['--add-data', str(source / 'app/resource' / name) + os.pathsep + 'app/resource/' + name]
    subprocess.run(command + [str(source / 'main.py')], cwd=ROOT, check=True)
    folder = output / 'Seraphine'
    shutil.copyfile(ROOT / 'LICENSE', folder / 'LICENSE')
    (folder / 'VERSION.txt').write_text(version + '\n', encoding='utf-8')
    (folder / 'SOURCE.txt').write_text('https://github.com/OldFarmCoder-JJ/lol-record-assistant\nCommit: ' + commit + '\n', encoding='utf-8')
    (folder / '使用说明.txt').write_text('解压完整文件夹后运行 Seraphine.exe，请保留 _internal 目录。\n先打开英雄联盟客户端。\n个人配置和缓存保存在 %APPDATA%/Seraphine。\n旧的上游版本首次迁移到本修改版时，请手动下载；之后可检查本仓库更新。\n', encoding='utf-8')
    write_manifest(folder, version, commit)
    files = sorted(p for p in folder.rglob('*') if p.is_file())
    import py7zr
    with py7zr.SevenZipFile(output / 'Seraphine.7z', 'w', filters=[{'id': py7zr.FILTER_LZMA2, 'preset': 5}]) as archive:
        for file in files:
            archive.write(file, arcname=file.relative_to(folder).as_posix())
    with zipfile.ZipFile(output / 'Seraphine.zip', 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in files:
            archive.write(file, arcname='Seraphine/' + file.relative_to(folder).as_posix())
    (output / 'SHA256SUMS.txt').write_text(''.join(digest(output / name) + '  ' + name + '\n'
                                                    for name in ('Seraphine.7z', 'Seraphine.zip')), encoding='utf-8')
    print('Release:', version, 'commit:', commit, 'files:', len(files), flush=True)
    for name in ('Seraphine.7z', 'Seraphine.zip', 'SHA256SUMS.txt'):
        print(name, (output / name).stat().st_size, flush=True)


if __name__ == '__main__':
    main()
