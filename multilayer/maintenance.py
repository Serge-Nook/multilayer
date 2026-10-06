import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from multilayer.model import MultilayerError


def install_appimage(source: Path) -> Path:
    if not source.is_file():
        raise MultilayerError("AppImage не найден")
    base = Path.home() / ".local"
    target = base / "opt/multilayer/Multilayer.AppImage"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        fd, temporary = tempfile.mkstemp(dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            Path(temporary).chmod(0o755)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    applications = base / "share/applications"
    applications.mkdir(parents=True, exist_ok=True)
    quoted = (
        str(target)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    (applications / "multilayer.desktop").write_text(
        f'[Desktop Entry]\nType=Application\nName=Мультислой\nComment=Виртуальные машины QEMU/KVM\nExec="{quoted}" --gui\nTerminal=false\nCategories=System;Emulator;\n',
        encoding="utf-8",
    )
    return target


def uninstall_appimage() -> None:
    target = Path.home() / ".local/opt/multilayer/Multilayer.AppImage"
    target.unlink(missing_ok=True)
    (Path.home() / ".local/share/applications/multilayer.desktop").unlink(missing_ok=True)


def maintain(action: str) -> str:
    image = os.environ.get("APPIMAGE")
    if image:
        if action == "install":
            install_appimage(Path(image))
            return "Приложение установлено/обновлено в ~/.local/opt/multilayer. ВМ сохранены."
        uninstall_appimage()
        return "Приложение удалено. Данные виртуальных машин сохранены."
    if sys.platform == "linux":
        pkexec = shutil.which("pkexec")
        apt = shutil.which("apt-get")
        if not pkexec or not apt:
            raise MultilayerError(
                "Используйте установочный AppImage или пакетный менеджер вашей ОС"
            )
        arguments = [
            pkexec,
            apt,
            "install" if action == "install" else "remove",
            "-y",
            "multilayer",
        ]
        result = subprocess.run(arguments, check=False)
        if result.returncode:
            raise MultilayerError(
                "Пакетный менеджер не завершил операцию. Для установки новой версии откройте скачанный DEB в менеджере пакетов."
            )
        return "Операция завершена. Виртуальные машины не удаляются."
    raise MultilayerError("Для установки/удаления в Windows запустите Multilayer-Setup.exe")
