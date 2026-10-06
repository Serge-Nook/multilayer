import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def linux_libraries(binary: Path) -> list[Path]:
    result = subprocess.run(["ldd", str(binary)], check=True, capture_output=True, text=True)
    if "not found" in result.stdout:
        raise RuntimeError("qemu-img has missing shared libraries")
    libraries = []
    for name, path in re.findall(r"^\s*(\S+) => (/\S+) \(", result.stdout, re.MULTILINE):
        if name not in {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1"}:
            libraries.append(Path(path))
    return libraries


def windows_libraries(binary: Path) -> list[Path]:
    import pefile

    available = {path.name.lower(): path for path in binary.parent.glob("*.dll")}
    pending = [binary]
    libraries: dict[str, Path] = {}
    while pending:
        with pefile.PE(str(pending.pop())) as pe:
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) + getattr(
                pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []
            ):
                name = entry.dll.decode("ascii").lower()
                if name in libraries:
                    continue
                if name in available:
                    libraries[name] = available[name]
                    pending.append(available[name])
                elif (
                    not name.startswith(("api-ms-win-", "ext-ms-win-"))
                    and not (Path(os.environ["SystemRoot"]) / "System32" / name).is_file()
                ):
                    raise RuntimeError("Missing qemu-img DLL: " + name)
    return list(libraries.values())


def linux_licenses(files: list[Path], directory: Path) -> dict[str, str]:
    packages = set()
    for path in files:
        for candidate in (path, path.resolve(), Path("/usr" + str(path))):
            result = subprocess.run(
                ["dpkg-query", "-S", str(candidate)], capture_output=True, text=True
            )
            if result.returncode == 0:
                packages.add(result.stdout.split(": ", 1)[0])
                break
        else:
            raise RuntimeError("Cannot identify the package for " + str(path))
    sources = {}
    for package in sorted(packages):
        copyright_file = Path("/usr/share/doc") / package.split(":")[0] / "copyright"
        shutil.copy2(copyright_file, directory / (package.replace(":", "-") + "-copyright"))
        source, version = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${source:Package} ${source:Version}", package], text=True
        ).split()
        sources[source] = version
    return sources


def bundle(binary: Path, target: Path) -> None:
    binary = binary.resolve(strict=True)
    shutil.rmtree(target, ignore_errors=True)
    (target / "bin").mkdir(parents=True)
    (target / "lib").mkdir()
    licenses = target / "licenses"
    licenses.mkdir()
    shutil.copy2(binary, target / "bin" / binary.name)
    if sys.platform == "win32":
        libraries = windows_libraries(binary)
        for library in libraries:
            shutil.copy2(library, target / "bin" / library.name)
        for name in ("COPYING", "COPYING.LIB"):
            shutil.copy2(binary.parent / name, licenses / name)
        sources = {"qemu-windows": "https://qemu.weilnetz.de/w64/"}
        source_note = "Windows build sources and build scripts: https://github.com/stweil/qemu\nDependencies: https://github.com/msys2/MINGW-packages\n"
    else:
        libraries = linux_libraries(binary)
        for library in libraries:
            shutil.copy2(library, target / "lib" / library.name)
        sources = linux_licenses([binary, *libraries], licenses)
        source_note = "Ubuntu source packages (versions in sources.json): https://archive.ubuntu.com/ubuntu/pool/\nBuild with the source package's debian/rules; install runtime via apt install qemu-utils.\n"
    (licenses / "sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
    version = subprocess.check_output([str(binary), "--version"], text=True).splitlines()[0]
    (licenses / "NOTICE.txt").write_text(
        version + "\nqemu-img is unmodified third-party software, not licensed as Multilayer.\n"
        "QEMU: https://www.qemu.org/ — GPL-2.0; see bundled copyright/license notices.\n"
        + source_note,
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path("build/qemu-runtime"))
    args = parser.parse_args()
    bundle(args.binary, args.target)
