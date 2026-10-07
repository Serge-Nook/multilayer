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
                "body": (
                    "Предварительная версия локального менеджера QEMU/KVM. "
                    "Автор: Горшков Сергей Владимирович — https://nookbat.ru.\n\n"
                    "### Новое\n"
                    "- Русский/английский GUI и CLI; меню «Язык», немедленное переключение "
                    "и сохранение выбора. CLI: `--lang ru|en`.\n"
                    "- Кликабельный сайт и новый текст о SteamOS/бесплатном использовании "
                    "в «О программе». Установка/удаление теперь доступна там.\n"
                    "- Импорт/экспорт выключенных ВМ через `.multis`: диск, настройки, "
                    "снимки, UEFI/TPM и пользовательская прошивка. Проверка формата, "
                    "контрольных сумм и путей; существующие ВМ не заменяются.\n"
                    "- Логотип в окнах, Linux-ярлыках, Windows EXE и установщике.\n\n"
                    "ISO и внешнюю папку обмена после переноса подключите заново. "
                    "При импорте ускорение выбирается автоматически, сетевые ограничения "
                    "и TPM сохраняются. Архив не зашифрован и содержит секреты гостя/TPM: "
                    "храните безопасно и импортируйте только доверенные файлы.\n\n"
                    "AppImage — Arch/SteamOS/Linux; DEB — Debian/Ubuntu с glibc >= 2.35; "
                    "EXE — Windows 10/11 x64. Установщики содержат QEMU, qemu-img, TCG/SDL, "
                    "BIOS/UEFI и библиотеки; Linux также содержит swtpm, ip и bpftool. "
                    "Отдельная установка QEMU не нужна. Лицензии/исходники: "
                    "`_internal/qemu/licenses`.\n\n"
                    "KVM/WHPX, ядро, драйверы и службы остаются возможностями основной ОС; "
                    "без аппаратного ускорения используется TCG. Строгие интернет/LAN "
                    "политики требуют Linux/systemd/cgroup v2/polkit. TPM доступен только "
                    "на Linux; штатная гостевая Windows 11 на Windows-хосте пока не поддерживается. "
                    "Обновление/удаление приложения сохраняет ВМ.\n\n"
                    "Проверены автоматические тесты и упакованный runtime, включая SDL, "
                    "перенос снимков/UEFI/TPM и английский CLI без QEMU в PATH. "
                    "Интерактивные GUI/установщики, нативные Arch/SteamOS/WHPX и полные "
                    "установки гостевых ОС не проверены.\n\n"
                    "**English:** Instant, persistent Russian/English switching; clickable "
                    "About website; application logo; stopped-VM import/export via `.multis` "
                    "with disks, settings, snapshots and UEFI/TPM. Reattach external ISOs "
                    "and shared folders after import. Archives contain guest secrets and "
                    "are not encrypted. QEMU is bundled; host virtualization/drivers/services "
                    "remain OS features. See the repository README for limitations."
                ),
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
