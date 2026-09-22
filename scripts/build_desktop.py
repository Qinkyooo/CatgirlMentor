"""Build a clean Windows distribution from a wheel and embedded Python archive.

Requires uv, Bun, Windows .NET Framework csc, and optional Inno Setup ISCC.
Network access is needed at build time only. Never copies a developer venv.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path = ROOT, env=None) -> None:
    subprocess.run(list(args), cwd=cwd, env=env, check=True)


def build(output: Path, python_zip: Path, iscc: Path | None, webui_built: bool = False) -> None:
    output = output.resolve()
    stage = output / "CatgirlMentor"
    if stage.exists():
        raise SystemExit("Use a new output directory; existing staging data is never deleted.")
    output.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not webui_built:
        run("bun", "install", "--frozen-lockfile", cwd=ROOT / "webui")
        run("bun", "run", "build", cwd=ROOT / "webui")
    elif not (ROOT / "nanobot/web/dist/index.html").is_file():
        raise ValueError("Build the WebUI before using --webui-built.")
    env = dict(os.environ, NANOBOT_SKIP_WEBUI_BUILD="1")
    run("uv", "build", "--wheel", "--out-dir", str(output / "wheels"), env=env)
    wheel = next((output / "wheels").glob("*.whl"))
    runtime = stage / "runtime"
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(python_zip) as archive:
        for info in archive.infolist():
            if not (runtime / info.filename).resolve().is_relative_to(runtime):
                raise ValueError("Unsafe runtime archive path")
        archive.extractall(runtime)
    pth = next(runtime.glob("python*._pth"))
    python_lib = next(runtime.glob("python*.zip")).name
    pth.write_text(f"{python_lib}\n.\nLib/site-packages\nimport site\n", encoding="utf-8")
    requirements = ROOT / "packaging" / "windows" / "requirements.txt"
    if not requirements.is_file():
        raise SystemExit("Generate packaging/windows/requirements.txt using uv export --extra desktop --no-dev --no-emit-project first.")
    # RapidFuzz's optional unsigned extensions can be blocked by Windows Smart
    # App Control. Build its supported pure-Python wheel from the hashed sdist.
    run("uv", "pip", "install", "--python", str(runtime / "python.exe"), "--target", str(runtime / "Lib" / "site-packages"), "--require-hashes", "--no-binary", "rapidfuzz", "--config-settings-package", "rapidfuzz:wheel.cmake=false", "-r", str(requirements))
    run("uv", "pip", "install", "--python", str(runtime / "python.exe"), "--target", str(runtime / "Lib" / "site-packages"), "--no-deps", str(wheel))
    # Local-wheel provenance contains the builder's absolute checkout path.
    for provenance in (runtime / "Lib" / "site-packages").glob("*.dist-info/direct_url.json"):
        provenance.unlink()
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copy2(ROOT / "nanobot" / "desktop" / "static" / "desktop.ico", stage / "desktop.ico")
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    run(str(csc), "/nologo", "/target:winexe", "/platform:x64", "/reference:System.Windows.Forms.dll", f"/win32icon:{stage / 'desktop.ico'}", f"/out:{stage / 'CatgirlMentor.exe'}", str(ROOT / "packaging" / "windows" / "Launcher.cs"))
    # Embedded Python is isolated from PYTHONPATH and registered system installs.
    run(str(runtime / "python.exe"), "-c", "import nanobot.desktop.server, pystray; from nanobot.config.schema import Config; Config(); print('desktop runtime OK')", cwd=stage)
    run(str(runtime / "python.exe"), str(ROOT / "scripts/smoke_ffxiv.py"), cwd=stage)
    run(str(runtime / "python.exe"), str(ROOT / "scripts/smoke_windows_compat.py"), cwd=stage)
    installed = runtime / "Lib" / "site-packages" / "nanobot"
    for required in ("web/dist/index.html", "desktop/static/index.html", "desktop/static/tray-32.png", "games/ffxiv/data/guide.sqlite3", "games/ffxiv/ffxiv-servers.json"):
        if not (installed / required).is_file():
            raise ValueError(f"Missing packaged resource: {required}")
    with python_zip.open("rb") as archive_file:
        archive_hash = hashlib.file_digest(archive_file, "sha256").hexdigest()
    manifest = {"version": version, "python_archive_sha256": archive_hash, "files": {}}
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            if path.name in {"config.json", "manager.json", "desktop.json", "pyvenv.cfg"} or path.suffix == ".jsonl":
                raise ValueError(f"Unexpected private/runtime data in package: {path}")
            with path.open("rb") as handle:
                manifest["files"][path.relative_to(stage).as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    (stage / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if iscc:
        run(str(iscc), f"/DAppRoot={stage}", f"/DOutputDir={output}", f"/DAppVersion={version}", str(ROOT / "packaging" / "windows" / "CatgirlMentor.iss"))
    print(f"Distribution ready: {stage}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-zip", type=Path, required=True)
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--webui-built", action="store_true", help="Reuse an already verified, unchanged frontend build")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Build on Windows x64.")
    build(args.output, args.python_zip.resolve(), args.iscc.resolve() if args.iscc else None, args.webui_built)
