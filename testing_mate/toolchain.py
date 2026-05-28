from __future__ import annotations

import os
import shutil
from pathlib import Path

DEFAULT_MINGW_ROOT = Path(r"C:\msys64\mingw64")
MINGW_ROOT_ENV = "TESTMATE_MINGW_ROOT"


def tool_root() -> Path:
    return Path(__file__).resolve().parents[1]


def normalize_mingw_root(path: str | Path) -> Path:
    selected = Path(path).expanduser()
    if selected.name.lower() == "bin":
        selected = selected.parent
    if selected.name.lower() == "msys64":
        return selected / "mingw64"
    if (selected / "mingw64" / "bin").exists():
        return selected / "mingw64"
    return selected


def configured_mingw_root() -> Path:
    raw = os.environ.get(MINGW_ROOT_ENV, "").strip()
    return normalize_mingw_root(raw) if raw else DEFAULT_MINGW_ROOT


def configure_mingw_root(path: str | Path) -> Path:
    root = normalize_mingw_root(path)
    os.environ[MINGW_ROOT_ENV] = str(root)
    return root


def local_mingw_bin() -> Path:
    return configured_mingw_root() / "bin"


def msys2_root() -> Path:
    root = configured_mingw_root()
    if root.name.lower() == "mingw64":
        return root.parent
    return root.parent if root.name.lower() == "bin" else root


def toolchain_path_entries() -> list[str]:
    candidates = [str(local_mingw_bin()), str(msys2_root() / "usr" / "bin")]
    return [item for item in candidates if Path(item).exists()]


def toolchain_env() -> dict[str, str]:
    env = os.environ.copy()
    entries = toolchain_path_entries()
    if entries:
        env["PATH"] = os.pathsep.join(entries + [env.get("PATH", "")])
    return env


def windows_to_msys_path(path: Path) -> str:
    resolved = Path(path).resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix()[3:] if resolved.drive else resolved.as_posix()
    return f"/{drive}/{rest}" if drive else rest


def msys_bash_path() -> str | None:
    root = msys2_root()
    for candidate in (str(root / "usr" / "bin" / "bash.exe"), str(root / "mingw64.exe")):
        if Path(candidate).exists():
            return candidate
    return shutil.which("bash", path=toolchain_env().get("PATH"))


def mingw_toolchain_available() -> bool:
    root = configured_mingw_root()
    msys_root = msys2_root()
    return (root / "bin").exists() and (
        (msys_root / "usr" / "bin" / "bash.exe").exists()
        or (msys_root / "mingw64.exe").exists()
    )
