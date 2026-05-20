from __future__ import annotations

import os
import shutil
from pathlib import Path


def tool_root() -> Path:
    return Path(__file__).resolve().parents[1]


def local_mingw_bin() -> Path:
    return tool_root() / "mingw64" / "bin"


def toolchain_path_entries() -> list[str]:
    candidates = [
        str(local_mingw_bin()),
        r"C:\msys64\mingw64\bin",
        r"C:\msys64\ucrt64\bin",
        r"C:\msys64\clang64\bin",
        r"C:\msys64\usr\bin",
    ]
    extra = os.environ.get("TESTMATE_TOOLCHAIN_PATH", "")
    candidates.extend(item.strip() for item in extra.split(os.pathsep) if item.strip())
    return [item for item in candidates if Path(item).exists()]


def toolchain_env() -> dict[str, str]:
    env = os.environ.copy()
    entries = toolchain_path_entries()
    if entries:
        env["PATH"] = os.pathsep.join(entries + [env.get("PATH", "")])
    return env


def windows_to_msys_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = resolved.as_posix()[3:] if resolved.drive else resolved.as_posix()
    return f"/{drive}/{rest}" if drive else rest


def msys_bash_path() -> str | None:
    local_bash = local_mingw_bin() / "bash.exe"
    for candidate in (str(local_bash), r"C:\msys64\usr\bin\bash.exe", r"C:\msys64\mingw64.exe"):
        if Path(candidate).exists():
            return candidate
    return shutil.which("bash", path=toolchain_env().get("PATH"))
