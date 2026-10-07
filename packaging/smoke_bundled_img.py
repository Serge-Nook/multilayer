import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil


def main(cli: Path) -> None:
    cli = cli.resolve(strict=True)
    launcher = [str(cli)]
    if cli.suffix == ".AppImage":
        launcher.append("--appimage-extract-and-run")
    environment = dict(os.environ)
    environment["PATH"] = (
        str(Path(os.environ["SystemRoot"]) / "System32") if sys.platform == "win32" else ""
    )
    graphical_environment = dict(environment, SDL_RENDER_DRIVER="software")
    if sys.platform == "linux" and not any(
        environment.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY")
    ):
        graphical_environment["SDL_VIDEODRIVER"] = "dummy"
    with tempfile.TemporaryDirectory() as temporary:

        def command(*arguments: str, graphical: bool = False) -> str:
            return subprocess.check_output(
                [*launcher, "--home", temporary, *arguments],
                env=graphical_environment if graphical else environment,
                text=True,
                encoding="utf-8",
                timeout=120,
            )

        diagnostic = json.loads(command("doctor"))
        tool = Path(diagnostic["dependencies"]["qemu-img"])
        if tool.parent.name != "bin" or tool.parent.parent.name != "qemu":
            raise RuntimeError("Frozen application did not select its bundled qemu-img")
        required = ["qemu-system-x86_64"]
        if sys.platform == "linux":
            required.extend(["swtpm", "ip", "bpftool"])
        for name in required:
            dependency = Path(diagnostic["dependencies"][name])
            if dependency.parent.name != "bin" or dependency.parent.parent.name != "qemu":
                raise RuntimeError("Frozen application did not select bundled " + name)
        vm = json.loads(
            command("create", "--name", "Bundled tool smoke", "--disk", "1", "--accelerator", "tcg")
        )
        identifier = vm["id"]
        command("snapshot", identifier, "create", "test")
        command("snapshot", identifier, "restore", "test", "--yes")
        command("clone", identifier, "--name", "Bundled clone")
        if len(json.loads(command("list"))) != 2:
            raise RuntimeError("Bundled qemu-img failed to create or clone a VM")
        command("start", identifier, "--headless")
        if json.loads(command("status", identifier))["status"] != "running":
            raise RuntimeError("Bundled QEMU did not start the VM")
        command("pause", identifier)
        if json.loads(command("status", identifier))["status"] != "paused":
            raise RuntimeError("Bundled QEMU did not pause the VM")
        command("resume", identifier)
        if json.loads(command("status", identifier))["status"] != "running":
            raise RuntimeError("Bundled QEMU did not resume the VM")
        command("stop", identifier, "--yes")
        command("start", identifier, "--headless")
        command("stop", identifier, "--yes")
        command("start", identifier, graphical=True)
        try:
            if json.loads(command("status", identifier))["status"] != "running":
                raise RuntimeError("Bundled QEMU did not start its SDL display")
            if sys.platform == "linux":
                runtime = json.loads((Path(temporary) / identifier / "runtime.json").read_text())
                process = psutil.Process(runtime["pid"])
                root = Path(process.exe()).parent.parent
                loaded = {Path(mapping.path) for mapping in process.memory_maps()}
                for name in ("ui-opengl.so", "ui-sdl.so"):
                    if root / "modules" / name not in loaded:
                        raise RuntimeError("SDL used a missing or host QEMU module: " + name)
            command("pause", identifier)
            command("resume", identifier)
        finally:
            command("stop", identifier, "--yes")
        firmware_arguments = [
            "create",
            "--name",
            "UEFI smoke",
            "--disk",
            "1",
            "--firmware",
            "uefi",
            "--accelerator",
            "tcg",
        ]
        if sys.platform == "linux":
            firmware_arguments.extend(["--tpm", "--secure-boot"])
        uefi = json.loads(command(*firmware_arguments))
        command("start", uefi["id"], "--headless")
        command("stop", uefi["id"], "--yes")
        archive = str(Path(temporary) / "transfer.multis")
        command("export", uefi["id"], archive)
        imported = json.loads(command("import", archive))
        if imported["id"] == uefi["id"] or imported["firmware"] != "uefi":
            raise RuntimeError("Bundled import did not preserve firmware or prevent VM overwrite")
        command("edit", imported["id"], "--accelerator", "tcg")
        command("start", imported["id"], "--headless")
        command("stop", imported["id"], "--yes")
        transfer = str(Path(temporary) / "snapshot.multis")
        command("export", identifier, transfer)
        restored = json.loads(command("import", transfer))
        command("snapshot", restored["id"], "restore", "test", "--yes")
        if json.loads(command("show", restored["id"]))["id"] != restored["id"]:
            raise RuntimeError("Imported snapshot restored the original VM identifier")
        help_text = command("--lang", "en", "--help")
        if "VM data directory" not in help_text or "Multilayer" not in help_text:
            raise RuntimeError("Bundled English CLI translations are missing")
        print(
            "Bundled QEMU/SDL, English CLI and .multis disk/snapshot/UEFI/TPM transfer passed without tools in PATH"
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
