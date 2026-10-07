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
        raise RuntimeError(str(binary) + " has missing shared libraries")
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
                    raise RuntimeError("Missing runtime DLL: " + name)
    return list(libraries.values())


def linux_licenses(files: list[Path], directory: Path) -> dict[str, str]:
    packages = set()
    for path in files:
        for candidate in (
            path,
            path.resolve(),
            Path("/usr" + str(path)),
            Path(str(path).removeprefix("/usr")),
        ):
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


def copy_tree(source: Path, target: Path) -> list[Path]:
    source = source.resolve(strict=True)
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            files.append(path)
    return files


def export_firmware(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    files = copy_tree(Path("/usr/share/OVMF"), target / "uefi")
    licenses = target / "licenses"
    licenses.mkdir(exist_ok=True)
    sources = linux_licenses(files, licenses)
    (licenses / "sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")


def elf_binary(name: str) -> Path:
    found = shutil.which(name)
    if not found:
        raise RuntimeError("Missing build-time component: " + name)
    binary = Path(found).resolve()
    if binary.read_bytes()[:4] != b"\x7fELF" and name == "bpftool":
        candidates = sorted(Path("/usr/lib").glob("linux-tools*/**/bpftool"))
        for candidate in reversed(candidates):
            if candidate.is_file() and candidate.read_bytes()[:4] == b"\x7fELF":
                return candidate.resolve()
    if binary.read_bytes()[:4] != b"\x7fELF":
        raise RuntimeError("Cannot bundle a system wrapper instead of ELF: " + str(binary))
    return binary


def linux_modules(library_root: Path = Path("/usr/lib")) -> list[Path]:
    modules = []
    # SDL's OpenGL symbols live in another QEMU module, not in an ldd dependency.
    for name in ("accel-tcg-x86_64.so", "ui-opengl.so", "ui-sdl.so"):
        matches = list(library_root.glob("*/qemu/" + name))
        if len(matches) != 1:
            raise RuntimeError("Missing or ambiguous QEMU module: " + name)
        modules.append(matches[0])
    return modules


def bundle(binary: Path, target: Path, firmware: Path | None = None) -> None:
    binary = binary.resolve(strict=True)
    shutil.rmtree(target, ignore_errors=True)
    (target / "bin").mkdir(parents=True)
    (target / "lib").mkdir()
    (target / "modules").mkdir()
    licenses = target / "licenses"
    licenses.mkdir()
    shutil.copy2(binary, target / "bin" / binary.name)
    if sys.platform == "win32":
        system = binary.parent / "qemu-system-x86_64.exe"
        shutil.copy2(system, target / "bin" / system.name)
        dlls = {
            path.name.lower(): path for tool in (binary, system) for path in windows_libraries(tool)
        }
        # Modules may be loaded dynamically rather than listed in PE imports.
        for module in binary.parent.glob("*.dll"):
            if module.name.startswith(("accel-", "ui-", "audio-", "hw-")):
                shutil.copy2(module, target / "modules" / module.name)
                for dependency in windows_libraries(module):
                    dlls[dependency.name.lower()] = dependency
        libraries = list(dlls.values())
        for library in libraries:
            shutil.copy2(library, target / "bin" / library.name)
        copy_tree(binary.parent / "share", target / "share")
        if firmware is None:
            raise RuntimeError("Windows packaging requires exported OVMF firmware")
        copy_tree(firmware / "uefi", target / "uefi")
        copy_tree(firmware / "licenses", licenses)
        for name in ("COPYING", "COPYING.LIB"):
            shutil.copy2(binary.parent / name, licenses / name)
        sources = json.loads((licenses / "sources.json").read_text(encoding="utf-8"))
        sources["qemu-windows"] = "https://qemu.weilnetz.de/w64/"
        source_note = "Windows build sources and build scripts: https://github.com/stweil/qemu\nDependencies: https://github.com/msys2/MINGW-packages\n"
    else:
        tools = [
            binary,
            *(elf_binary(name) for name in ("qemu-system-x86_64", "swtpm", "ip", "bpftool")),
        ]
        for tool in tools[1:]:
            name = "bpftool" if tool.name == "bpftool" else tool.name
            shutil.copy2(tool, target / "bin" / name)
        modules = linux_modules()
        for module in modules:
            shutil.copy2(module, target / "modules" / module.name)
        libraries = sorted({path for tool in [*tools, *modules] for path in linux_libraries(tool)})
        for library in libraries:
            shutil.copy2(library, target / "lib" / library.name)
        data = []
        for source in (
            Path("/usr/share/qemu"),
            Path("/usr/share/seabios"),
            Path("/usr/lib/ipxe/qemu"),
        ):
            data.extend(copy_tree(source, target / "share"))
        data.extend(copy_tree(Path("/usr/share/OVMF"), target / "uefi"))
        sources = linux_licenses([*tools, *modules, *libraries, *data], licenses)
        source_note = "Ubuntu source packages (versions in sources.json): https://archive.ubuntu.com/ubuntu/pool/\nBuild with the source package's debian/rules; install runtime via apt install qemu-utils.\n"
    (licenses / "sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
    version = subprocess.check_output([str(binary), "--version"], text=True).splitlines()[0]
    (licenses / "NOTICE.txt").write_text(
        version
        + "\nBundled QEMU, firmware and runtime dependencies are unmodified third-party software, not licensed as Multilayer.\n"
        "QEMU: https://www.qemu.org/ — GPL-2.0; see bundled copyright/license notices.\n"
        + source_note,
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--target", type=Path, default=Path("build/qemu-runtime"))
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--firmware-only", action="store_true")
    args = parser.parse_args()
    if args.firmware_only:
        export_firmware(args.target)
    elif args.binary:
        bundle(args.binary, args.target, args.firmware)
    else:
        parser.error("--binary is required unless --firmware-only is used")
