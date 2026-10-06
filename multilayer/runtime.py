import os
import shutil
import sys
from pathlib import Path

from multilayer import __version__
from multilayer.model import MultilayerError

TOOLS = {"qemu-img", "qemu-system-x86_64", "swtpm", "ip", "bpftool"}


def bundled_root() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    bases = [Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))]
    bases.extend([Path(sys.executable).parent / "_internal", Path(sys.executable).parent])
    for base in bases:
        root = base / "qemu"
        if (root / "bin").is_dir():
            return root.resolve()
    return None


def bundled_executable(name: str) -> Path | None:
    if name in TOOLS and (root := bundled_root()):
        candidate = root / "bin" / (name + (".exe" if sys.platform == "win32" else ""))
        if candidate.is_file():
            return candidate.resolve()
    return None


def executable(name: str) -> str:
    if bundled := bundled_executable(name):
        return str(bundled)
    if (
        getattr(sys, "frozen", False)
        and name in TOOLS
        and (sys.platform == "linux" or name.startswith("qemu-"))
    ):
        raise MultilayerError(
            f"Встроенный компонент {name} отсутствует в Мультислое {__version__}. "
            "Закройте старые окна и переустановите свежий дистрибутив целиком. "
            "Отдельная установка QEMU не требуется."
        )
    found = shutil.which(name)
    if not found and sys.platform == "linux":
        found = shutil.which(name, path="/usr/local/bin:/usr/bin:/bin:/usr/sbin")
    if not found and sys.platform == "win32":
        directory = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "qemu"
        candidate = directory / (name + ".exe")
        if candidate.is_file():
            found = str(candidate)
    if not found:
        raise MultilayerError(
            f"Не найден {name}. Для запуска из исходников установите зависимости и добавьте их в PATH."
        )
    return str(Path(found).resolve())


def external_env(program: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        if original := env.get("LD_LIBRARY_PATH_ORIG"):
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
        env.pop("QEMU_MODULE_DIR", None)
        root = bundled_root()
        if root and program and Path(program).resolve().parent == root / "bin":
            if sys.platform == "linux":
                env["LD_LIBRARY_PATH"] = str(root / "lib")
            env["QEMU_MODULE_DIR"] = str(root / "modules")
    return env
