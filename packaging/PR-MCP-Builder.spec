from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH).resolve().parent

streamlit_datas, streamlit_binaries, streamlit_hidden = collect_all("streamlit")
hiddenimports = list(streamlit_hidden)
hiddenimports += collect_submodules("app")
hiddenimports += [
    "scripts.analyze_regulation_corpus",
    "scripts.generate_mcp_client_config",
    "scripts.mcp_bundle_contract",
    "scripts.find_available_ui_port",
    "scripts.run_regulation_mcp",
]

datas = list(streamlit_datas)
datas += [
    (str(project_root / "frontend" / "streamlit_app.py"), "frontend"),
]

a = Analysis(
    [str(project_root / "packaging" / "windows_launcher.py")],
    pathex=[str(project_root)],
    binaries=streamlit_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "unittest.mock",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "tkinter",
        "matplotlib",
        "IPython",
        "notebook",
        "black",
        "yapf",
        "pygame",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PR MCP Builder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
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
    name="PR MCP Builder",
)
