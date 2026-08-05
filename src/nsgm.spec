# PyInstaller build description.
#   pyinstaller nsgm.spec
# Paths are relative to this file, which sits beside the nsgm package.
import importlib.util
import sys

block_cipher = None

# webview gives the app a window of its own; its backends differ per
# platform and none of them exist everywhere. Naming them unconditionally
# fills the build log with "Hidden import not found" errors that look like
# failures and are not, so only what is actually installed is declared.
CANDIDATES = [
    "sqlite3",
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.cocoa",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "clr_loader",
    "pythonnet",
]
OPTIONAL_IMPORTS = []
for name in CANDIDATES:
    try:
        if importlib.util.find_spec(name) is not None:
            OPTIONAL_IMPORTS.append(name)
    except (ImportError, ValueError, ModuleNotFoundError):
        pass
print("nsgm.spec: bundling", OPTIONAL_IMPORTS)

analysis = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    # The interface is plain files read at runtime, so they have to travel
    # with the binary rather than being imported.
    datas=[("nsgm/web", "nsgm/web")],
    hiddenimports=OPTIONAL_IMPORTS,
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
    # No console window. The app has a window of its own now, and a black
    # terminal sitting behind it is the main thing that made this look
    # unfinished. Startup errors still print if launched from a terminal.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS will not treat a bare Unix executable as an application: double
# clicking one opens it in TextEdit. It needs a .app bundle, which is a
# folder with a particular shape, and that is what this produces.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Non-Steam Game Manager.app",
        icon=None,
        bundle_identifier="com.tia987.nonsteamgamemanager",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
            "CFBundleShortVersionString": "1.0.0",
            # It has a window, so it belongs in the Dock like anything else.
            "LSUIElement": False,
        },
    )
