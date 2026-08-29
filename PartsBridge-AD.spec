# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata
from PyInstaller.utils.win32 import versioninfo


app_version = (0, 3, 8, 0)
version_resource = versioninfo.VSVersionInfo(
    ffi=versioninfo.FixedFileInfo(filevers=app_version, prodvers=app_version),
    kids=[
        versioninfo.StringFileInfo(
            [
                versioninfo.StringTable(
                    '080404B0',
                    [
                        versioninfo.StringStruct('CompanyName', 'foke'),
                        versioninfo.StringStruct('FileDescription', 'PartsBridge AD'),
                        versioninfo.StringStruct('FileVersion', '0.3.8.0'),
                        versioninfo.StringStruct('InternalName', 'PartsBridge-AD'),
                        versioninfo.StringStruct('LegalCopyright', 'Copyright (c) 2026 foke'),
                        versioninfo.StringStruct('OriginalFilename', 'PartsBridge-AD.exe'),
                        versioninfo.StringStruct('ProductName', 'PartsBridge AD'),
                        versioninfo.StringStruct('ProductVersion', '0.3.8.0'),
                    ],
                )
            ]
        ),
        versioninfo.VarFileInfo([versioninfo.VarStruct('Translation', [2052, 1200])]),
    ],
)

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('easyeda2kicad')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('altium_monkey')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Retain dependency licenses and source metadata in the redistributable build.
datas += copy_metadata('easyeda2kicad', recursive=True)
datas += copy_metadata('altium-monkey', recursive=True)
datas += collect_data_files('geometer', includes=['licenses/**'])
datas += [(str(Path(sys.base_prefix) / 'LICENSE.txt'), 'licenses/Python')]
datas += copy_metadata('PyInstaller')
datas += [(str(Path(SPECPATH) / 'third_party_licenses'), 'licenses/third-party')]


a = Analysis(
    ['start_gui.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PartsBridge-AD',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    version=version_resource,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PartsBridge-AD',
)
