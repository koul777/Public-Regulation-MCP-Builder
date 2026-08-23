import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH).resolve().parent
version_file = os.environ.get("PR_MCP_BUILDER_VERSION_FILE", "").strip()
if not version_file or not Path(version_file).is_file():
    raise RuntimeError(
        "PR_MCP_BUILDER_VERSION_FILE must point to the generated release version metadata."
    )

streamlit_datas, streamlit_binaries, streamlit_hidden = collect_all("streamlit")
hiddenimports = list(streamlit_hidden)
hiddenimports += collect_submodules("app")
hiddenimports += [
    "scripts.analyze_regulation_corpus",
    "scripts.generate_mcp_client_config",
    "scripts.mcp_bundle_contract",
    "scripts.mcp_connection_diagnostic",
    "scripts.refresh_mcp_client_connection",
    "scripts.find_available_ui_port",
    "scripts.run_qwen_chat",
    "scripts.run_regulation_mcp",
]

datas = list(streamlit_datas)
datas += [
    (str(project_root / "frontend" / "streamlit_app.py"), "frontend"),
    (str(project_root / "frontend" / "qwen_chat_app.py"), "frontend"),
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
    version=version_file,
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
