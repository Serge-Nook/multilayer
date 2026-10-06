import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pycdlib

from multilayer.cli import main, parser
from multilayer.engine import (
    Engine,
    accelerator,
    executable,
    external_env,
    option_path,
    whpx_available,
)
from multilayer.maintenance import install_appimage, uninstall_appimage
from multilayer.model import VM, MultilayerError
from multilayer.network import local_networks, network_arguments, policy_command, verify_policy
from multilayer.qmp import QMP
from multilayer.sharing import folder_iso
from multilayer.store import Store


class ModelTests(unittest.TestCase):
    def test_defaults_are_network_off(self):
        vm = VM(name="Debian")
        vm.validate()
        self.assertEqual(vm.network, "off")

    def test_invalid_identifiers_and_values_rejected(self):
        for change in (
            {"id": "../../tmp"},
            {"name": ""},
            {"cpus": 0},
            {"memory_mb": 1},
            {"disk_gb": -1},
            {"network": "fake"},
            {"nic": "bad"},
            {"boot": "fake"},
            {"accelerator": "fake"},
            {"name": "line\nbreak"},
        ):
            with self.subTest(change=change), self.assertRaises(MultilayerError):
                VM(**{**VM(name="Test").to_dict(), **change}).validate(False)

    def test_windows_11_requirements(self):
        with self.assertRaises(MultilayerError):
            VM(name="Windows", guest="Windows 11").validate()
        VM(
            name="Windows",
            guest="Windows 11",
            memory_mb=4096,
            disk_gb=64,
            firmware="uefi",
            tpm=True,
        ).validate()

    def test_secure_boot_requires_uefi(self):
        with self.assertRaises(MultilayerError):
            VM(name="Test", secure_boot=True).validate()

    def test_missing_iso_rejected(self):
        with self.assertRaises(MultilayerError):
            VM(name="Test", iso="/missing/path.iso").validate()

    def test_cli_create_and_edit_defaults(self):
        create = parser().parse_args(["create", "--name", "Test"])
        edit = parser().parse_args(["edit", VM(name="Test").id, "--network", "nat"])
        self.assertEqual(create.network, "off")
        self.assertIsNone(edit.memory_mb)
        self.assertIsNone(edit.tpm)

    def test_destructive_cli_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--home", root, "delete", VM(name="Test").id]), 1)

    def test_accelerators_are_host_specific(self):
        with (
            patch("multilayer.engine.sys.platform", "win32"),
            patch("multilayer.engine.whpx_available", return_value=True),
        ):
            self.assertEqual(accelerator(VM(name="Test")), "whpx")
            with self.assertRaises(MultilayerError):
                accelerator(VM(name="Test", accelerator="kvm"))
        with (
            patch("multilayer.engine.sys.platform", "win32"),
            patch("multilayer.engine.whpx_available", return_value=False),
        ):
            self.assertEqual(accelerator(VM(name="Test")), "tcg")
        with (
            patch("multilayer.engine.sys.platform", "linux"),
            patch("os.access", return_value=False),
        ):
            self.assertEqual(accelerator(VM(name="Test")), "tcg")

    def test_option_commas_escaped(self):
        self.assertEqual(option_path("C:/my,folder/a.iso"), "C:/my,,folder/a.iso")

    def test_frozen_library_path_sanitized(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(
                os.environ, {"LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": "/system"}
            ),
        ):
            self.assertEqual(external_env()["LD_LIBRARY_PATH"], "/system")


class ExecutableTests(unittest.TestCase):
    def test_bundled_runtime_precedes_host_on_both_platforms(self):
        for platform, suffix in (("linux", ""), ("win32", ".exe")):
            for name in ("qemu-img", "qemu-system-x86_64"):
                with (
                    self.subTest(platform=platform, name=name),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    base = Path(temporary)
                    (base / "qemu/bin").mkdir(parents=True)
                    binary = base / "qemu/bin" / (name + suffix)
                    binary.write_bytes(b"bundled")
                    with (
                        patch.object(sys, "frozen", True, create=True),
                        patch.object(sys, "_MEIPASS", str(base), create=True),
                        patch.object(sys, "platform", platform),
                        patch("shutil.which") as which,
                    ):
                        self.assertEqual(executable(name), str(binary.resolve()))
                        which.assert_not_called()

    def test_source_run_uses_host_tool(self):
        with (
            patch.object(sys, "frozen", False, create=True),
            patch("shutil.which", return_value="/host/qemu-img"),
        ):
            self.assertEqual(executable("qemu-img"), str(Path("/host/qemu-img").resolve()))

    def test_linux_falls_back_to_standard_system_directories(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys, "frozen", False, create=True),
            patch("shutil.which", side_effect=[None, "/usr/bin/qemu-img"]) as which,
        ):
            self.assertEqual(executable("qemu-img"), str(Path("/usr/bin/qemu-img").resolve()))
            which.assert_called_with("qemu-img", path="/usr/local/bin:/usr/bin:/bin:/usr/sbin")

    def test_missing_tool_still_reports_error(self):
        with (
            patch.object(sys, "frozen", False, create=True),
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.is_file", return_value=False),
            self.assertRaisesRegex(MultilayerError, "Не найден qemu-img"),
        ):
            executable("qemu-img")

    def test_incomplete_frozen_runtime_requires_reinstall(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", temporary, create=True),
                patch.object(sys, "platform", "linux"),
                self.assertRaisesRegex(MultilayerError, "переустановите свежий дистрибутив"),
            ):
                executable("qemu-img")

    def test_bundled_library_path_does_not_leak_to_system_qemu(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "qemu/bin").mkdir(parents=True)
            binary = base / "qemu/bin/qemu-img"
            binary.write_bytes(b"bundled")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(base), create=True),
                patch.object(sys, "platform", "linux"),
                patch.dict(
                    os.environ, {"LD_LIBRARY_PATH": "/qt", "LD_LIBRARY_PATH_ORIG": "/original"}
                ),
            ):
                self.assertEqual(
                    external_env(str(binary))["LD_LIBRARY_PATH"], str((base / "qemu/lib").resolve())
                )
                self.assertEqual(
                    external_env("/usr/bin/qemu-system-x86_64")["LD_LIBRARY_PATH"], "/original"
                )
                self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/qt")

    def test_whpx_capability_detection(self):
        class Query:
            def __call__(self, code, output, size, written):
                output._obj.value = 1
                return 0

        query = Query()
        library = type("Library", (), {"WHvGetCapability": query})()
        with patch("ctypes.WinDLL", return_value=library, create=True):
            self.assertTrue(whpx_available())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name))

    def test_unicode_roundtrip(self):
        vm = VM(name="Астра — тест")
        self.store.save(vm)
        self.assertEqual(self.store.get(vm.id).to_dict(), vm.to_dict())
        self.assertEqual(len(self.store.list()), 1)

    def test_mismatched_id_rejected(self):
        vm = VM(name="Test")
        self.store.save(vm)
        path = self.store.path(vm.id) / "vm.json"
        data = vm.to_dict()
        data["id"] = VM(name="other").id
        path.write_text(json.dumps(data))
        with self.assertRaises(MultilayerError):
            self.store.get(vm.id)

    def test_corrupt_json_reported(self):
        vm = VM(name="Test")
        self.store.save(vm)
        (self.store.path(vm.id) / "vm.json").write_text("broken")
        with self.assertRaises(MultilayerError):
            self.store.get(vm.id)

    def test_deleted_vm_cannot_be_loaded(self):
        vm = VM(name="Test")
        self.store.save(vm)
        (self.store.path(vm.id) / ".deleting").touch()
        with self.assertRaises(MultilayerError):
            self.store.get(vm.id)

    def test_private_directory(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permissions")
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)


class NetworkTests(unittest.TestCase):
    def test_off_has_no_default_nic(self):
        self.assertEqual(network_arguments(VM(name="Test")), ["-nic", "none"])

    def test_isolation_restricts_user_network(self):
        self.assertIn("restrict=on", network_arguments(VM(name="Test", network="isolated"))[1])

    def test_nat_not_misrepresented_as_isolated(self):
        self.assertIn("restrict=off", network_arguments(VM(name="Test", network="nat"))[1])

    def test_windows_policy_fails_closed(self):
        with patch("multilayer.network.sys.platform", "win32"), self.assertRaises(MultilayerError):
            network_arguments(VM(name="Test", network="internet"))

    def test_bad_tap_rejected(self):
        with self.assertRaises(MultilayerError):
            network_arguments(VM(name="Test", network="tap", tap="tap0,script=bad"))

    def test_no_policy_for_regular_nat(self):
        command = ["qemu", "-S"]
        self.assertEqual(
            policy_command(VM(name="Test", network="nat"), command, Path(".")), (command, None)
        )

    @unittest.skipIf(sys.platform == "win32", "Linux systemd policy")
    def test_policy_uses_valid_systemd_properties(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.is_dir", return_value=True),
            patch("shutil.which", side_effect=lambda name: "/usr/bin/" + name),
            patch("multilayer.network.local_networks", return_value=["127.0.0.0/8"]),
        ):
            for network in ("internet", "lan"):
                command, unit = policy_command(
                    VM(name="Test", network=network), ["qemu", "-S"], Path(temporary)
                )
                self.assertIn("--property=IPAccounting=yes", command)
                self.assertTrue(unit.endswith(".service"))
                self.assertEqual(command[-2:], ["qemu", "-S"])

    def test_explicit_cidrs_validated(self):
        with self.assertRaises(MultilayerError):
            local_networks(VM(name="Test", lan_cidrs=["not a subnet"]))

    def test_failed_bpf_verification_blocks_launch(self):
        result = type("Result", (), {"stdout": "[]"})()
        with (
            patch("multilayer.network.executable", return_value="/usr/bin/bpftool"),
            patch("subprocess.run", return_value=result),
            self.assertRaises(MultilayerError),
        ):
            verify_policy("test.service")

    def test_both_bpf_directions_required(self):
        result = type(
            "Result", (), {"stdout": '[{"attach_type":"ingress"},{"attach_type":"egress"}]'}
        )()
        with (
            patch("multilayer.network.executable", return_value="/usr/bin/bpftool"),
            patch("subprocess.run", return_value=result),
        ):
            verify_policy("test.service")


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "share"
        self.folder.mkdir()

    def test_unicode_names_and_nested_files(self):
        (self.folder / "Каталог").mkdir()
        (self.folder / "Каталог/файл.txt").write_text("Привет", encoding="utf-8")
        destination = self.root / "share.iso"
        folder_iso(self.folder, destination)
        iso = pycdlib.PyCdlib()
        try:
            iso.open(str(destination))
            output = io.BytesIO()
            iso.get_file_from_iso_fp(output, joliet_path="/Каталог/файл.txt")
            self.assertEqual(output.getvalue().decode(), "Привет")
        finally:
            iso.close()

    def test_symlinks_rejected(self):
        if sys.platform == "win32":
            self.skipTest("Windows symlink requires developer mode")
        (self.folder / "link").symlink_to(self.root)
        with self.assertRaises(MultilayerError):
            folder_iso(self.folder, self.root / "share.iso")

    def test_rebuild_replaces_old_content(self):
        destination = self.root / "share.iso"
        folder_iso(self.folder, destination)
        (self.folder / "hello.txt").write_text("new")
        folder_iso(self.folder, destination)
        self.assertGreater(destination.stat().st_size, 0)


class MaintenanceTests(unittest.TestCase):
    def test_install_update_uninstall_keeps_data(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("pathlib.Path.home", return_value=Path(temporary)),
        ):
            home = Path(temporary)
            source = home / "source.AppImage"
            source.write_bytes(b"version one")
            data = home / ".local/share/multilayer"
            data.mkdir(parents=True)
            (data / "precious").write_text("vm")
            destination = install_appimage(source)
            self.assertEqual(destination.read_bytes(), b"version one")
            source.write_bytes(b"version two")
            install_appimage(source)
            self.assertEqual(destination.read_bytes(), b"version two")
            uninstall_appimage()
            self.assertFalse(destination.exists())
            self.assertEqual((data / "precious").read_text(), "vm")


@unittest.skipIf(sys.platform == "win32", "Unix socket protocol test")
class QMPTests(unittest.TestCase):
    def test_events_ignored_and_errors_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            endpoint = str(Path(temporary) / "qmp")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(endpoint)
            server.listen(1)

            def serve():
                connection, _ = server.accept()
                with connection, connection.makefile("rb") as incoming:
                    connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
                    for index in range(3):
                        request = json.loads(incoming.readline())
                        connection.sendall(b'{"event":"STOP"}\r\n')
                        response = {"id": request["id"], "return": {"status": "running"}}
                        if index == 2:
                            response = {"id": request["id"], "error": {"desc": "expected error"}}
                        connection.sendall(json.dumps(response).encode() + b"\r\n")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            with QMP(endpoint) as qmp:
                self.assertEqual(qmp.execute("query-status"), {"status": "running"})
                with self.assertRaisesRegex(MultilayerError, "expected error"):
                    qmp.execute("bad")
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


@unittest.skipUnless(sys.platform == "win32", "Windows named pipe test")
class WindowsQMPTests(unittest.TestCase):
    def test_real_named_pipe_transport(self):
        import win32file
        import win32pipe

        endpoint = rf"\\.\pipe\multilayer-test-{uuid4()}"
        handle = win32pipe.CreateNamedPipe(
            endpoint,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1,
            65536,
            65536,
            1000,
            None,
        )
        self.addCleanup(handle.Close)
        errors = []

        def serve():
            try:
                win32pipe.ConnectNamedPipe(handle, None)
                win32file.WriteFile(handle, b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
                buffer = b""
                for _ in range(2):
                    while b"\n" not in buffer:
                        _, data = win32file.ReadFile(handle, 65536)
                        buffer += data
                    line, buffer = buffer.split(b"\n", 1)
                    request = json.loads(line)
                    win32file.WriteFile(
                        handle,
                        json.dumps({"id": request["id"], "return": {"status": "running"}}).encode()
                        + b"\r\n",
                    )
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with QMP(endpoint) as qmp:
            self.assertEqual(qmp.execute("query-status"), {"status": "running"})
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


class EngineGuardTests(unittest.TestCase):
    def test_live_process_blocks_disk_operations_without_qmp(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            vm = VM(name="Test")
            store.save(vm)
            engine = Engine(store)
            with patch.object(engine, "_alive", return_value=True):
                self.assertEqual(engine.status(vm.id), "unreachable")
                with self.assertRaises(MultilayerError):
                    engine.require_stopped(vm)
