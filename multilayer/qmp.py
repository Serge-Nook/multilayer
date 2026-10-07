import ctypes
import json
import os
import select
import socket
import struct
import sys
import time
from typing import Any

from multilayer.i18n import tr
from multilayer.model import MultilayerError


def windows_unix_socket(endpoint: str, timeout: float) -> socket.socket:
    # Windows supports AF_UNIX, but CPython's Windows build has no address codec for it.
    path = os.fsencode(endpoint)
    if len(path) >= 108 or b"\0" in path:
        raise OSError(tr("Некорректный путь локального сокета QMP"))
    address = ctypes.create_string_buffer(struct.pack("H", 1) + path.ljust(108, b"\0"), 110)
    loader = getattr(ctypes, "WinDLL")  # noqa: B009
    library = loader("Ws2_32.dll", winmode=0x800)
    connect = library.connect
    connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
    connect.restype = ctypes.c_int
    last_error = library.WSAGetLastError
    last_error.argtypes = []
    last_error.restype = ctypes.c_int
    sock = socket.socket(1, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        if connect(sock.fileno(), ctypes.byref(address), ctypes.sizeof(address)) != 0:
            error = last_error()
            if error not in (10035, 10036, 10037):
                raise OSError(error, tr("Не удалось подключиться к локальному сокету QMP"))
            _, writable, exceptional = select.select([], [sock], [sock], timeout)
            if not writable and not exceptional:
                raise TimeoutError(tr("Таймаут соединения QMP"))
            if error := sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                raise OSError(error, tr("Ошибка локального сокета QMP"))
        sock.settimeout(timeout)
        return sock
    except Exception:
        sock.close()
        raise


class QMP:
    def __init__(self, endpoint: str, timeout: float = 3):
        self.timeout = timeout
        self.sequence = 0
        self.buffer = b""
        self.sock: socket.socket | None = None
        self.pipe: Any = None
        try:
            if sys.platform == "win32" and endpoint.startswith("\\\\.\\pipe\\"):
                import win32file
                import win32pipe

                win32pipe.WaitNamedPipe(endpoint, int(timeout * 1000))
                self.pipe = win32file.CreateFile(
                    endpoint,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
            elif sys.platform == "win32":
                self.sock = windows_unix_socket(endpoint, timeout)
            else:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(timeout)
                self.sock.connect(endpoint)
            if "QMP" not in self._receive():
                raise MultilayerError(tr("Некорректное приветствие QMP"))
            self.execute("qmp_capabilities")
        except Exception as exc:
            self.close()
            raise OSError(tr("Соединение QMP недоступно: {error}", error=exc)) from exc

    def _receive(self) -> dict:
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError(tr("Таймаут QMP"))
            if self.sock is not None:
                data = self.sock.recv(65536)
            else:
                import win32file
                import win32pipe

                available = win32pipe.PeekNamedPipe(self.pipe, 0)[1]
                if not available:
                    time.sleep(0.01)
                    continue
                _, data = win32file.ReadFile(self.pipe, min(available, 65536))
            if not data:
                raise ConnectionError(tr("QEMU закрыл соединение QMP"))
            self.buffer += data
            if len(self.buffer) > 4 * 1024 * 1024:
                raise MultilayerError(tr("Слишком большой ответ QMP"))
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def execute(self, command: str, arguments: dict | None = None) -> Any:
        self.sequence += 1
        request: dict = {"execute": command, "id": self.sequence}
        if arguments is not None:
            request["arguments"] = arguments
        data = json.dumps(request).encode() + b"\n"
        if self.sock is not None:
            self.sock.sendall(data)
        else:
            import win32file

            win32file.WriteFile(self.pipe, data)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response = self._receive()
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise MultilayerError(response["error"].get("desc", str(response["error"])))
            return response.get("return")
        raise TimeoutError(tr("Нет ответа на команду QMP"))

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
        if self.pipe is not None:
            self.pipe.Close()

    def __enter__(self) -> "QMP":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
