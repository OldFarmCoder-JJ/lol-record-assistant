"""Work around Qt5 resolving its plugin directory incorrectly under Unicode paths."""
import os
import sys
import shutil
import tempfile
import hashlib
from pathlib import Path


def configure_platform_plugins():
    if sys.platform != 'win32' or os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH'):
        return
    import PyQt5
    source = Path(PyQt5.__file__).parent / 'Qt5' / 'plugins' / 'platforms'
    if str(source).isascii() or not source.is_dir():
        return
    version = hashlib.sha256((source / 'qwindows.dll').read_bytes()).hexdigest()[:12]
    target = Path(tempfile.gettempdir()) / 'Seraphine' / ('qt-platforms-'+version)
    if not str(target).isascii():
        return
    target.mkdir(parents=True, exist_ok=True)
    for plugin in source.glob('*.dll'):
        cached = target / plugin.name
        if not cached.exists() or cached.stat().st_size != plugin.stat().st_size:
            shutil.copy2(plugin, cached)
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(target)
