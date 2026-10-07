import errno
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from multilayer import __version__
from multilayer.engine import Engine, executable, run
from multilayer.i18n import tr
from multilayer.model import VM, MultilayerError
from multilayer.store import write_json

FORMAT_VERSION = 1
MANIFEST_LIMIT = 4 * 1024**2
FILE_LIMIT = 10000
CHUNK = 1024**2
SNAPSHOT = r"[A-Za-z0-9_-]{1,48}"
TPM_FILE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"


def fail() -> MultilayerError:
    return MultilayerError(tr("Некорректный или повреждённый архив .multis"))


def allowed(name: str) -> bool:
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(10)),
        *(f"lpt{i}" for i in range(10)),
    }
    if any(
        part.endswith(".") or part.split(".")[0].lower() in reserved for part in name.split("/")
    ):
        return False
    return bool(
        name in ("disk.qcow2", "uefi-vars.fd", "firmware/code.fd", "firmware/vars.fd")
        or re.fullmatch(r"tpm/" + TPM_FILE, name)
        or re.fullmatch(r"snapshots/" + SNAPSHOT + r"/(vm.json|uefi-vars.fd)", name)
        or re.fullmatch(r"snapshots/" + SNAPSHOT + r"/tpm/" + TPM_FILE, name)
        or re.fullmatch(r"snapshots/" + SNAPSHOT + r"/firmware/(code|vars).fd", name)
    )


def check_disk(path: Path) -> None:
    with path.open("rb") as source:
        header = source.read(80)
    if len(header) < 72 or header[:4] != b"QFI\xfb":
        raise fail()
    version = struct.unpack(">I", header[4:8])[0]
    if version not in (2, 3) or (version == 3 and len(header) < 80):
        raise fail()
    if struct.unpack(">Q", header[8:16])[0] or (
        version == 3 and struct.unpack(">Q", header[72:80])[0] & 4
    ):
        raise MultilayerError(tr("Диск с внешним backing/data-файлом нельзя переносить"))
    info = json.loads(
        run([executable("qemu-img"), "info", "-f", "qcow2", "--output=json", str(path)])
    )
    if info.get("backing-filename") or info.get("format-specific", {}).get("data", {}).get(
        "data-file"
    ):
        raise MultilayerError(tr("Диск с внешним backing/data-файлом нельзя переносить"))


def portable_config(vm: VM) -> dict:
    data = vm.to_dict()
    for key in ("iso", "drivers_iso", "shared_folder"):
        data[key] = ""
    return data


def export_vm(engine: Engine, identifier: str, destination: Path, overwrite: bool = False) -> Path:
    destination = destination.expanduser().absolute()
    if destination.suffix.lower() != ".multis":
        raise MultilayerError(tr("Расширение архива должно быть .multis"))
    if destination.exists() and not overwrite:
        raise MultilayerError(tr("Файл экспорта уже существует"))
    with engine.store.lock(identifier):
        vm = engine.store.get(identifier)
        engine.require_stopped(vm)
        directory = engine.store.path(identifier)
        sources: dict[str, Path | bytes] = {"disk.qcow2": directory / "disk.qcow2"}
        check_disk(directory / "disk.qcow2")

        def config(original: VM, prefix: str = "") -> dict:
            data = portable_config(original)
            for key, filename in (("firmware_code", "code.fd"), ("firmware_vars", "vars.fd")):
                if value := data[key]:
                    name = prefix + "firmware/" + filename
                    sources[name] = Path(value)
                    data[key] = name
            return data

        data = config(vm)
        for root in (directory / "uefi-vars.fd", directory / "tpm", directory / "snapshots"):
            if root.is_symlink():
                raise fail()
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")) if root.is_dir() else [root]:
                if path.is_symlink():
                    raise fail()
                if path.is_dir():
                    continue
                if path.name == ".lock":
                    continue
                name = path.relative_to(directory).as_posix()
                if not allowed(name):
                    raise fail()
                if path.name == "vm.json":
                    original = VM(**json.loads(path.read_text("utf-8")))
                    original.validate(check_files=False)
                    if original.id != vm.id:
                        raise fail()
                    sources[name] = json.dumps(
                        config(original, path.parent.relative_to(directory).as_posix() + "/"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                else:
                    sources[name] = path
        if len(sources) > FILE_LIMIT:
            raise fail()
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".multis-")
        os.close(fd)
        try:
            files = {}
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
            ) as archive:
                for name, source in sorted(sources.items()):
                    if not allowed(name):
                        raise fail()
                    checksum = hashlib.sha256()
                    size = 0
                    with archive.open(name, "w", force_zip64=True) as out:
                        if isinstance(source, bytes):
                            out.write(source)
                            checksum.update(source)
                            size = len(source)
                        else:
                            if not stat.S_ISREG(source.lstat().st_mode):
                                raise fail()
                            with source.open("rb") as incoming:
                                while chunk := incoming.read(CHUNK):
                                    out.write(chunk)
                                    checksum.update(chunk)
                                    size += len(chunk)
                    files[name] = {"size": size, "sha256": checksum.hexdigest()}
                manifest = {
                    "format": "multilayer",
                    "version": FORMAT_VERSION,
                    "application_version": __version__,
                    "architecture": "x86_64",
                    "vm": data,
                    "files": files,
                }
                encoded = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                if len(encoded) > MANIFEST_LIMIT:
                    raise fail()
                archive.writestr("manifest.json", encoded)
            with Path(temporary).open("rb") as handle:
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temporary, destination)
            else:
                try:
                    os.link(temporary, destination)
                except OSError as exc:
                    if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
                        raise
                    # FAT/exFAT cannot publish using a hard link.
                    with destination.open("xb"):
                        pass
                    try:
                        os.replace(temporary, destination)
                    except Exception:
                        destination.unlink(missing_ok=True)
                        raise
            return destination
        finally:
            Path(temporary).unlink(missing_ok=True)


def load_manifest(archive: zipfile.ZipFile) -> dict:
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if not 2 <= len(entries) <= FILE_LIMIT + 1 or len(set(n.lower() for n in names)) != len(names):
        raise fail()
    if "manifest.json" not in names:
        raise fail()
    for entry in entries:
        mode = entry.external_attr >> 16
        if (
            entry.flag_bits & 1
            or entry.is_dir()
            or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
        ):
            raise fail()
    directories: dict[str, str] = {}
    for name in names:
        parts = name.split("/")
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index])
            previous = directories.setdefault(directory.lower(), directory)
            if previous != directory:
                raise fail()
    info = archive.getinfo("manifest.json")
    if info.file_size > MANIFEST_LIMIT:
        raise fail()
    manifest = json.loads(archive.read(info))
    if not isinstance(manifest, dict) or manifest.get("format") != "multilayer":
        raise fail()
    if type(manifest.get("version")) is not int or manifest["version"] != FORMAT_VERSION:
        raise MultilayerError(tr("Эта версия формата .multis не поддерживается"))
    if manifest.get("architecture") != "x86_64":
        raise fail()
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or "disk.qcow2" not in files
        or set(files) != set(names) - {"manifest.json"}
    ):
        raise fail()
    for name, metadata in files.items():
        if not allowed(name) or not isinstance(metadata, dict):
            raise fail()
        if (
            type(metadata.get("size")) is not int
            or metadata["size"] != archive.getinfo(name).file_size
        ):
            raise fail()
        if not isinstance(metadata.get("sha256"), str) or not re.fullmatch(
            "[a-f0-9]{64}", metadata["sha256"]
        ):
            raise fail()
        if name.endswith("vm.json") and metadata["size"] > MANIFEST_LIMIT:
            raise fail()
    return manifest


def local_config(data: dict, identifier: str, directory: Path, files: dict) -> VM:
    vm = VM(**data)
    vm.validate(check_files=False)
    if vm.iso or vm.drivers_iso or vm.shared_folder:
        raise fail()
    vm.id = identifier
    vm.accelerator = "auto"
    for key in ("firmware_code", "firmware_vars"):
        if name := getattr(vm, key):
            if name not in files or not name.endswith(("/code.fd", "/vars.fd")):
                raise fail()
            setattr(vm, key, str(directory / name))
    return vm


def import_vm(engine: Engine, source: Path, name: str | None = None) -> VM:
    destination: Path | None = None
    try:
        with zipfile.ZipFile(source) as archive:
            manifest = load_manifest(archive)
            original = VM(**manifest["vm"])
            original.validate(check_files=False)
            identifier = original.id
            while engine.store.path(identifier).exists():
                identifier = str(uuid4())
            final = engine.store.path(identifier)
            files = manifest["files"]
            if (
                sum(item["size"] for item in files.values())
                > shutil.disk_usage(engine.store.root).free
            ):
                raise MultilayerError(tr("Недостаточно места для импорта ВМ"))
            vm = local_config(manifest["vm"], identifier, final, files)
            if name is not None:
                vm.name = name
            vm.validate(check_files=False)
            with tempfile.TemporaryDirectory(dir=engine.store.root, prefix=".import-") as temporary:
                staging = Path(temporary)
                for filename, metadata in files.items():
                    path = staging / filename
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    checksum = hashlib.sha256()
                    size = 0
                    with archive.open(filename) as incoming, path.open("xb") as out:
                        path.chmod(0o600)
                        while chunk := incoming.read(CHUNK):
                            size += len(chunk)
                            if size > metadata["size"]:
                                raise fail()
                            checksum.update(chunk)
                            out.write(chunk)
                    if size != metadata["size"] or checksum.hexdigest() != metadata["sha256"]:
                        raise fail()
                check_disk(staging / "disk.qcow2")
                for path in staging.glob("snapshots/*/vm.json"):
                    snapshot_data = json.loads(path.read_text("utf-8"))
                    if snapshot_data.get("id") != original.id:
                        raise fail()
                    snapshot = local_config(snapshot_data, identifier, final, files)
                    write_json(path, snapshot.to_dict())
                final.mkdir(mode=0o700)
                destination = final
                for path in staging.iterdir():
                    os.replace(path, final / path.name)
                engine.store.save(vm)
                destination = None
                return vm
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        zipfile.BadZipFile,
        RuntimeError,
    ) as exc:
        raise fail() from exc
    finally:
        if destination is not None:
            shutil.rmtree(destination)
