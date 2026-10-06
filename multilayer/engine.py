import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psutil

from multilayer.model import VM, MultilayerError
from multilayer.network import network_arguments, policy_command, verify_policy
from multilayer.qmp import QMP
from multilayer.sharing import folder_iso
from multilayer.store import Store, write_json


def executable(name: str) -> str:
    found = shutil.which(name)
    if not found and sys.platform == "win32":
        for directory in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "qemu",
            Path(sys.executable).parent / "qemu",
        ):
            candidate = directory / (name + ".exe")
            if candidate.is_file():
                found = str(candidate)
                break
    if not found:
        raise MultilayerError(
            f"Не найден {name}. Установите QEMU/зависимости и добавьте их в PATH."
        )
    return str(Path(found).resolve())


def run(arguments: list[str], timeout: int = 120) -> str:
    try:
        return subprocess.run(
            arguments,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=external_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        message = getattr(exc, "stderr", None) or str(exc)
        raise MultilayerError(message.strip()) from exc


def option_path(path: Path | str) -> str:
    return str(path).replace(",", ",,")


def external_env() -> dict[str, str]:
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        if original := env.get("LD_LIBRARY_PATH_ORIG"):
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def accelerator(vm: VM) -> str:
    if vm.accelerator != "auto":
        if vm.accelerator == "kvm" and sys.platform != "linux":
            raise MultilayerError("KVM доступен только в Linux")
        if vm.accelerator == "whpx" and sys.platform != "win32":
            raise MultilayerError("WHPX доступен только в Windows")
        return vm.accelerator
    if sys.platform == "linux" and os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "kvm"
    return "whpx" if sys.platform == "win32" else "tcg"


def find_firmware(vm: VM) -> tuple[Path, Path]:
    if vm.firmware_code and vm.firmware_vars:
        return Path(vm.firmware_code), Path(vm.firmware_vars)
    bases = [Path("/usr/share/OVMF"), Path("/usr/share/edk2/x64"), Path("/usr/share/edk2/ovmf")]
    if sys.platform == "win32":
        bases.append(Path(executable("qemu-system-x86_64")).parent / "share")
    pairs = (
        [
            ("OVMF_CODE_4M.secboot.fd", "OVMF_VARS_4M.ms.fd"),
            ("OVMF_CODE.secboot.fd", "OVMF_VARS.ms.fd"),
        ]
        if vm.secure_boot
        else [("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"), ("OVMF_CODE.fd", "OVMF_VARS.fd")]
    )
    for base in bases:
        for code, variables in pairs:
            if (base / code).is_file() and (base / variables).is_file():
                return base / code, base / variables
    raise MultilayerError(
        "Не найдена пара UEFI CODE/VARS. Установите OVMF или укажите оба файла прошивки. Для Secure Boot используйте прошивку с ключами Microsoft."
    )


class Engine:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.children: dict[str, subprocess.Popen] = {}
        self.tpm_children: dict[str, subprocess.Popen] = {}

    def endpoint(self, vm: VM) -> str:
        if sys.platform == "win32":
            return rf"\\.\pipe\multilayer-{vm.id}"
        path = str(self.store.path(vm.id) / "qmp.sock")
        if len(path.encode()) > 100:
            raise MultilayerError("Путь данных слишком длинный для сокета QMP (максимум 100 байт)")
        return path

    def status(self, identifier: str) -> str:
        vm = self.store.get(identifier)
        try:
            with QMP(self.endpoint(vm), timeout=0.5) as qmp:
                return qmp.execute("query-status")["status"]
        except (OSError, ValueError, MultilayerError, TimeoutError):
            if self._alive(vm):
                return "unreachable"
            return "shutdown" if self._alive(vm, tpm=True) else "stopped"

    def _alive(self, vm: VM, tpm: bool = False) -> bool:
        try:
            runtime = json.loads((self.store.path(vm.id) / "runtime.json").read_text())
            prefix = "tpm_" if tpm else ""
            pid = runtime.get(prefix + "pid")
            if pid is None:
                return False
            process = psutil.Process(pid)
            if abs(process.create_time() - runtime[prefix + "created"]) > 0.01:
                return False
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (OSError, ValueError, KeyError, psutil.NoSuchProcess):
            return False
        except psutil.AccessDenied:
            return True

    def require_stopped(self, vm: VM) -> None:
        if self.status(vm.id) != "stopped":
            raise MultilayerError("Сначала полностью выключите ВМ")

    def create(self, vm: VM) -> VM:
        vm.validate()
        directory = self.store.path(vm.id)
        if directory.exists():
            raise MultilayerError("ВМ с таким ID уже существует")
        directory.mkdir(mode=0o700)
        try:
            run(
                [
                    executable("qemu-img"),
                    "create",
                    "-f",
                    "qcow2",
                    str(directory / "disk.qcow2"),
                    f"{vm.disk_gb}G",
                ]
            )
            self.store.save(vm)
            return vm
        except Exception:
            shutil.rmtree(directory)
            raise

    def update(self, vm: VM) -> None:
        with self.store.lock(vm.id):
            self.require_stopped(vm)
            previous = self.store.get(vm.id)
            if previous.disk_gb != vm.disk_gb:
                raise MultilayerError(
                    "Изменение размера диска пока не поддерживается; создайте новую ВМ"
                )
            self.store.save(vm)

    def command(self, vm: VM, headless: bool = False) -> list[str]:
        vm.validate()
        directory = self.store.path(vm.id)
        accel = accelerator(vm)
        args = [
            executable("qemu-system-x86_64"),
            "-name",
            option_path(vm.name),
            "-uuid",
            vm.id,
            "-machine",
            "q35,smm=on" if vm.secure_boot else "q35",
            "-accel",
            accel,
            "-cpu",
            "host" if accel == "kvm" else "max" if accel == "tcg" else "qemu64",
            "-m",
            str(vm.memory_mb),
            "-smp",
            str(vm.cpus),
            "-S",
            "-drive",
            f"file={option_path(directory / 'disk.qcow2')},format=qcow2,if=ide,id=disk0",
            "-device",
            "qemu-xhci",
            "-device",
            "usb-tablet",
            "-vga",
            "std",
            "-boot",
            "order=dc,menu=on" if vm.boot == "dvd" else "order=cd,menu=on",
            "-monitor",
            "none",
            "-serial",
            f"file:{directory / 'serial.log'}",
            "-display",
            "none" if headless else "sdl" if sys.platform == "win32" else "gtk",
        ]
        endpoint = self.endpoint(vm)
        if sys.platform == "win32":
            args.extend(
                [
                    "-chardev",
                    f"pipe,id=qmp,path=multilayer-{vm.id}",
                    "-mon",
                    "chardev=qmp,mode=control",
                ]
            )
        else:
            args.extend(["-qmp", f"unix:{option_path(endpoint)},server=on,wait=off"])
        args.extend(network_arguments(vm))
        for number, image in enumerate(
            (vm.iso, vm.drivers_iso, str(directory / "share.iso") if vm.shared_folder else "")
        ):
            args.extend(
                [
                    "-drive",
                    "if=none,media=cdrom,readonly=on,id=cd"
                    + str(number)
                    + (f",file={option_path(image)}" if image else ""),
                    "-device",
                    f"ide-cd,drive=cd{number},bus=ide.{number + 1},unit=0,id=dvd{number}",
                ]
            )
        if vm.firmware == "uefi":
            code, _ = find_firmware(vm)
            args.extend(
                [
                    "-drive",
                    f"if=pflash,format=raw,readonly=on,file={option_path(code)}",
                    "-drive",
                    f"if=pflash,format=raw,file={option_path(directory / 'uefi-vars.fd')}",
                ]
            )
            if vm.secure_boot:
                args.extend(["-global", "driver=cfi.pflash01,property=secure,value=on"])
        if vm.tpm:
            args.extend(
                [
                    "-chardev",
                    f"socket,id=chrtpm,path={option_path(directory / 'swtpm.sock')}",
                    "-tpmdev",
                    "emulator,id=tpm0,chardev=chrtpm",
                    "-device",
                    "tpm-tis,tpmdev=tpm0",
                ]
            )
        return args

    def _prepare(self, vm: VM) -> subprocess.Popen | None:
        directory = self.store.path(vm.id)
        if vm.firmware == "uefi":
            _, variables = find_firmware(vm)
            if not (directory / "uefi-vars.fd").exists():
                shutil.copyfile(variables, directory / "uefi-vars.fd")
        if vm.shared_folder:
            folder_iso(Path(vm.shared_folder), directory / "share.iso")
        if not vm.tpm:
            return None
        if sys.platform != "linux":
            raise MultilayerError(
                "Виртуальный TPM этой версии требует Linux и swtpm; Windows 11 на Windows-хосте пока не поддерживается"
            )
        tpm = directory / "tpm"
        tpm.mkdir(exist_ok=True, mode=0o700)
        endpoint = directory / "swtpm.sock"
        endpoint.unlink(missing_ok=True)
        with (directory / "swtpm.log").open("ab") as log:
            process = subprocess.Popen(
                [
                    executable("swtpm"),
                    "socket",
                    "--tpm2",
                    "--tpmstate",
                    f"dir={tpm}",
                    "--ctrl",
                    f"type=unixio,path={endpoint}",
                    "--terminate",
                    "--flags",
                    "not-need-init",
                ],
                stdout=log,
                stderr=log,
                env=external_env(),
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if endpoint.exists():
                return process
            if process.poll() is not None:
                break
            time.sleep(0.05)
        process.terminate()
        process.wait(timeout=5)
        raise MultilayerError("Не удалось запустить TPM; смотрите swtpm.log")

    def start(self, identifier: str, headless: bool = False) -> str:
        with self.store.lock(identifier):
            vm = self.store.get(identifier)
            self.require_stopped(vm)
            vm.validate()
            command = self.command(vm, headless)
            command, unit = policy_command(vm, command, self.store.path(vm.id))
            tpm = self._prepare(vm)
            directory = self.store.path(vm.id)
            if sys.platform != "win32":
                Path(self.endpoint(vm)).unlink(missing_ok=True)
            process = None
            try:
                with (directory / "qemu.log").open("ab") as log:
                    process = subprocess.Popen(
                        command,
                        stdout=log,
                        stderr=log,
                        start_new_session=sys.platform != "win32",
                        env=external_env(),
                    )
                if unit:
                    try:
                        code = process.wait(timeout=120)
                    except subprocess.TimeoutExpired as exc:
                        raise MultilayerError("Истёк срок авторизации сетевой политики") from exc
                    if code != 0:
                        raise MultilayerError("Запуск systemd/firewall отменён; смотрите qemu.log")
                pid = (
                    int(
                        run(
                            [executable("systemctl"), "show", unit, "--property=MainPID", "--value"]
                        ).strip()
                    )
                    if unit
                    else process.pid
                )
                write_json(
                    directory / "runtime.json",
                    {
                        "pid": pid,
                        "created": psutil.Process(pid).create_time(),
                        "unit": unit,
                        "accelerator": accelerator(vm),
                        "tpm_pid": tpm.pid if tpm else None,
                        "tpm_created": psutil.Process(tpm.pid).create_time() if tpm else None,
                    },
                )
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with QMP(self.endpoint(vm), timeout=1) as qmp:
                            if unit:
                                verify_policy(unit)
                            qmp.execute("cont")
                        break
                    except (OSError, TimeoutError):
                        if time.monotonic() >= deadline or (
                            not unit and process.poll() is not None
                        ):
                            tail = (directory / "qemu.log").read_text(errors="replace")[-2000:]
                            raise MultilayerError("QEMU не запущен:\n" + tail) from None
                        time.sleep(0.1)
                self.children[vm.id] = process
                if tpm:
                    self.tpm_children[vm.id] = tpm
                return accelerator(vm)
            except Exception:
                try:
                    with QMP(self.endpoint(vm), timeout=1) as qmp:
                        qmp.execute("quit")
                except Exception:
                    if process and not unit and process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                if tpm and tpm.poll() is None:
                    tpm.terminate()
                    tpm.wait(timeout=5)
                raise

    def control(self, identifier: str, action: str) -> None:
        commands = {
            "shutdown": "system_powerdown",
            "stop": "quit",
            "pause": "stop",
            "resume": "cont",
            "reset": "system_reset",
        }
        if action not in commands:
            raise MultilayerError("Неизвестная команда управления")
        vm = self.store.get(identifier)
        with self.store.lock(identifier), QMP(self.endpoint(vm)) as qmp:
            qmp.execute(commands[action])
        if action == "stop":
            deadline = time.monotonic() + 10
            while self.status(identifier) != "stopped" and time.monotonic() < deadline:
                time.sleep(0.1)
            if child := self.children.pop(identifier, None):
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise MultilayerError("QEMU ещё завершает работу") from exc
            if tpm := self.tpm_children.pop(identifier, None):
                try:
                    tpm.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise MultilayerError("TPM ещё сохраняет состояние") from exc
            if self.status(identifier) != "stopped":
                raise MultilayerError("ВМ ещё завершает работу")

    def link(self, identifier: str, enabled: bool) -> None:
        vm = self.store.get(identifier)
        if vm.network == "off":
            raise MultilayerError("У ВМ нет сетевого адаптера")
        with self.store.lock(identifier), QMP(self.endpoint(vm)) as qmp:
            qmp.execute("set_link", {"name": "nic0", "up": enabled})

    def delete(self, identifier: str) -> None:
        vm = self.store.get(identifier)
        with self.store.lock(identifier):
            self.require_stopped(vm)
            (self.store.path(identifier) / ".deleting").touch()
        self.store.remove(identifier)

    def clone(self, identifier: str, name: str) -> VM:
        with self.store.lock(identifier):
            source = self.store.get(identifier)
            self.require_stopped(source)
            target = VM(**{**source.to_dict(), "id": str(uuid4()), "name": name})
            target.validate()
            directory = self.store.path(target.id)
            directory.mkdir(mode=0o700)
            try:
                run(
                    [
                        executable("qemu-img"),
                        "convert",
                        "-O",
                        "qcow2",
                        str(self.store.path(source.id) / "disk.qcow2"),
                        str(directory / "disk.qcow2"),
                    ],
                    timeout=3600,
                )
                for name in ("uefi-vars.fd", "tpm"):
                    path = self.store.path(source.id) / name
                    if path.is_dir():
                        shutil.copytree(path, directory / name)
                    elif path.is_file():
                        shutil.copyfile(path, directory / name)
                self.store.save(target)
                return target
            except Exception:
                shutil.rmtree(directory)
                raise

    def snapshot(self, identifier: str, action: str, name: str = "") -> list[dict]:
        with self.store.lock(identifier):
            vm = self.store.get(identifier)
            self.require_stopped(vm)
            directory = self.store.path(identifier)
            image = str(directory / "disk.qcow2")
            if action == "list":
                return json.loads(
                    run([executable("qemu-img"), "info", "--output=json", image])
                ).get("snapshots", [])
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
                raise MultilayerError("Имя снимка: 1–48 латинских букв, цифр, _ или -")
            if action not in ("create", "restore", "delete"):
                raise MultilayerError("Неизвестная операция со снимком")
            snapshot = directory / "snapshots" / name
            if action == "create":
                if snapshot.exists():
                    raise MultilayerError("Снимок уже существует")
                snapshot.mkdir(parents=True, mode=0o700)
                try:
                    for item in ("uefi-vars.fd", "tpm", "vm.json"):
                        source = directory / item
                        if source.is_dir():
                            shutil.copytree(source, snapshot / item)
                        elif source.is_file():
                            shutil.copyfile(source, snapshot / item)
                    run([executable("qemu-img"), "snapshot", "-c", name, image])
                except Exception:
                    shutil.rmtree(snapshot)
                    raise
            else:
                if not snapshot.is_dir():
                    raise MultilayerError("Снимок не найден")
                run(
                    [
                        executable("qemu-img"),
                        "snapshot",
                        "-a" if action == "restore" else "-d",
                        name,
                        image,
                    ]
                )
                if action == "restore":
                    for item in ("uefi-vars.fd", "tpm", "vm.json"):
                        source = snapshot / item
                        target = directory / item
                        if target.is_dir():
                            shutil.rmtree(target)
                        elif target.is_file():
                            target.unlink()
                        if source.is_dir():
                            shutil.copytree(source, target)
                        elif source.is_file():
                            shutil.copyfile(source, target)
                else:
                    shutil.rmtree(snapshot)
            return []
