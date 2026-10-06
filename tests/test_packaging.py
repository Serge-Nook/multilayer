import runpy
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.libraries = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "packaging/bundle_qemu_img.py")
        )["linux_libraries"]

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
