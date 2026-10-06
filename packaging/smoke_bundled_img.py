import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main(cli: Path) -> None:
    cli = cli.resolve(strict=True)
    environment = dict(os.environ)
    environment["PATH"] = (
        str(Path(os.environ["SystemRoot"]) / "System32") if sys.platform == "win32" else ""
    )
    with tempfile.TemporaryDirectory() as temporary:

        def command(*arguments: str) -> str:
            return subprocess.check_output(
                [str(cli), "--home", temporary, *arguments],
                env=environment,
                text=True,
                encoding="utf-8",
                timeout=120,
            )

        diagnostic = json.loads(command("doctor"))
        tool = Path(diagnostic["dependencies"]["qemu-img"])
        if not tool.is_relative_to(cli.parent) or tool.parent.name != "bin":
            raise RuntimeError("Frozen application did not select its bundled qemu-img")
        vm = json.loads(command("create", "--name", "Bundled tool smoke", "--disk", "1"))
        identifier = vm["id"]
        command("snapshot", identifier, "create", "test")
        command("snapshot", identifier, "restore", "test", "--yes")
        command("clone", identifier, "--name", "Bundled clone")
        if len(json.loads(command("list"))) != 2:
            raise RuntimeError("Bundled qemu-img failed to create or clone a VM")
        print("Bundled qemu-img: create, snapshot, restore and clone passed without QEMU in PATH")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
