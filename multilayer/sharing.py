import os
import tempfile
from pathlib import Path

from multilayer.model import MultilayerError


def folder_iso(folder: Path, destination: Path) -> None:
    import pycdlib

    folder = folder.resolve()
    if not folder.is_dir():
        raise MultilayerError("Папка обмена не найдена")
    entries: list[tuple[Path, bool]] = []
    total = 0
    for parent, directories, files in os.walk(folder, followlinks=False):
        for name in sorted(directories + files):
            path = Path(parent) / name
            if path.is_symlink():
                raise MultilayerError(
                    f"Папка обмена содержит ссылку: {path}. Ссылки не копируются."
                )
            if not path.is_file() and not path.is_dir():
                raise MultilayerError(f"Необычный тип файла: {path}")
            if len(name) > 64:
                raise MultilayerError(f"Joliet поддерживает имена до 64 символов: {name}")
            entries.append((path, path.is_dir()))
            if path.is_file():
                total += path.stat().st_size
                if total > 8 * 1024**3:
                    raise MultilayerError("Папка обмена больше 8 ГБ; выберите меньшую папку")
    entries.sort(key=lambda item: (len(item[0].relative_to(folder).parts), str(item[0])))
    iso = pycdlib.PyCdlib()
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".iso")
    os.close(fd)
    try:
        iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="MULTILAYER_SHARE")
        mapping = {folder: ""}
        for index, (path, directory) in enumerate(entries):
            identifier = f"D{index:07d}" if directory else f"F{index:07d}.DAT;1"
            iso_path = mapping[path.parent] + "/" + identifier
            joliet = "/" + path.relative_to(folder).as_posix()
            if directory:
                iso.add_directory(iso_path=iso_path, joliet_path=joliet, rr_name=path.name)
                mapping[path] = iso_path
            else:
                iso.add_file(str(path), iso_path=iso_path, joliet_path=joliet, rr_name=path.name)
        iso.write(temporary)
        os.replace(temporary, destination)
    except (OSError, pycdlib.pycdlibexception.PyCdlibException) as exc:
        raise MultilayerError(f"Не удалось собрать диск обмена: {exc}") from exc
    finally:
        iso.close()
        Path(temporary).unlink(missing_ok=True)
