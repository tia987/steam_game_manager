# PyInstaller build description.
#   pyinstaller nsgm.spec
# Produces a single self-contained executable in dist/.
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

analysis = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=[],
    # The interface is plain files read at runtime, so they have to travel
    # with the binary rather than being imported.
    datas=[("src/nsgm/web", "nsgm/web")],
    hiddenimports=["sqlite3"],
    hookspath=[],
    runtime_hooks=[],
    # Nothing here draws a window of its own; the UI is the browser.
    excludes=["tkinter", "test", "unittest", "pydoc_data", "lib2to3"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="nonsteam-game-manager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,          # the window shows the address and any errors
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
