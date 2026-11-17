# PyInstaller spec — Live_Clipping, one-folder build (all platforms)
# Build with:  pip install pyinstaller  &&  pyinstaller build_pyinstaller.spec
# Output: dist/Live_Clipping/  (run Live_Clipping / Live_Clipping.exe inside)
# Note: ffmpeg + yt-dlp must still be installed on the target machine
# (ffmpeg is a system binary; yt-dlp is bundled as a Python module here,
# but the app also accepts a system yt-dlp binary if present).
# -*- mode: python ; coding: utf-8 -*-
block_cipher = None

a = Analysis(
    ['live_clipping.py'],
    pathex=[],
    binaries=[],
    datas=[('panel', 'panel'), ('engine', 'engine')],
    hiddenimports=['yt_dlp'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Live_Clipping',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=True, upx_exclude=[], name='Live_Clipping',
)
