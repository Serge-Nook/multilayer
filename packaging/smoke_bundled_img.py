import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main(cli: Path) -> None:
    cli = cli.resolve(strict=True)
    launcher = [str(cli)]
    if cli.suffix == ".AppImage":
        launcher.append("--appimage-extract-and-run")
    environment = dict(os.environ)
    environment["PATH"] = (
        str(Path(os.environ["SystemRoot"]) / "System32") if sys.platform == "win32" else ""
    )
    with tempfile.TemporaryDirectory() as temporary:

        def command(*arguments: str) -> str:
            return subprocess.check_output(
                [*launcher, "--home", temporary, *arguments],
                env=environment,
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
        command("stop", identifier, "--yes")
        if sys.platform == "linux":
            uefi = json.loads(
                command(
                    "create",
                    "--name",
                    "UEFI TPM smoke",
                    "--disk",
                    "1",
                    "--firmware",
                    "uefi",
                    "--tpm",
                    "--secure-boot",
                    "--accelerator",
                    "tcg",
                )
            )
            command("start", uefi["id"], "--headless")
            command("stop", uefi["id"], "--yes")
        print("Bundled QEMU runtime passed without QEMU, firmware, TPM or tools in PATH")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
