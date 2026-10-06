import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from multilayer import APP_NAME, AUTHOR, WEBSITE, __version__
from multilayer.cli import doctor
from multilayer.engine import Engine
from multilayer.maintenance import maintain
from multilayer.model import GUESTS, NETWORKS, VM, MultilayerError
from multilayer.store import Store

STATUS = {
    "stopped": "Выключена",
    "running": "Работает",
    "paused": "Пауза",
    "prelaunch": "Подготовка",
    "shutdown": "Выключение",
}


class Result(QObject):
    done = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, operation: Callable):
        super().__init__()
        self.operation = operation
        self.signals = Result()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.operation())
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


def browse(field: QLineEdit, directory: bool = False) -> QWidget:
    widget = QWidget()
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(field)
    button = QPushButton("Обзор…")
    row.addWidget(button)

    def choose() -> None:
        path = (
            QFileDialog.getExistingDirectory(widget, "Папка обмена", field.text())
            if directory
            else QFileDialog.getOpenFileName(widget, "Выберите файл", field.text())[0]
        )
        if path:
            field.setText(path)

    button.clicked.connect(choose)
    return widget


class VMDialog(QDialog):
    def __init__(self, vm: VM | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.original = vm
        vm = vm or VM(name="Новая виртуальная машина")
        self.setWindowTitle("Настройки ВМ" if self.original else "Создать виртуальную машину")
        self.resize(660, 570)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        self.fields: dict[str, Any] = {}

        def tab(title: str) -> QFormLayout:
            page = QWidget()
            form = QFormLayout(page)
            tabs.addTab(page, title)
            return form

        def text(
            form: QFormLayout, key: str, label: str, picker: bool = False, directory: bool = False
        ) -> None:
            field = QLineEdit(str(getattr(vm, key)))
            self.fields[key] = field
            form.addRow(label, browse(field, directory) if picker else field)

        def combo(form: QFormLayout, key: str, label: str, choices: list | tuple | dict) -> None:
            field = QComboBox()
            for choice in choices:
                field.addItem(choices[choice] if isinstance(choices, dict) else choice, choice)
            field.setCurrentIndex(field.findData(getattr(vm, key)))
            self.fields[key] = field
            form.addRow(label, field)

        def spin(form: QFormLayout, key: str, label: str, minimum: int, maximum: int) -> None:
            field = QSpinBox()
            field.setRange(minimum, maximum)
            field.setValue(getattr(vm, key))
            self.fields[key] = field
            form.addRow(label, field)

        hardware = tab("Оборудование")
        text(hardware, "name", "Название")
        combo(hardware, "guest", "Гостевая ОС", GUESTS)
        spin(hardware, "memory_mb", "Оперативная память, МБ", 256, 1048576)
        spin(hardware, "cpus", "Процессоры", 1, 256)
        spin(hardware, "disk_gb", "Диск QCOW2, ГБ", 1, 16384)
        self.fields["disk_gb"].setEnabled(self.original is None)
        combo(hardware, "accelerator", "Ускорение", ("auto", "kvm", "whpx", "tcg"))
        combo(hardware, "firmware", "Прошивка", {"bios": "BIOS", "uefi": "UEFI"})
        for key, label in (
            ("tpm", "TPM 2.0 (Linux + swtpm)"),
            ("secure_boot", "Secure Boot (прошивка с ключами Microsoft)"),
        ):
            field = QCheckBox(label)
            field.setChecked(getattr(vm, key))
            self.fields[key] = field
            hardware.addRow(field)

        media = tab("ISO и обмен")
        text(media, "iso", "Установочный ISO", picker=True)
        text(media, "drivers_iso", "ISO драйверов", picker=True)
        combo(
            media, "boot", "Первое устройство загрузки", {"dvd": "Виртуальный DVD", "disk": "Диск"}
        )
        text(media, "shared_folder", "Папка обмена", picker=True, directory=True)
        note = QLabel(
            "Папка доступна гостю как дополнительный DVD только для чтения.\nСодержимое обновляется при запуске ВМ. Для обновления выключите ВМ и запустите снова.\nСимволические ссылки не копируются; лимит — 8 ГБ."
        )
        note.setWordWrap(True)
        media.addRow(note)
        text(media, "firmware_code", "UEFI CODE (необязательно)", picker=True)
        text(media, "firmware_vars", "UEFI VARS (необязательно)", picker=True)

        network = tab("Сеть")
        combo(network, "network", "Режим", NETWORKS)
        combo(network, "nic", "Модель адаптера", ("e1000", "virtio-net-pci", "rtl8139"))
        text(network, "tap", "Существующий TAP")
        text(network, "dns", "Публичный DNS (только интернет)")
        self.cidrs = QLineEdit(", ".join(vm.lan_cidrs))
        network.addRow("Дополнительные LAN подсети", self.cidrs)
        help_label = QLabel(
            "NAT разрешает и интернет, и LAN. Изоляция запрещает внешние соединения.\nРаздельный доступ требует Linux, systemd/cgroup v2, bpftool и авторизации polkit.\nФильтры проверяются до включения CPU ВМ. Без подтверждённого firewall запуск отменяется.\nРежим «только LAN» использует NAT: входящие подключения и обнаружение LAN не доступны.\nДля полноценного присутствия в LAN используйте заранее настроенный TAP/мост."
        )
        help_label.setWordWrap(True)
        network.addRow(help_label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.fields["guest"].currentTextChanged.connect(self.guest_changed)

    def guest_changed(self, guest: str) -> None:
        if guest == "Windows 11":
            self.fields["memory_mb"].setValue(max(4096, self.fields["memory_mb"].value()))
            self.fields["cpus"].setValue(max(2, self.fields["cpus"].value()))
            if self.original is None:
                self.fields["disk_gb"].setValue(max(64, self.fields["disk_gb"].value()))
            self.fields["firmware"].setCurrentIndex(self.fields["firmware"].findData("uefi"))
            self.fields["tpm"].setChecked(True)

    def value(self) -> VM:
        values = self.original.to_dict() if self.original else {}
        for key, field in self.fields.items():
            if isinstance(field, QCheckBox):
                values[key] = field.isChecked()
            elif isinstance(field, QComboBox):
                values[key] = field.currentData()
            elif isinstance(field, QSpinBox):
                values[key] = field.value()
            else:
                values[key] = field.text().strip()
        values["lan_cidrs"] = [
            cidr.strip() for cidr in self.cidrs.text().split(",") if cidr.strip()
        ]
        vm = VM(**values)
        vm.validate()
        return vm

    def accept(self) -> None:
        try:
            self.value()
            super().accept()
        except MultilayerError as exc:
            QMessageBox.warning(self, "Проверьте настройки", str(exc))


class Window(QMainWindow):
    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.busy = False
        self.refreshing = False
        self.workers: set[Worker] = set()
        self.rows: list[VM] = []
        self.setWindowTitle(f"{APP_NAME} — виртуальные машины")
        self.resize(1100, 720)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        title = QLabel("МУЛЬТИСЛОЙ")
        title.setStyleSheet("font-size: 25px; font-weight: 700; padding: 8px 0;")
        layout.addWidget(title)
        subtitle = QLabel("Локальные виртуальные машины · QEMU / KVM / WHPX")
        layout.addWidget(subtitle)
        toolbar = QHBoxLayout()
        layout.addLayout(toolbar)
        self.buttons = {}
        for label, operation in (
            ("Создать", self.create_vm),
            ("Настройки", self.edit),
            ("Запустить", lambda: self.control("start")),
            ("Выключить ОС", lambda: self.control("shutdown")),
            ("Пауза", lambda: self.control("pause")),
            ("Продолжить", lambda: self.control("resume")),
            ("Остановить", lambda: self.control("stop")),
        ):
            button = QPushButton(label)
            button.clicked.connect(operation)
            toolbar.addWidget(button)
            self.buttons[label] = button
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Виртуальная машина", "Гостевая ОС", "Состояние", "RAM", "CPU", "Сеть"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        splitter.addWidget(self.table)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        splitter.addWidget(self.details)
        splitter.setSizes([420, 160])
        actions = QHBoxLayout()
        layout.addLayout(actions)
        for label, callback in (
            ("Клонировать", self.clone),
            ("Снимки диска", self.snapshots),
            ("Отключить кабель", lambda: self.cable(False)),
            ("Подключить кабель", lambda: self.cable(True)),
            ("Журнал QEMU", self.log),
            ("Удалить ВМ", self.delete),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.buttons[label] = button
        for label, callback in (
            ("Диагностика", self.diagnostic),
            ("Установка / удаление", self.maintenance),
            ("О программе", self.about),
        ):
            action = QAction(label, self)
            action.triggered.connect(callback)
            self.menuBar().addAction(action)
        self.statusBar().showMessage(
            "ВМ сохраняются отдельно от приложения. По умолчанию сеть отключена."
        )
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2500)
        self.refresh()

    def selected(self) -> VM:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            raise MultilayerError("Выберите виртуальную машину")
        return self.rows[row]

    def perform(self, operation: Callable, callback: Callable | None = None) -> None:
        if self.busy:
            return
        self.busy = True
        for button in self.buttons.values():
            button.setEnabled(False)
        self.statusBar().showMessage("Выполняется операция…")
        worker = Worker(operation)
        self.workers.add(worker)
        worker.signals.error.connect(lambda message: QMessageBox.critical(self, "Ошибка", message))
        if callback:
            worker.signals.done.connect(callback)

        def complete() -> None:
            self.workers.discard(worker)
            self.busy = False
            self.statusBar().showMessage("Готово")
            for button in self.buttons.values():
                button.setEnabled(True)
            self.refresh()

        worker.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(worker)

    def refresh(self) -> None:
        if self.busy or self.refreshing:
            return
        self.refreshing = True
        worker = Worker(
            lambda: [(vm, self.engine.status(vm.id)) for vm in self.engine.store.list()]
        )
        self.workers.add(worker)
        worker.signals.done.connect(self.render_rows)
        worker.signals.error.connect(lambda message: self.statusBar().showMessage(message))

        def complete() -> None:
            self.refreshing = False
            self.workers.discard(worker)

        worker.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(worker)

    def render_rows(self, rows: list) -> None:
        selected = (
            self.rows[self.table.currentRow()].id
            if 0 <= self.table.currentRow() < len(self.rows)
            else None
        )
        self.table.blockSignals(True)
        self.rows = [vm for vm, _ in rows]
        self.table.setRowCount(len(rows))
        for row, (vm, status) in enumerate(rows):
            values = (
                vm.name,
                vm.guest,
                STATUS.get(status, status),
                f"{vm.memory_mb} МБ",
                str(vm.cpus),
                NETWORKS[vm.network],
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
            if selected == vm.id:
                self.table.selectRow(row)
        if not selected and rows:
            self.table.selectRow(0)
        self.table.resizeColumnsToContents()
        self.table.blockSignals(False)
        self.selection_changed()

    def selection_changed(self) -> None:
        try:
            vm = self.selected()
            self.details.setPlainText(
                f"{vm.name}\nID: {vm.id}\nISO: {vm.iso or 'не подключён'}\n"
                f"Ускорение: {vm.accelerator} · Прошивка: {vm.firmware} · TPM: {'да' if vm.tpm else 'нет'}\n"
                f"Обмен: {vm.shared_folder or 'не подключён'} (только чтение)\nДанные: {self.engine.store.path(vm.id)}"
            )
        except MultilayerError:
            self.details.setPlainText(
                "Создайте виртуальную машину, выберите ISO и запустите установку ОС."
            )

    def create_vm(self) -> None:
        dialog = VMDialog(parent=self)
        if dialog.exec():
            vm = dialog.value()
            self.perform(lambda: self.engine.create(vm))

    def edit(self) -> None:
        try:
            vm = self.selected()
            dialog = VMDialog(vm, self)
            if dialog.exec():
                updated = dialog.value()
                self.perform(lambda: self.engine.update(updated))
        except MultilayerError as exc:
            QMessageBox.warning(self, "Настройки", str(exc))

    def confirm(self, text: str) -> bool:
        return (
            QMessageBox.question(
                self,
                "Подтверждение",
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def control(self, action: str) -> None:
        try:
            vm = self.selected()
            if action == "stop" and not self.confirm(
                "Принудительно остановить ВМ? Несохранённые данные гостевой ОС могут быть потеряны."
            ):
                return
            if action == "start":
                self.perform(
                    lambda: self.engine.start(vm.id),
                    lambda accel: self.statusBar().showMessage(
                        f"ВМ запущена: {accel}. Консоль — в отдельном окне QEMU."
                    ),
                )
            else:
                self.perform(lambda: self.engine.control(vm.id, action))
        except MultilayerError as exc:
            QMessageBox.warning(self, "Управление", str(exc))

    def cable(self, enabled: bool) -> None:
        try:
            vm = self.selected()
            self.perform(lambda: self.engine.link(vm.id, enabled))
        except MultilayerError as exc:
            QMessageBox.warning(self, "Сеть", str(exc))

    def clone(self) -> None:
        try:
            vm = self.selected()
            name, accepted = QInputDialog.getText(
                self, "Клонировать", "Название копии", text=vm.name + " — копия"
            )
            if accepted and name.strip():
                self.perform(lambda: self.engine.clone(vm.id, name.strip()))
        except MultilayerError as exc:
            QMessageBox.warning(self, "Клонировать", str(exc))

    def snapshots(self) -> None:
        try:
            vm = self.selected()
            action, accepted = QInputDialog.getItem(
                self,
                "Снимки диска",
                "Выберите действие (ВМ должна быть выключена)",
                ["Список", "Создать", "Восстановить", "Удалить"],
                editable=False,
            )
            if not accepted:
                return
            operation = {
                "Список": "list",
                "Создать": "create",
                "Восстановить": "restore",
                "Удалить": "delete",
            }[action]
            name = ""
            if operation != "list":
                name, accepted = QInputDialog.getText(
                    self, action, "Имя снимка: латинские буквы, цифры, _ или -"
                )
                if not accepted:
                    return
            if operation in ("restore", "delete") and not self.confirm(
                "Изменения после снимка/снимок будут потеряны. Продолжить?"
            ):
                return
            self.perform(
                lambda: self.engine.snapshot(vm.id, operation, name),
                lambda data: QMessageBox.information(
                    self, "Снимки диска", json.dumps(data, ensure_ascii=False, indent=2)
                )
                if operation == "list"
                else None,
            )
        except MultilayerError as exc:
            QMessageBox.warning(self, "Снимки", str(exc))

    def delete(self) -> None:
        try:
            vm = self.selected()
            if self.confirm(
                f"Удалить «{vm.name}» вместе с диском и снимками? Отменить удаление нельзя."
            ):
                self.perform(lambda: self.engine.delete(vm.id))
        except MultilayerError as exc:
            QMessageBox.warning(self, "Удаление", str(exc))

    def log(self) -> None:
        try:
            path = self.engine.store.path(self.selected().id) / "qemu.log"
            self.details.setPlainText(
                path.read_text(errors="replace")[-20000:] if path.exists() else "Журнал пока пуст"
            )
        except MultilayerError as exc:
            QMessageBox.warning(self, "Журнал", str(exc))

    def diagnostic(self) -> None:
        self.details.setPlainText(json.dumps(doctor(), ensure_ascii=False, indent=2))

    def maintenance(self) -> None:
        Maintenance(self).exec()

    def about(self) -> None:
        QMessageBox.about(
            self,
            "О программе",
            f"{APP_NAME} {__version__}\nАвтор: {AUTHOR}\n{WEBSITE}\nQEMU/KVM в Linux; QEMU/WHPX в Windows.\nЭто первая версия, не замена всему функционалу Proxmox.",
        )

    def closeEvent(self, event: Any) -> None:
        if self.busy:
            QMessageBox.warning(
                self, "Операция выполняется", "Дождитесь завершения операции перед закрытием."
            )
            event.ignore()
            return
        self.timer.stop()
        QThreadPool.globalInstance().waitForDone()
        event.accept()


class Maintenance(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Мультислой — установка")
        self.resize(460, 260)
        layout = QVBoxLayout(self)
        label = QLabel(
            "Мультислой\nУстановить/обновить приложение или удалить его.\nВиртуальные машины и их диски сохраняются."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.buttons = []
        self.worker: Worker | None = None
        for title, action in (
            ("Установить / обновить", "install"),
            ("Удалить приложение", "remove"),
        ):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, operation=action: self.execute(operation))
            layout.addWidget(button)
            self.buttons.append(button)
        button = QPushButton("Выйти")
        button.clicked.connect(self.reject)
        layout.addWidget(button)
        self.buttons.append(button)

    def execute(self, action: str) -> None:
        if (
            action == "remove"
            and QMessageBox.question(self, "Удалить приложение?", "Виртуальные машины сохранятся.")
            != QMessageBox.StandardButton.Yes
        ):
            return
        for button in self.buttons:
            button.setEnabled(False)
        self.worker = Worker(lambda: maintain(action))
        self.worker.signals.done.connect(
            lambda text: QMessageBox.information(self, "Установка", text)
        )
        self.worker.signals.error.connect(lambda text: QMessageBox.critical(self, "Ошибка", text))

        def complete() -> None:
            for button in self.buttons:
                button.setEnabled(True)
            if action == "install":
                self.accept()

        self.worker.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(self.worker)


def launch(home: Path | None = None, maintenance: bool = False) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("nookbat.ru")
    if maintenance:
        dialog = Maintenance()
        if not dialog.exec():
            return 0
    window = Window(Engine(Store(home)))
    window.show()
    return app.exec()
