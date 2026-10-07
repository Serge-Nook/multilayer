from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from multilayer.i18n import tr

GUESTS = (
    "Debian",
    "Ubuntu",
    "Arch Linux",
    "Astra Linux",
    "РЕД ОС",
    "FreeBSD",
    "CentOS",
    "Windows 10",
    "Windows 11",
    "Другая ОС",
)
NETWORKS = {
    "off": "Сетевой адаптер отключён",
    "isolated": "Изолированная сеть: без интернета и LAN",
    "nat": "NAT: интернет и локальная сеть",
    "internet": "Только интернет (Linux, системный firewall)",
    "lan": "Только локальная сеть (Linux, системный firewall)",
    "tap": "Мост через существующий TAP (Linux)",
}


class MultilayerError(Exception):
    pass


@dataclass
class VM:
    name: str
    guest: str = "Debian"
    id: str = field(default_factory=lambda: str(uuid4()))
    memory_mb: int = 2048
    cpus: int = 2
    disk_gb: int = 32
    iso: str = ""
    drivers_iso: str = ""
    boot: str = "dvd"
    firmware: str = "bios"
    secure_boot: bool = False
    tpm: bool = False
    accelerator: str = "auto"
    network: str = "off"
    nic: str = "e1000"
    tap: str = ""
    lan_cidrs: list[str] = field(default_factory=list)
    dns: str = "1.1.1.1"
    shared_folder: str = ""
    firmware_code: str = ""
    firmware_vars: str = ""

    def validate(self, check_files: bool = True) -> None:
        for key, value in self.to_dict().items():
            valid = (
                type(value) is int
                if key in ("memory_mb", "cpus", "disk_gb")
                else type(value) is bool
                if key in ("secure_boot", "tpm")
                else isinstance(value, list)
                and len(value) <= 256
                and all(isinstance(item, str) for item in value)
                if key == "lan_cidrs"
                else isinstance(value, str)
            )
            if not valid:
                raise MultilayerError(tr("Недопустимое значение: {value}", value=key))
        try:
            if str(UUID(self.id)) != self.id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise MultilayerError(tr("Некорректный идентификатор ВМ")) from exc
        if not self.name.strip() or len(self.name) > 80 or any(ord(c) < 32 for c in self.name):
            raise MultilayerError(tr("Название ВМ: от 1 до 80 символов без управляющих символов"))
        if not 256 <= self.memory_mb <= 1048576 or not 1 <= self.cpus <= 256:
            raise MultilayerError(tr("RAM: 256–1048576 МБ; процессоры: 1–256"))
        if not 1 <= self.disk_gb <= 16384:
            raise MultilayerError(tr("Размер диска: 1–16384 ГБ"))
        for value, choices in (
            (self.network, NETWORKS),
            (self.nic, ("e1000", "virtio-net-pci", "rtl8139")),
            (self.accelerator, ("auto", "kvm", "whpx", "tcg")),
            (self.firmware, ("bios", "uefi")),
            (self.boot, ("dvd", "disk")),
        ):
            if value not in choices:
                raise MultilayerError(tr("Недопустимое значение: {value}", value=value))
        if self.secure_boot and self.firmware != "uefi":
            raise MultilayerError(tr("Secure Boot требует UEFI"))
        if self.guest == "Windows 11" and (
            self.firmware != "uefi"
            or not self.tpm
            or self.memory_mb < 4096
            or self.disk_gb < 64
            or self.cpus < 2
        ):
            raise MultilayerError(
                tr("Windows 11: UEFI, TPM 2.0, RAM ≥ 4096 МБ, диск ≥ 64 ГБ, CPU ≥ 2")
            )
        if check_files:
            for label, value in (
                ("ISO", self.iso),
                (tr("ISO драйверов"), self.drivers_iso),
                ("UEFI CODE", self.firmware_code),
                ("UEFI VARS", self.firmware_vars),
            ):
                if value and not Path(value).is_file():
                    raise MultilayerError(
                        tr("{label}: файл не найден: {path}", label=label, path=value)
                    )
            if self.shared_folder and not Path(self.shared_folder).is_dir():
                raise MultilayerError(tr("Папка обмена не найдена"))

    def to_dict(self) -> dict:
        return asdict(self)
