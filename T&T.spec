# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\minh0\\OneDrive\\Desktop\\T&T\\source_code\\main.py'],
    pathex=['C:\\Users\\minh0\\OneDrive\\Desktop\\T&T\\source_code'],
    binaries=[],
    datas=[('C:\\Users\\minh0\\OneDrive\\Desktop\\T&T\\source_code\\ui', 'ui'), ('C:\\Users\\minh0\\OneDrive\\Desktop\\T&T\\source_code\\assets', 'assets'), ('C:\\Users\\minh0\\OneDrive\\Desktop\\T&T\\source_code\\VERSION', '.')],
    hiddenimports=['app', 'fotello_engine', 'autohdr_engine', 'upscale_engine', 'autoenhance_engine', 'detail_merge_engine', 'storage_manager', 'free_tools_engine', 'image_metadata', 'image_upload_preprocessing', 'tls_config', 'clr'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'torchvision', 'transformers', 'sklearn', 'huggingface_hub', 'safetensors', 'tokenizers', 'sympy', 'pypdf'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='T&T',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\minh0\\OneDrive\\Desktop\\T&T\\source_code\\assets\\autofotello-app-logo.ico'],
)

