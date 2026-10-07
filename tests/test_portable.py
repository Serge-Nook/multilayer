import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from multilayer.cli import main
from multilayer.engine import Engine
from multilayer.model import VM, MultilayerError
from multilayer.portable import check_disk, export_vm, import_vm
from multilayer.store import Store


@unittest.skipUnless(shutil.which("qemu-img"), "Install QEMU for disk archive tests")
class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ml-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = Engine(Store(self.root / "source"))
        self.target = Engine(Store(self.root / "target"))
        self.vm = self.engine.create(
            VM(
                name="Перенос",
                memory_mb=256,
                cpus=1,
                disk_gb=1,
                accelerator="tcg",
                network="isolated",
            )
        )
        self.directory = self.engine.store.path(self.vm.id)
        self.file = self.root / "portable.multis"

    def build_archive(self):
        return export_vm(self.engine, self.vm.id, self.file)

    def rewrite(self, change):
        with zipfile.ZipFile(self.file) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(contents["manifest.json"])
        change(contents, manifest)
        contents["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(self.file, "w") as archive:
            for name, data in contents.items():
                archive.writestr(name, data)

    def assert_rejected_without_partial_vm(self):
        before = [vm.to_dict() for vm in self.target.store.list()]
        with self.assertRaises(MultilayerError):
            import_vm(self.target, self.file)
        self.assertEqual([vm.to_dict() for vm in self.target.store.list()], before)
        self.assertEqual(list(self.target.store.root.glob(".import-*")), [])

    def test_roundtrip_preserves_identity_disk_settings_and_snapshot_state(self):
        self.vm.iso = str(self.root / "installer.iso")
        Path(self.vm.iso).write_bytes(b"iso")
        self.vm.shared_folder = str(self.root / "shared")
        Path(self.vm.shared_folder).mkdir()
        self.engine.update(self.vm)
        (self.directory / "uefi-vars.fd").write_bytes(b"nvram")
        (self.directory / "tpm").mkdir()
        (self.directory / "tpm/tpm2-00.permall").write_bytes(b"secret")
        self.engine.snapshot(self.vm.id, "create", "before")
        archive_path = self.build_archive()
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(json.loads(archive.read("manifest.json"))["version"], 1)
            self.assertNotIn("runtime.json", archive.namelist())
            self.assertNotIn("qmp.sock", archive.namelist())
        moved = import_vm(self.target, self.file)
        self.assertEqual(moved.id, self.vm.id)
        self.assertEqual(moved.network, "isolated")
        self.assertEqual(moved.accelerator, "auto")
        self.assertEqual((moved.iso, moved.shared_folder), ("", ""))
        target = self.target.store.path(moved.id)
        self.assertEqual(
            hashlib.sha256((target / "disk.qcow2").read_bytes()).hexdigest(),
            hashlib.sha256((self.directory / "disk.qcow2").read_bytes()).hexdigest(),
        )
        self.assertEqual((target / "uefi-vars.fd").read_bytes(), b"nvram")
        self.assertEqual((target / "tpm/tpm2-00.permall").read_bytes(), b"secret")
        self.assertEqual(self.target.snapshot(moved.id, "list")[0]["name"], "before")
        (target / "uefi-vars.fd").write_bytes(b"changed")
        self.target.snapshot(moved.id, "restore", "before")
        restored = self.target.store.get(moved.id)
        self.assertEqual(restored.id, moved.id)
        self.assertEqual(restored.iso, "")
        self.assertEqual(restored.accelerator, "auto")
        self.assertEqual((target / "uefi-vars.fd").read_bytes(), b"nvram")

    def test_import_conflict_adds_separate_vm_and_rebases_snapshot_id(self):
        self.engine.snapshot(self.vm.id, "create", "s")
        self.build_archive()
        first = import_vm(self.target, self.file)
        second = import_vm(self.target, self.file, name="Копия")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.name, "Копия")
        self.target.snapshot(second.id, "restore", "s")
        self.assertEqual(self.target.store.get(second.id).id, second.id)
        self.assertEqual(len(self.target.store.list()), 2)
        self.assertEqual(first.name, "Перенос")

    def test_custom_firmware_and_snapshot_firmware_paths_are_rebased(self):
        code = self.root / "custom-code.fd"
        variables = self.root / "custom-vars.fd"
        code.write_bytes(b"firmware-code")
        variables.write_bytes(b"firmware-template")
        self.vm.firmware_code = str(code)
        self.vm.firmware_vars = str(variables)
        self.engine.update(self.vm)
        self.engine.snapshot(self.vm.id, "create", "s")
        self.build_archive()
        moved = import_vm(self.target, self.file)
        self.assertEqual(Path(moved.firmware_code).read_bytes(), b"firmware-code")
        self.assertTrue(Path(moved.firmware_code).is_relative_to(self.target.store.root))
        self.target.snapshot(moved.id, "restore", "s")
        restored = self.target.store.get(moved.id)
        self.assertTrue(Path(restored.firmware_code).is_relative_to(self.target.store.root))
        self.assertEqual(Path(restored.firmware_vars).read_bytes(), b"firmware-template")
        # A previously imported VM can itself be exported again.
        export_vm(self.target, moved.id, self.root / "again.multis")

    def test_stopped_guard_and_existing_archive_protection(self):
        with patch.object(self.engine, "status", return_value="unreachable"):
            with self.assertRaises(MultilayerError):
                self.build_archive()
        self.assertFalse(self.file.exists())
        self.build_archive()
        original = self.file.read_bytes()
        with self.assertRaises(MultilayerError):
            self.build_archive()
        self.assertEqual(self.file.read_bytes(), original)
        export_vm(self.engine, self.vm.id, self.file, overwrite=True)
        with self.assertRaises(MultilayerError):
            export_vm(self.engine, self.vm.id, self.root / "wrong.zip")

    def test_failed_export_preserves_existing_archive_and_cleans_temporary_files(self):
        self.file.write_bytes(b"existing archive")
        with patch("multilayer.portable.zipfile.ZipFile", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                export_vm(self.engine, self.vm.id, self.file, overwrite=True)
        self.assertEqual(self.file.read_bytes(), b"existing archive")
        self.assertFalse(list(self.root.glob(".multis-*")))

    def test_export_to_filesystem_without_hard_links(self):
        import errno

        with patch("multilayer.portable.os.link", side_effect=OSError(errno.EOPNOTSUPP, "exFAT")):
            self.build_archive()
        self.assertIsNotNone(import_vm(self.target, self.file))

    def test_path_traversal_absolute_paths_reserved_names_and_case_collisions_rejected(self):
        for name in (
            "../escape",
            "/absolute",
            "tpm/../escape",
            "tpm\\evil",
            "tpm/CON",
            "tpm/a.",
            "snapshots/s/tpm/x",
        ):
            with self.subTest(name=name):
                export_vm(self.engine, self.vm.id, self.file, overwrite=True)

                def extra(contents, manifest, name=name):
                    contents[name] = b"payload"
                    manifest["files"][name] = {
                        "size": 7,
                        "sha256": hashlib.sha256(b"payload").hexdigest(),
                    }
                    if name.startswith("snapshots/"):
                        other = "snapshots/S/tpm/y"
                        contents[other] = b"payload"
                        manifest["files"][other] = manifest["files"][name]

                self.rewrite(extra)
                self.assert_rejected_without_partial_vm()
        self.assertFalse((self.root / "escape").exists())

    def test_corrupt_checksum_and_missing_payload_rejected(self):
        self.build_archive()
        self.rewrite(
            lambda contents, manifest: manifest["files"]["disk.qcow2"].update(sha256="0" * 64)
        )
        self.assert_rejected_without_partial_vm()
        export_vm(self.engine, self.vm.id, self.file, overwrite=True)
        self.rewrite(lambda contents, manifest: contents.pop("disk.qcow2"))
        self.assert_rejected_without_partial_vm()

    def test_invalid_manifest_version_configuration_and_sizes_rejected(self):
        cases = [
            lambda c, m: m.update(version=2),
            lambda c, m: m.update(version=True),
            lambda c, m: m.update(architecture="arm"),
            lambda c, m: m["vm"].update(id="../bad"),
            lambda c, m: m["vm"].update(memory_mb="bad"),
            lambda c, m: m["vm"].update(tpm="bad"),
            lambda c, m: m["vm"].update(shared_folder="/host"),
            lambda c, m: m["vm"].update(firmware_code="../host"),
            lambda c, m: m["files"]["disk.qcow2"].update(size=-1),
        ]
        for change in cases:
            export_vm(self.engine, self.vm.id, self.file, overwrite=True)
            self.rewrite(change)
            self.assert_rejected_without_partial_vm()

    def test_links_duplicates_and_not_a_zip_rejected(self):
        self.build_archive()
        with zipfile.ZipFile(self.file, "a") as archive:
            link = zipfile.ZipInfo("tpm/link")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "/outside")
        self.assert_rejected_without_partial_vm()
        self.file.write_bytes(b"not a zip")
        self.assert_rejected_without_partial_vm()

    def test_disk_backing_and_external_data_headers_are_rejected(self):
        original = (self.directory / "disk.qcow2").read_bytes()
        for offset, value in ((8, 512), (72, 4)):
            payload = bytearray(original)
            payload[offset : offset + 8] = struct.pack(">Q", value)
            path = self.root / "bad.qcow2"
            path.write_bytes(payload)
            with self.assertRaises(MultilayerError):
                check_disk(path)

    def test_free_space_check_and_failed_final_save_leave_no_vm(self):
        self.build_archive()
        usage = shutil.disk_usage(self.target.store.root)
        with patch("multilayer.portable.shutil.disk_usage", return_value=type(usage)(1, 1, 0)):
            self.assert_rejected_without_partial_vm()
        with patch.object(self.target.store, "save", side_effect=OSError("save failed")):
            self.assert_rejected_without_partial_vm()
        self.assertEqual(list(self.target.store.root.iterdir()), [])

    def test_export_does_not_accept_symlink_payloads(self):
        if os.name == "nt":
            self.skipTest("Windows symlinks require developer mode")
        self.vm.firmware_code = str(self.root / "link.fd")
        actual = self.root / "actual.fd"
        actual.write_bytes(b"secret")
        Path(self.vm.firmware_code).symlink_to(actual)
        self.engine.update(self.vm)
        with self.assertRaises(MultilayerError):
            self.build_archive()

    def test_cli_export_import_and_english_guest_identifier_are_stable(self):
        from multilayer.i18n import language, set_language

        previous = language()
        self.addCleanup(set_language, previous, False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["--home", str(self.engine.store.root), "export", self.vm.id, str(self.file)]),
                0,
            )
            self.assertEqual(
                main(["--home", str(self.target.store.root), "import", str(self.file)]), 0
            )
            self.assertEqual(
                main(
                    [
                        "--lang",
                        "en",
                        "--home",
                        str(self.target.store.root),
                        "create",
                        "--name",
                        "RED",
                        "--guest",
                        "RED OS",
                        "--disk",
                        "1",
                    ]
                ),
                0,
            )
        self.assertEqual(
            next(vm for vm in self.target.store.list() if vm.name == "RED").guest, "РЕД ОС"
        )

    def test_incorrect_snapshot_identity_rejected(self):
        self.engine.snapshot(self.vm.id, "create", "s")
        self.build_archive()

        def change(contents, manifest):
            filename = "snapshots/s/vm.json"
            data = json.loads(contents[filename])
            data["id"] = str(uuid4())
            contents[filename] = json.dumps(data).encode()
            manifest["files"][filename] = {
                "size": len(contents[filename]),
                "sha256": hashlib.sha256(contents[filename]).hexdigest(),
            }

        self.rewrite(change)
        self.assert_rejected_without_partial_vm()
