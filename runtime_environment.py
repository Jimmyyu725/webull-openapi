from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def base_python() -> Path:
    """Return a Python interpreter outside the active virtual environment."""
    directory = Path(sys.base_prefix) / "bin"
    names = (
        f"python{sys.version_info.major}.{sys.version_info.minor}",
        f"python{sys.version_info.major}",
        "python3",
        "python",
    )
    for name in names:
        candidate = directory / name
        if candidate.exists():
            return candidate
    raise RuntimeError(f"No base Python interpreter found under {directory}")


def ensure_runtime_venv(path: Path) -> Path:
    """Create or repair a deploy venv without nesting it under the project venv."""
    base = base_python()
    python = path / "bin" / "python"
    configuration = path / "pyvenv.cfg"
    try:
        home = next(
            line.split("=", 1)[1].strip()
            for line in configuration.read_text(encoding="utf-8").splitlines()
            if line.startswith("home =")
        )
    except (OSError, StopIteration):
        home = None
    expected_home = str(base.parent)
    if not python.exists() or home != expected_home:
        command = [str(base), "-m", "venv"]
        if python.exists():
            command.append("--upgrade")
        subprocess.run([*command, str(path)], check=True)
    return python
