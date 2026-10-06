import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.bundle = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "packaging/bundle_qemu.py")
        )
        self.libraries = self.bundle["linux_libraries"]

    def test_merged_usr_package_path_is_checked(self):
        responses = [subprocess.CompletedProcess([], 1, "") for _ in range(3)]
        responses.append(subprocess.CompletedProcess([], 0, "iproute2: /bin/ip\n"))
        with (
            patch("subprocess.run", side_effect=responses),
            patch("subprocess.check_output", return_value="iproute2 5.15.0"),
            patch("shutil.copy2") as copy,
        ):
            sources = self.bundle["linux_licenses"]([Path("/usr/bin/ip")], Path("licenses"))
            self.assertEqual(sources, {"iproute2": "5.15.0"})
            copy.assert_called_once()

    def test_unknown_package_blocks_packaging(self):
        with (
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "")),
            self.assertRaisesRegex(RuntimeError, "Cannot identify the package"),
        ):
            self.bundle["linux_licenses"]([Path("/unknown")], Path("licenses"))

    def test_linux_sdl_includes_its_opengl_module_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "x86_64-linux-gnu/qemu"
            directory.mkdir(parents=True)
            names = ("accel-tcg-x86_64.so", "ui-opengl.so", "ui-sdl.so")
            for name in names:
                (directory / name).touch()
            self.assertEqual(self.bundle["linux_modules"](root), [directory / n for n in names])

    def test_missing_opengl_module_blocks_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "x86_64-linux-gnu/qemu"
            directory.mkdir(parents=True)
            for name in ("accel-tcg-x86_64.so", "ui-sdl.so"):
                (directory / name).touch()
            with self.assertRaisesRegex(RuntimeError, "ui-opengl.so"):
                self.bundle["linux_modules"](root)

    def test_ambiguous_module_blocks_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for arch in ("arch-a", "arch-b"):
                directory = root / arch / "qemu"
                directory.mkdir(parents=True)
                (directory / "accel-tcg-x86_64.so").touch()
            with self.assertRaisesRegex(RuntimeError, "ambiguous QEMU module"):
                self.bundle["linux_modules"](root)

    def test_firmware_export_includes_source_metadata(self):
        export = self.bundle["export_firmware"]
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                export.__globals__,
                {
                    "copy_tree": Mock(return_value=[]),
                    "linux_licenses": Mock(return_value={"edk2": "version"}),
                },
            ),
        ):
            target = Path(temporary) / "firmware"
            export(target)
            self.assertEqual(
                json.loads((target / "licenses/sources.json").read_text()), {"edk2": "version"}
            )

    def test_runtime_tree_retains_nested_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            (source / "nested").mkdir(parents=True)
            (source / "nested/firmware.fd").write_bytes(b"firmware")
            target = Path(temporary) / "target"
            self.bundle["copy_tree"](source, target)
            self.assertEqual((target / "nested/firmware.fd").read_bytes(), b"firmware")

    def test_collects_transitive_libraries_but_not_host_glibc(self):
        output = """
        linux-vdso.so.1 (0x123)
        libglib-2.0.so.0 => /usr/lib/libglib-2.0.so.0 (0x123)
        libffi.so.8 => /usr/lib/libffi.so.8 (0x123)
        libc.so.6 => /lib/libc.so.6 (0x123)
        libm.so.6 => /lib/libm.so.6 (0x123)
        /lib64/ld-linux-x86-64.so.2 (0x123)
        """
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, output)):
            self.assertEqual(
                self.libraries(Path("qemu-img")),
                [Path("/usr/lib/libglib-2.0.so.0"), Path("/usr/lib/libffi.so.8")],
            )

    def test_missing_libraries_block_packaging(self):
        with (
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "libfoo.so => not found"),
            ),
            self.assertRaisesRegex(RuntimeError, "missing shared libraries"),
        ):
            self.libraries(Path("qemu-img"))
