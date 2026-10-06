import hashlib
import json
import os
import tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


def api(url: str, method: str = "GET", data: bytes | dict | None = None) -> dict:
    if urlparse(url).hostname not in ("api.github.com", "uploads.github.com"):
        raise ValueError("Unexpected GitHub API host")
    headers = {
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"
        if isinstance(data, dict)
        else "application/octet-stream",
    }
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=180) as response:
        return json.load(response)


def main(directory: Path = Path("dist")) -> None:
    tag = os.environ["GITHUB_REF_NAME"]
    version = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    if os.environ["GITHUB_REF_TYPE"] != "tag" or tag != "v" + version:
        raise ValueError("Release tag must match the application version")
    files = []
    for pattern in ("*.AppImage", "*.deb", "*-Setup.exe"):
        matches = list(directory.glob(pattern))
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one installer: {pattern}")
        files.extend(matches)
    sums = directory / "SHA256SUMS"
    sums.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in files
        ),
        encoding="utf-8",
    )
    files.append(sums)
    endpoint = "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + "/releases"
    try:
        release = api(endpoint + "/tags/" + quote(tag, safe=""))
    except HTTPError as error:
        if error.code != 404:
            raise
        release = api(
            endpoint,
            "POST",
            {
                "tag_name": tag,
                "name": "Мультислой " + tag,
                "draft": True,
                "prerelease": True,
                "body": "Предварительная версия локального менеджера QEMU/KVM: русский GUI и CLI, ISO, QCOW2, BIOS/UEFI, снимки, клонирование и управление сетью.\n\nАвтор: Горшков Сергей Владимирович — https://nookbat.ru.\n\nAppImage — Arch/Linux, DEB — Debian/Ubuntu с glibc >= 2.35, EXE — Windows 10/11 x64. В 0.1.4 исправлена упаковка графической консоли: добавлен зависимый модуль ui-opengl вместе с библиотеками; сборки проверяют запуск SDL-дисплея, а Linux — также загрузку модулей именно из дистрибутива. Установщики включают QEMU system emulator, qemu-img, TCG, SDL, BIOS/UEFI и необходимые библиотеки; Linux также включает swtpm, ip и bpftool. Отдельная установка прикладных компонентов не нужна. Лицензии и сведения об исходниках — в _internal/qemu/licenses.\n\nKVM/WHPX, ядро, драйверы и базовые службы ОС не могут поставляться внутри приложения; без аппаратного ускорения используется TCG. Раздельные политики internet/LAN используют systemd/cgroup v2 и polkit. TPM доступен только на Linux; гостевая Windows 11 на Windows-хосте пока не поддерживается. GUI, полные установки гостевых ОС и WHPX ещё не проверены интерактивно. Инструкции и ограничения — в README репозитория.",
            },
        )
    if not release["draft"]:
        raise ValueError("Published releases are never overwritten; use a new version")
    existing = {asset["name"]: asset for asset in release["assets"]}
    upload = release["upload_url"].split("{", 1)[0]
    for path in files:
        data = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if asset := existing.get(path.name):
            if asset.get("digest") != digest:
                raise ValueError(f"Draft asset differs; refusing to overwrite: {path.name}")
        else:
            api(upload + "?name=" + quote(path.name), "POST", data)
    published = api(release["url"], "PATCH", {"draft": False})
    print(published["html_url"])


if __name__ == "__main__":
    main()
