import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import pycdlib

from multilayer.engine import Engine, run
from multilayer.model import VM, MultilayerError
from multilayer.store import Store


@unittest.skipUnless(
    os.environ.get("MULTILAYER_INTEGRATION") == "1" and shutil.which("qemu-system-x86_64"),
    "Set MULTILAYER_INTEGRATION=1; install QEMU",
)
class QemuIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ml-")
        self.root = Path(self.temporary.name)
        self.engine = Engine(Store(self.root / "vms"))
        self.vm = self.engine.create(
            VM(name="Интеграционный тест", memory_mb=256, cpus=1, disk_gb=1, accelerator="tcg")
        )

    def tearDown(self):
        for vm in self.engine.store.list():
            if self.engine.status(vm.id) != "stopped":
                self.engine.control(vm.id, "stop")
        self.temporary.cleanup()

    def test_start_pause_resume_reconnect_stop(self):
        self.assertEqual(self.engine.start(self.vm.id, headless=True), "tcg")
        self.assertEqual(self.engine.status(self.vm.id), "running")
        self.engine.control(self.vm.id, "pause")
        self.assertEqual(self.engine.status(self.vm.id), "paused")
        reconnect = Engine(self.engine.store)
        reconnect.control(self.vm.id, "resume")
        self.assertEqual(reconnect.status(self.vm.id), "running")
        with self.assertRaises(MultilayerError):
            self.engine.delete(self.vm.id)
        self.engine.control(self.vm.id, "stop")
        self.assertEqual(self.engine.status(self.vm.id), "stopped")

    def test_nat_nic_live_cable_control(self):
        self.vm.network = "nat"
        self.engine.update(self.vm)
        self.engine.start(self.vm.id, headless=True)
        self.engine.link(self.vm.id, False)
        self.engine.link(self.vm.id, True)
        self.assertEqual(self.engine.status(self.vm.id), "running")

    def test_snapshot_restores_disk_bytes_and_clone_is_independent(self):
        image = str(self.engine.store.path(self.vm.id) / "disk.qcow2")
        run(["qemu-io", "-f", "qcow2", "-c", "write -P 0x42 0 512", image])
        self.engine.snapshot(self.vm.id, "create", "before")
        run(["qemu-io", "-f", "qcow2", "-c", "write -P 0x55 0 512", image])
        self.engine.snapshot(self.vm.id, "restore", "before")
        run(["qemu-io", "-f", "qcow2", "-c", "read -P 0x42 0 512", image])
        clone = self.engine.clone(self.vm.id, "Копия")
        self.assertNotEqual(clone.id, self.vm.id)
        run(
            [
                "qemu-io",
                "-f",
                "qcow2",
                "-c",
                "write -P 0x77 0 512",
                str(self.engine.store.path(clone.id) / "disk.qcow2"),
            ]
        )
        run(["qemu-io", "-f", "qcow2", "-c", "read -P 0x42 0 512", image])
        self.engine.snapshot(self.vm.id, "delete", "before")
        self.assertEqual(self.engine.snapshot(self.vm.id, "list"), [])

    def test_uefi_and_tpm_start_and_persist(self):
        if not shutil.which("swtpm") or not Path("/usr/share/OVMF").exists():
            self.skipTest("Install OVMF and swtpm")
        self.vm.firmware = "uefi"
        self.vm.tpm = True
        self.engine.update(self.vm)
        self.engine.start(self.vm.id, headless=True)
        self.assertEqual(self.engine.status(self.vm.id), "running")
        self.engine.control(self.vm.id, "stop")
        self.assertTrue((self.engine.store.path(self.vm.id) / "uefi-vars.fd").is_file())
        self.assertTrue((self.engine.store.path(self.vm.id) / "tpm").is_dir())
        self.engine.start(self.vm.id, headless=True)
        self.assertEqual(self.engine.status(self.vm.id), "running")

    def test_boots_selected_iso_and_mounts_shared_folder(self):
        marker = b"MULTILAYER_BOOT_OK"
        code = (
            b"\xba\xf8\x03"
            + b"".join(b"\xb0" + bytes([char]) + b"\xee" for char in marker)
            + b"\xfa\xf4\xeb\xfd"
        )
        sector = code.ljust(510, b"\0") + b"\x55\xaa"
        boot = self.root / "boot.img"
        boot.write_bytes(sector.ljust(1440 * 1024, b"\0"))
        image = self.root / "installer.iso"
        iso = pycdlib.PyCdlib()
        iso.new()
        iso.add_file(str(boot), iso_path="/BOOT.IMG;1")
        iso.add_eltorito("/BOOT.IMG;1", media_name="floppy")
        iso.write(str(image))
        iso.close()
        share = self.root / "share"
        share.mkdir()
        (share / "hello.txt").write_text("from host")
        self.vm.iso = str(image)
        self.vm.shared_folder = str(share)
        self.engine.update(self.vm)
        self.engine.start(self.vm.id, headless=True)
        log = self.engine.store.path(self.vm.id) / "serial.log"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if log.exists() and marker in log.read_bytes():
                break
            time.sleep(0.1)
        self.assertIn(marker, log.read_bytes())
        self.assertTrue((self.engine.store.path(self.vm.id) / "share.iso").is_file())
