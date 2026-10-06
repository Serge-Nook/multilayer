import json
import socket
import sys
import time
from typing import Any

from multilayer.model import MultilayerError


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
            else:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(timeout)
                self.sock.connect(endpoint)
            if "QMP" not in self._receive():
                raise MultilayerError("Некорректное приветствие QMP")
            self.execute("qmp_capabilities")
        except Exception as exc:
            self.close()
            raise OSError(f"Соединение QMP недоступно: {exc}") from exc

    def _receive(self) -> dict:
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self.buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError("Таймаут QMP")
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
                raise ConnectionError("QEMU закрыл соединение QMP")
            self.buffer += data
            if len(self.buffer) > 4 * 1024 * 1024:
                raise MultilayerError("Слишком большой ответ QMP")
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
        raise TimeoutError("Нет ответа на команду QMP")

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
        if self.pipe is not None:
            self.pipe.Close()

    def __enter__(self) -> "QMP":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
