import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from multilayer.model import VM, MultilayerError
from multilayer.runtime import bundled_executable, executable, external_env

PRIVATE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]
LOCAL = [
    *PRIVATE,
    "127.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "::1/128",
    "fe80::/10",
    "ff00::/8",
]


def local_networks(vm: VM) -> list[str]:
    networks = set(LOCAL)
    for cidr in vm.lan_cidrs:
        try:
            networks.add(str(ipaddress.ip_network(cidr, strict=False)))
        except ValueError as exc:
            raise MultilayerError(f"Некорректная LAN подсеть: {cidr}") from exc
    ip = executable("ip")
    try:
        result = subprocess.run(
            [ip, "-j", "address", "show"],
            check=True,
            capture_output=True,
            text=True,
            env=external_env(ip),
        )
        for interface in json.loads(result.stdout):
            for address in interface.get("addr_info", []):
                networks.add(
                    str(
                        ipaddress.ip_network(
                            f"{address['local']}/{address['prefixlen']}", strict=False
                        )
                    )
                )
    except (subprocess.SubprocessError, ValueError, KeyError) as exc:
        raise MultilayerError("Не удалось определить локальные подсети; запуск отменён") from exc
    return sorted(networks)


def network_arguments(vm: VM) -> list[str]:
    if vm.network == "off":
        return ["-nic", "none"]
    if vm.network == "tap":
        if sys.platform != "linux" or not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", vm.tap):
            raise MultilayerError("Мост требует Linux и имени существующего TAP (до 15 символов)")
        backend = f"tap,id=net0,ifname={vm.tap},script=no,downscript=no"
    else:
        if vm.network in ("internet", "lan") and sys.platform != "linux":
            raise MultilayerError(
                "Раздельный доступ к интернету/LAN реализован только в Linux. В Windows выберите отключение, изоляцию или NAT."
            )
        restrict = "on" if vm.network == "isolated" else "off"
        backend = f"user,id=net0,restrict={restrict},ipv6=off"
    mac = "52:54:00:" + ":".join(vm.id.replace("-", "")[i : i + 2] for i in (0, 2, 4))
    return ["-netdev", backend, "-device", f"{vm.nic},netdev=net0,id=nic0,mac={mac}"]


def policy_command(vm: VM, command: list[str], directory: Path) -> tuple[list[str], str | None]:
    if vm.network not in ("internet", "lan"):
        return command, None
    if sys.platform != "linux" or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        raise MultilayerError("Сетевые политики требуют Linux с systemd и cgroup v2")
    binaries = {name: shutil.which(name) for name in ("systemd-run", "pkexec")}
    binaries["bpftool"] = executable("bpftool")
    if not all(binaries.values()) or not Path("/run/systemd/system").is_dir():
        raise MultilayerError("Сетевые политики требуют systemd, bpftool и polkit (pkexec)")
    networks = local_networks(vm)
    unit = f"multilayer-{vm.id}.service"
    properties = [
        f"--property=User={os.getuid()}",
        f"--property=Group={os.getgid()}",
        "--property=NoNewPrivileges=yes",
        "--property=KillMode=control-group",
        "--property=UMask=0077",
        "--property=IPAccounting=yes",
    ]
    if vm.network == "internet":
        properties.append("--property=IPAddressDeny=" + " ".join(networks))
        try:
            resolver = ipaddress.ip_address(vm.dns)
            if any(resolver in ipaddress.ip_network(cidr) for cidr in networks):
                raise ValueError("DNS находится в запрещённой подсети")
        except ValueError as exc:
            raise MultilayerError(
                f"Для режима «только интернет» укажите публичный DNS: {exc}"
            ) from exc
        resolv = directory / "resolv.conf"
        resolv.write_text(f"nameserver {resolver}\n", encoding="ascii")
        if any(c in str(resolv) for c in (":", " ", "\n")):
            raise MultilayerError(
                "Для сетевой политики путь данных не должен содержать пробелы или двоеточия"
            )
        properties.append(f"--property=BindReadOnlyPaths={resolv}:/etc/resolv.conf")
    else:
        properties.extend(
            ["--property=IPAddressDeny=any", "--property=IPAddressAllow=" + " ".join(networks)]
        )
    environment = []
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"):
        if value := os.environ.get(name):
            environment.append(f"--setenv={name}={value}")
    for name, value in external_env(command[0]).items():
        if name in ("LD_LIBRARY_PATH", "QEMU_MODULE_DIR"):
            environment.append(f"--setenv={name}={value}")
    return [
        str(binaries["pkexec"]),
        str(binaries["systemd-run"]),
        f"--unit={unit}",
        "--collect",
        "--service-type=exec",
        *properties,
        *environment,
        "--",
        *command,
    ], unit


def verify_policy(unit: str) -> None:
    try:
        bpftool = executable("bpftool")
        command = [str(shutil.which("pkexec"))]
        if bundled_executable("bpftool"):
            command.extend(
                [executable("env"), "LD_LIBRARY_PATH=" + external_env(bpftool)["LD_LIBRARY_PATH"]]
            )
        command.append(bpftool)
        result = subprocess.run(
            [
                *command,
                "-j",
                "cgroup",
                "show",
                f"/sys/fs/cgroup/system.slice/{unit}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        programs = json.loads(result.stdout)
        types = {entry.get("attach_type", "") for entry in programs}
        if not (
            {"ingress", "egress"} <= types or {"cgroup_inet_ingress", "cgroup_inet_egress"} <= types
        ):
            raise ValueError("не подтверждены ingress/egress BPF фильтры")
    except (subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise MultilayerError(
            f"Не удалось подтвердить firewall: {exc}. ВМ не будет запущена."
        ) from exc
