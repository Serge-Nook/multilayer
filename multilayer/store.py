import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from multilayer.model import VM, MultilayerError


def default_home() -> Path:
    if override := os.environ.get("MULTILAYER_HOME"):
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Multilayer" / "data"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "multilayer"


def write_json(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path | None = None):
        self.root = (root or default_home()).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sys.platform != "win32":
            self.root.chmod(0o700)

    def path(self, identifier: str) -> Path:
        VM(name="validate", id=identifier).validate(check_files=False)
        return self.root / identifier

    def list(self) -> list[VM]:
        result = []
        for directory in sorted(self.root.iterdir()):
            if directory.is_dir() and (directory / "vm.json").is_file():
                result.append(self.get(directory.name))
        return sorted(result, key=lambda vm: vm.name.casefold())

    def get(self, identifier: str) -> VM:
        if (self.path(identifier) / ".deleting").exists():
            raise MultilayerError("ВМ удаляется")
        try:
            vm = VM(**json.loads((self.path(identifier) / "vm.json").read_text("utf-8")))
            vm.validate(check_files=False)
            if vm.id != identifier:
                raise ValueError("ID не совпадает с каталогом")
            return vm
        except (OSError, ValueError, TypeError) as exc:
            raise MultilayerError(f"Не удалось прочитать ВМ {identifier}: {exc}") from exc

    def save(self, vm: VM) -> None:
        vm.validate()
        directory = self.path(vm.id)
        directory.mkdir(mode=0o700, exist_ok=True)
        write_json(directory / "vm.json", vm.to_dict())

    def remove(self, identifier: str) -> None:
        shutil.rmtree(self.path(identifier))

    @contextlib.contextmanager
    def lock(self, identifier: str) -> Iterator[None]:
        directory = self.path(identifier)
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / ".lock"
        with path.open("a+b") as handle:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        handle.seek(0)
                        if handle.read(1) == b"":
                            handle.write(b"0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise MultilayerError("ВМ занята другой операцией") from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
