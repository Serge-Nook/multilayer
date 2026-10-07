import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from multilayer import APP_NAME, AUTHOR, WEBSITE, __version__
from multilayer.engine import Engine, accelerator, executable
from multilayer.i18n import language, set_language, tr
from multilayer.model import GUESTS, NETWORKS, VM, MultilayerError
from multilayer.store import Store


def vm_options(parser: argparse.ArgumentParser, update: bool = False) -> None:
    parser.add_argument("--name", required=not update)

    def localized_guest(value: str) -> str:
        return next((tr(guest) for guest in GUESTS if value in (guest, tr(guest))), value)

    parser.add_argument(
        "--guest",
        type=localized_guest,
        choices=[tr(guest) for guest in GUESTS],
        default=None if update else "Debian",
    )
    parser.add_argument("--memory", dest="memory_mb", type=int, default=None if update else 2048)
    parser.add_argument("--cpus", type=int, default=None if update else 2)
    if not update:
        parser.add_argument("--disk", dest="disk_gb", type=int, default=32)
    for name in (
        "iso",
        "drivers-iso",
        "shared-folder",
        "firmware-code",
        "firmware-vars",
        "tap",
        "dns",
    ):
        parser.add_argument(
            "--" + name, default=None if update else "1.1.1.1" if name == "dns" else ""
        )
    parser.add_argument("--network", choices=NETWORKS, default=None if update else "off")
    parser.add_argument(
        "--nic", choices=("e1000", "virtio-net-pci", "rtl8139"), default=None if update else "e1000"
    )
    parser.add_argument(
        "--accelerator", choices=("auto", "kvm", "whpx", "tcg"), default=None if update else "auto"
    )
    parser.add_argument("--firmware", choices=("bios", "uefi"), default=None if update else "bios")
    parser.add_argument("--boot", choices=("dvd", "disk"), default=None if update else "dvd")
    parser.add_argument(
        "--tpm", action=argparse.BooleanOptionalAction, default=None if update else False
    )
    parser.add_argument(
        "--secure-boot", action=argparse.BooleanOptionalAction, default=None if update else False
    )
    parser.add_argument(
        "--lan-cidr", dest="lan_cidrs", action="append", default=None if update else []
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="multilayer", description=f"{tr(APP_NAME)} — {tr(AUTHOR)}, {WEBSITE}"
    )
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--lang", choices=("ru", "en"), default=language())
    result.add_argument("--home", type=Path, help=tr("Каталог данных ВМ"))
    result.add_argument("--gui", action="store_true", help=tr("Открыть графический интерфейс"))
    result.add_argument(
        "--maintenance",
        action="store_true",
        help=tr("Установка/обновление или удаление приложения"),
    )
    commands = result.add_subparsers(dest="command")
    commands.add_parser("list", help=tr("Список ВМ (JSON)"))
    commands.add_parser("doctor", help=tr("Диагностика зависимостей и ускорения"))
    export = commands.add_parser("export", help=tr("Экспорт ВМ"))
    export.add_argument("id")
    export.add_argument("file", type=Path)
    export.add_argument("--overwrite", action="store_true")
    imported = commands.add_parser("import", help=tr("Импорт ВМ"))
    imported.add_argument("file", type=Path)
    imported.add_argument("--name")
    create = commands.add_parser("create", help=tr("Создать ВМ и диск QCOW2"))
    vm_options(create)
    edit = commands.add_parser("edit", help=tr("Изменить выключенную ВМ"))
    edit.add_argument("id")
    vm_options(edit, update=True)
    for name in (
        "show",
        "status",
        "start",
        "shutdown",
        "stop",
        "pause",
        "resume",
        "reset",
        "delete",
        "clone",
        "link",
        "snapshot",
    ):
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "start":
            command.add_argument("--headless", action="store_true")
        elif name in ("stop", "reset", "delete"):
            command.add_argument(
                "--yes", action="store_true", help=tr("Подтвердить потенциальную потерю данных")
            )
        elif name == "clone":
            command.add_argument("--name", required=True)
        elif name == "link":
            command.add_argument("state", choices=("up", "down"))
        elif name == "snapshot":
            command.add_argument("action", choices=("list", "create", "restore", "delete"))
            command.add_argument("name", nargs="?", default="")
            command.add_argument("--yes", action="store_true")
    return result


def doctor() -> dict:
    dependencies: dict[str, str | None] = {}
    for name in (
        "qemu-system-x86_64",
        "qemu-img",
        "swtpm",
        "ip",
        "systemd-run",
        "bpftool",
        "pkexec",
    ):
        try:
            dependencies[name] = executable(name)
        except MultilayerError:
            dependencies[name] = None
    return {
        "platform": sys.platform,
        "architecture": "x86-64",
        "accelerator": accelerator(VM(name="diagnostic")),
        "kvm_accessible": os.access("/dev/kvm", os.R_OK | os.W_OK),
        "dependencies": dependencies,
        "network_policy_linux_only": True,
        "tpm_linux_only": True,
        "note": tr(
            "TCG — медленная эмуляция. В Linux предоставьте пользователю доступ к /dev/kvm. В Windows включите Windows Hypervisor Platform и VT-x/AMD-V в UEFI."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--lang", choices=("ru", "en"))
    selected, _ = preliminary.parse_known_args(argv)
    if selected.lang:
        set_language(selected.lang, persist=False)
    arguments = parser().parse_args(argv)
    try:
        if arguments.gui or arguments.maintenance or not arguments.command:
            from multilayer.gui import launch

            maintenance = arguments.maintenance or bool(
                os.environ.get("APPIMAGE") and not arguments.gui and not arguments.command
            )
            return launch(arguments.home, maintenance=maintenance)
        engine = Engine(Store(arguments.home))
        command = arguments.command
        data: Any
        if command == "doctor":
            data = doctor()
        elif command == "list":
            data = [{**vm.to_dict(), "status": engine.status(vm.id)} for vm in engine.store.list()]
        elif command == "export":
            from multilayer.portable import export_vm

            data = {
                "file": str(export_vm(engine, arguments.id, arguments.file, arguments.overwrite))
            }
        elif command == "import":
            from multilayer.portable import import_vm

            data = import_vm(engine, arguments.file, arguments.name).to_dict()
        elif command == "create":
            values = {
                key: value
                for key, value in vars(arguments).items()
                if key in VM.__dataclass_fields__
            }
            values["guest"] = next(guest for guest in GUESTS if tr(guest) == values["guest"])
            data = engine.create(VM(**values)).to_dict()
        elif command == "edit":
            values = engine.store.get(arguments.id).to_dict()
            values.update(
                {
                    key: value
                    for key, value in vars(arguments).items()
                    if key in VM.__dataclass_fields__ and value is not None
                }
            )
            values["guest"] = next(
                (guest for guest in GUESTS if tr(guest) == values["guest"]), values["guest"]
            )
            vm = VM(**values)
            engine.update(vm)
            data = vm.to_dict()
        elif command == "show":
            data = engine.store.get(arguments.id).to_dict()
        elif command == "status":
            data = {"status": engine.status(arguments.id)}
        elif command == "start":
            data = {"accelerator": engine.start(arguments.id, headless=arguments.headless)}
        elif command == "link":
            engine.link(arguments.id, arguments.state == "up")
            data = {"link": arguments.state}
        elif command == "snapshot":
            if arguments.action in ("restore", "delete") and not arguments.yes:
                raise MultilayerError(tr("Восстановление/удаление снимка требует --yes"))
            data = engine.snapshot(arguments.id, arguments.action, arguments.name)
        elif command == "clone":
            data = engine.clone(arguments.id, arguments.name).to_dict()
        else:
            if command in ("stop", "reset", "delete") and not arguments.yes:
                raise MultilayerError(
                    tr("Операция может привести к потере данных; подтвердите её флагом --yes")
                )
            if command == "delete":
                engine.delete(arguments.id)
            else:
                engine.control(arguments.id, command)
            data = {"ok": True}
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except (MultilayerError, OSError, ValueError) as exc:
        print(tr("Ошибка: {value0}", value0=exc), file=sys.stderr)
        return 1
