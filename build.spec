from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'main.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'templates'), 'templates'), (str(root / 'assets'), 'assets'), (str(root / 'styles.qss'), '.')],
    hiddenimports=['PySide6.QtWebEngineCore', 'PySide6.QtPdf', 'PySide6.QtMultimedia'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='蓉城时光印', debug=False, strip=False, upx=False, console=False)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='蓉城时光印')
