from pathlib import Path


spec_location = Path(SPECPATH).resolve()
spec_directory = spec_location if spec_location.is_dir() else spec_location.parent
project_root = spec_directory.parent
scripts_root = project_root / "scripts"

hidden_imports = [
    "plan_api.server",
    "plan_api.pipeline",
    "plan_api.worker",
    "replay_validator",
    "stage1_simple.solve",
    "stage2_simple.solve",
    "stage3_simple.solve",
    "stage4_simple.solve",
    "solver_vnext.domain",
    "solver_vnext.physical",
    "solver_vnext.frontier",
    "solver_vnext.serial",
    "solver_vnext.spotting",
    "solver_vnext.placement",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.loops.asyncio",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
]

excluded_modules = [
    "pytest",
    "_pytest",
    "iniconfig",
    "packaging",
    "pluggy",
    "pygments",
    "yaml",
    "dotenv",
    "httpx",
    "httpcore",
    "itsdangerous",
    "jinja2",
    "multipart",
    "streamlit",
    "numpy",
    "pandas",
    "matplotlib",
    "openpyxl",
    "tkinter",
    "httptools",
    "watchfiles",
    "websockets",
    "uvloop",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.workers",
    "anyio.pytest_plugin",
    "anyio._backends._trio",
    "trio",
    "outcome",
]

a = Analysis(
    [str(project_root / "packaging" / "windows_server_entry.py")],
    pathex=[str(project_root), str(scripts_root)],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="train-cal-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name="train-cal-server",
)
