import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QLibraryInfo,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    QTranslator,
    Signal,
)
from PySide6.QtGui import QIcon
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
from multilayer.branding import logo_path
from multilayer.cli import doctor
from multilayer.engine import Engine
from multilayer.i18n import LANGUAGES, language, set_language, tr
from multilayer.maintenance import maintain
from multilayer.model import GUESTS, NETWORKS, VM, MultilayerError
from multilayer.store import Store

STATUS = {
    "stopped": "Выключена",
    "running": "Работает",
    "paused": "Пауза",
    "prelaunch": "Подготовка",
    "shutdown": "Выключение",
    "unreachable": "Нет связи с QEMU",
}

_translator: QTranslator | None = None


def translate_qt(app: QApplication) -> None:
    global _translator
    if _translator is not None:
        app.removeTranslator(_translator)
    _translator = QTranslator(app)
    if language() == "ru":
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "qt-translations"
        system = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if _translator.load("qtbase_ru", str(bundled)) or _translator.load("qtbase_ru", system):
            app.installTranslator(_translator)


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
    button = QPushButton(tr("Обзор…"))
    row.addWidget(button)

    def choose() -> None:
        path = (
            QFileDialog.getExistingDirectory(
                widget,
                tr("Папка обмена"),
                field.text(),
                QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog,
            )
            if directory
            else QFileDialog.getOpenFileName(
                widget,
                tr("Выберите файл"),
                field.text(),
                options=QFileDialog.Option.DontUseNativeDialog,
            )[0]
        )
        if path:
            field.setText(path)

    button.clicked.connect(choose)
    return widget


class VMDialog(QDialog):
    def __init__(self, vm: VM | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.original = vm
        vm = vm or VM(name=tr("Новая виртуальная машина"))
        self.setWindowTitle(
            tr("Настройки ВМ") if self.original else tr("Создать виртуальную машину")
        )
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
                field.addItem(tr(choices[choice] if isinstance(choices, dict) else choice), choice)
            field.setCurrentIndex(field.findData(getattr(vm, key)))
            self.fields[key] = field
            form.addRow(label, field)

        def spin(form: QFormLayout, key: str, label: str, minimum: int, maximum: int) -> None:
            field = QSpinBox()
            field.setRange(minimum, maximum)
            field.setValue(getattr(vm, key))
            self.fields[key] = field
            form.addRow(label, field)

        hardware = tab(tr("Оборудование"))
        text(hardware, "name", tr("Название"))
        combo(hardware, "guest", tr("Гостевая ОС"), GUESTS)
        spin(hardware, "memory_mb", tr("Оперативная память, МБ"), 256, 1048576)
        spin(hardware, "cpus", tr("Процессоры"), 1, 256)
        spin(hardware, "disk_gb", tr("Диск QCOW2, ГБ"), 1, 16384)
        self.fields["disk_gb"].setEnabled(self.original is None)
        combo(hardware, "accelerator", tr("Ускорение"), ("auto", "kvm", "whpx", "tcg"))
        combo(hardware, "firmware", tr("Прошивка"), {"bios": "BIOS", "uefi": "UEFI"})
        for key, label in (
            ("tpm", tr("TPM 2.0 (Linux, встроенный swtpm)")),
            ("secure_boot", tr("Secure Boot (прошивка с ключами Microsoft)")),
        ):
            field = QCheckBox(label)
            field.setChecked(getattr(vm, key))
            self.fields[key] = field
            hardware.addRow(field)

        media = tab(tr("ISO и обмен"))
        text(media, "iso", tr("Установочный ISO"), picker=True)
        text(media, "drivers_iso", tr("ISO драйверов"), picker=True)
        combo(
            media,
            "boot",
            tr("Первое устройство загрузки"),
            {"dvd": tr("Виртуальный DVD"), "disk": tr("Диск")},
        )
        text(media, "shared_folder", tr("Папка обмена"), picker=True, directory=True)
        note = QLabel(
            tr(
                "Папка доступна гостю как дополнительный DVD только для чтения.\nСодержимое обновляется при запуске ВМ. Для обновления выключите ВМ и запустите снова.\nСимволические ссылки не копируются; лимит — 8 ГБ."
            )
        )
        note.setWordWrap(True)
        media.addRow(note)
        text(media, "firmware_code", tr("UEFI CODE (необязательно)"), picker=True)
        text(media, "firmware_vars", tr("UEFI VARS (необязательно)"), picker=True)

        network = tab(tr("Сеть"))
        combo(network, "network", tr("Режим"), NETWORKS)
        combo(network, "nic", tr("Модель адаптера"), ("e1000", "virtio-net-pci", "rtl8139"))
        text(network, "tap", tr("Существующий TAP"))
        text(network, "dns", tr("Публичный DNS (только интернет)"))
        self.cidrs = QLineEdit(", ".join(vm.lan_cidrs))
        network.addRow(tr("Дополнительные LAN подсети"), self.cidrs)
        help_label = QLabel(
            tr(
                "NAT разрешает и интернет, и LAN. Изоляция запрещает внешние соединения.\nРаздельный доступ требует Linux, systemd/cgroup v2, bpftool и авторизации polkit.\nФильтры проверяются до включения CPU ВМ. Без подтверждённого firewall запуск отменяется.\nРежим «только LAN» использует NAT: входящие подключения и обнаружение LAN не доступны.\nДля полноценного присутствия в LAN используйте заранее настроенный TAP/мост."
            )
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
            QMessageBox.warning(self, tr("Проверьте настройки"), str(exc))


class Window(QMainWindow):
    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.busy = False
        self.refreshing = False
        self.workers: set[Worker] = set()
        self.rows: list[VM] = []
        self.cached_rows: list = []
        self.build_ui()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2500)
        self.refresh()

    def build_ui(self) -> None:
        self.setWindowTitle(
            tr("{value0} {value1} — виртуальные машины", value0=tr(APP_NAME), value1=__version__)
        )
        self.resize(1100, 720)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        heading = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QIcon(str(logo_path())).pixmap(56, 56))
        heading.addWidget(logo)
        title = QLabel(tr("МУЛЬТИСЛОЙ"))
        title.setStyleSheet("font-size: 25px; font-weight: 700; padding: 8px 0;")
        heading.addWidget(title)
        heading.addStretch()
        layout.addLayout(heading)
        subtitle = QLabel(tr("Локальные виртуальные машины · QEMU / KVM / WHPX"))
        layout.addWidget(subtitle)
        toolbar = QHBoxLayout()
        layout.addLayout(toolbar)
        self.buttons = {}
        for label, operation in (
            (tr("Создать"), self.create_vm),
            (tr("Настройки"), self.edit),
            (tr("Запустить"), lambda: self.control("start")),
            (tr("Выключить ОС"), lambda: self.control("shutdown")),
            (tr("Пауза"), lambda: self.control("pause")),
            (tr("Продолжить"), lambda: self.control("resume")),
            (tr("Остановить"), lambda: self.control("stop")),
            (tr("Перезагрузить"), lambda: self.control("reset")),
        ):
            button = QPushButton(label)
            button.clicked.connect(operation)
            toolbar.addWidget(button)
            self.buttons[label] = button
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            [tr("Виртуальная машина"), tr("Гостевая ОС"), tr("Состояние"), "RAM", "CPU", tr("Сеть")]
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
            (tr("Импорт"), self.import_vm),
            (tr("Экспорт"), self.export_vm),
            (tr("Клонировать"), self.clone),
            (tr("Снимки диска"), self.snapshots),
            (tr("Отключить кабель"), lambda: self.cable(False)),
            (tr("Подключить кабель"), lambda: self.cable(True)),
            (tr("Журнал QEMU"), self.log),
            (tr("Удалить ВМ"), self.delete),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
            self.buttons[label] = button
        self.menuBar().clear()
        diagnostic = self.menuBar().addAction(tr("Диагностика"))
        diagnostic.triggered.connect(self.diagnostic)
        languages = self.menuBar().addMenu(tr("Язык"))
        for code, label in LANGUAGES.items():
            action = languages.addAction(label)
            action.setCheckable(True)
            action.setChecked(language() == code)
            action.triggered.connect(
                lambda checked=False, selected=code: self.change_language(selected)
            )
        about = self.menuBar().addAction(tr("О программе"))
        about.triggered.connect(self.about)
        self.statusBar().showMessage(
            tr("ВМ сохраняются отдельно от приложения. По умолчанию сеть отключена.")
        )

    def change_language(self, code: str) -> None:
        if self.busy:
            QMessageBox.warning(
                self,
                tr("Операция выполняется"),
                tr("Дождитесь завершения операции перед закрытием."),
            )
            return
        try:
            set_language(code)
        except OSError as exc:
            QMessageBox.warning(self, tr("Язык"), str(exc))
            return
        app = QApplication.instance()
        if isinstance(app, QApplication):
            translate_qt(app)
        current = self.table.currentRow()
        identifier = self.rows[current].id if 0 <= current < len(self.rows) else None
        self.build_ui()
        self.render_rows(self.cached_rows)
        for row, vm in enumerate(self.rows):
            if vm.id == identifier:
                self.table.selectRow(row)

    def selected(self) -> VM:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            raise MultilayerError(tr("Выберите виртуальную машину"))
        return self.rows[row]

    def perform(self, operation: Callable, callback: Callable | None = None) -> None:
        if self.busy:
            return
        self.busy = True
        for button in self.buttons.values():
            button.setEnabled(False)
        self.statusBar().showMessage(tr("Выполняется операция…"))
        worker = Worker(operation)
        self.workers.add(worker)

        def failed(message: str) -> None:
            self.statusBar().showMessage(tr("Ошибка: ") + message)
            QMessageBox.critical(self, tr("Ошибка"), message)

        def succeeded(value: Any) -> None:
            self.statusBar().showMessage(tr("Готово"))
            if callback:
                callback(value)

        worker.signals.error.connect(failed)
        worker.signals.done.connect(succeeded)

        def complete() -> None:
            self.workers.discard(worker)
            self.busy = False
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
        self.cached_rows = rows
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
                tr(vm.guest),
                tr(STATUS.get(status, status)),
                tr("{value0} МБ", value0=vm.memory_mb),
                str(vm.cpus),
                tr(NETWORKS[vm.network]),
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
                tr(
                    "{value0}\nID: {value1}\nISO: {value2}\nУскорение: {value3} · Прошивка: {value4} · TPM: {value5}\nОбмен: {value6} (только чтение)\nДанные: {value7}",
                    value0=vm.name,
                    value1=vm.id,
                    value2=vm.iso or tr("не подключён"),
                    value3=vm.accelerator,
                    value4=vm.firmware,
                    value5=tr("да") if vm.tpm else tr("нет"),
                    value6=vm.shared_folder or tr("не подключён"),
                    value7=self.engine.store.path(vm.id),
                )
            )
        except MultilayerError:
            self.details.setPlainText(
                tr("Создайте виртуальную машину, выберите ISO и запустите установку ОС.")
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
            QMessageBox.warning(self, tr("Настройки"), str(exc))

    def confirm(self, text: str) -> bool:
        return (
            QMessageBox.question(
                self,
                tr("Подтверждение"),
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
                tr(
                    "Принудительно остановить ВМ? Несохранённые данные гостевой ОС могут быть потеряны."
                )
            ):
                return
            if action == "reset" and not self.confirm(
                tr(
                    "Перезагрузить ВМ немедленно? Несохранённые данные гостевой ОС могут быть потеряны."
                )
            ):
                return
            if action == "start":
                self.perform(
                    lambda: self.engine.start(vm.id),
                    lambda accel: self.statusBar().showMessage(
                        tr("ВМ запущена: {value0}. Консоль — в отдельном окне QEMU.", value0=accel)
                    ),
                )
            else:
                self.perform(lambda: self.engine.control(vm.id, action))
        except MultilayerError as exc:
            QMessageBox.warning(self, tr("Управление"), str(exc))

    def cable(self, enabled: bool) -> None:
        try:
            vm = self.selected()
            self.perform(lambda: self.engine.link(vm.id, enabled))
        except MultilayerError as exc:
            QMessageBox.warning(self, tr("Сеть"), str(exc))

    def clone(self) -> None:
        try:
            vm = self.selected()
            name, accepted = QInputDialog.getText(
                self, tr("Клонировать"), tr("Название копии"), text=vm.name + tr(" — копия")
            )
            if accepted and name.strip():
                self.perform(lambda: self.engine.clone(vm.id, name.strip()))
        except MultilayerError as exc:
            QMessageBox.warning(self, tr("Клонировать"), str(exc))

    def snapshots(self) -> None:
        try:
            vm = self.selected()
            action, accepted = QInputDialog.getItem(
                self,
                tr("Снимки диска"),
                tr("Выберите действие (ВМ должна быть выключена)"),
                [tr("Список"), tr("Создать"), tr("Восстановить"), tr("Удалить")],
                editable=False,
            )
            if not accepted:
                return
            operation = {
                tr("Список"): "list",
                tr("Создать"): "create",
                tr("Восстановить"): "restore",
                tr("Удалить"): "delete",
            }[action]
            name = ""
            if operation != "list":
                name, accepted = QInputDialog.getText(
                    self, action, tr("Имя снимка: латинские буквы, цифры, _ или -")
                )
                if not accepted:
                    return
            if operation in ("restore", "delete") and not self.confirm(
                tr("Изменения после снимка/снимок будут потеряны. Продолжить?")
            ):
                return
            self.perform(
                lambda: self.engine.snapshot(vm.id, operation, name),
                lambda data: QMessageBox.information(
                    self, tr("Снимки диска"), json.dumps(data, ensure_ascii=False, indent=2)
                )
                if operation == "list"
                else None,
            )
        except MultilayerError as exc:
            QMessageBox.warning(self, tr("Снимки"), str(exc))

    def delete(self) -> None:
        try:
            vm = self.selected()
            if self.confirm(
                tr(
                    "Удалить «{value0}» вместе с диском и снимками? Отменить удаление нельзя.",
                    value0=vm.name,
                )
            ):
                self.perform(lambda: self.engine.delete(vm.id))
        except MultilayerError as exc:
            QMessageBox.warning(self, tr("Удаление"), str(exc))

    def log(self) -> None:
        try:
            path = self.engine.store.path(self.selected().id) / "qemu.log"
            self.details.setPlainText(
                path.read_text(errors="replace")[-20000:]
                if path.exists()
                else tr("Журнал пока пуст")
            )
        except MultilayerError as exc:
            QMessageBox.warning(self, tr("Журнал"), str(exc))

    def diagnostic(self) -> None:
        self.details.setPlainText(json.dumps(doctor(), ensure_ascii=False, indent=2))

    def maintenance(self) -> None:
        Maintenance(self).exec()

    def export_vm(self) -> None:
        from multilayer.portable import export_vm

        try:
            vm = self.selected()
            self.engine.require_stopped(vm)
            filename, _ = QFileDialog.getSaveFileName(
                self,
                tr("Экспорт ВМ"),
                vm.name + ".multis",
                tr("Мультислой (*.multis)"),
                options=QFileDialog.Option.DontUseNativeDialog,
            )
            if not filename:
                return
            path = Path(filename)
            if path.suffix.lower() != ".multis":
                path = Path(filename + ".multis")
                if path.exists() and not self.confirm(tr("Файл уже существует. Заменить?")):
                    return
            if not self.confirm(
                tr(
                    "Экспорт содержит диск, настройки, снимки и UEFI/TPM, включая секреты гостевой ОС. ISO и внешняя папка обмена не включаются. Храните архив в безопасном месте. Продолжить?"
                )
            ):
                return
            self.perform(
                lambda: export_vm(self.engine, vm.id, path, overwrite=True),
                lambda result: QMessageBox.information(self, tr("Экспорт ВМ"), str(result)),
            )
        except (MultilayerError, OSError) as exc:
            QMessageBox.warning(self, tr("Экспорт ВМ"), str(exc))

    def import_vm(self) -> None:
        from multilayer.portable import import_vm

        filename, _ = QFileDialog.getOpenFileName(
            self,
            tr("Импорт ВМ"),
            "",
            tr("Мультислой (*.multis)"),
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not filename or not self.confirm(
            tr(
                "Будет добавлена выключенная ВМ без замены существующих. ISO и папку обмена подключите заново. Ускорение выбирается автоматически; сетевые настройки и TPM сохраняются и могут требовать Linux. Импортируйте только доверенные архивы. Продолжить?"
            )
        ):
            return
        self.perform(
            lambda: import_vm(self.engine, Path(filename)),
            lambda vm: QMessageBox.information(self, tr("Импорт ВМ"), vm.name),
        )

    def about(self) -> None:
        About(self).exec()

    def closeEvent(self, event: Any) -> None:
        if self.busy:
            QMessageBox.warning(
                self,
                tr("Операция выполняется"),
                tr("Дождитесь завершения операции перед закрытием."),
            )
            event.ignore()
            return
        self.timer.stop()
        QThreadPool.globalInstance().waitForDone()
        event.accept()


class About(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("О программе"))
        self.resize(500, 280)
        layout = QVBoxLayout(self)
        icon = QLabel()
        icon.setPixmap(QIcon(str(logo_path())).pixmap(72, 72))
        layout.addWidget(icon)
        layout.addWidget(QLabel(tr(APP_NAME) + " " + __version__))
        layout.addWidget(QLabel(tr("Автор: {author}", author=tr(AUTHOR))))
        link = QLabel(f'<a href="{WEBSITE}">{WEBSITE}</a>')
        link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        link.setOpenExternalLinks(True)
        layout.addWidget(link)
        description = QLabel(
            tr(
                "Эта программа разрабатывалась для SteamOS, полностью бесплатна для коммерческого и некоммерческого использования."
            )
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        maintenance = QPushButton(tr("Установка / удаление"))
        maintenance.clicked.connect(lambda: Maintenance(self).exec())
        layout.addWidget(maintenance)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class Maintenance(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Мультислой {value0} — установка", value0=__version__))
        self.resize(460, 260)
        layout = QVBoxLayout(self)
        label = QLabel(
            tr(
                "Мультислой {value0}\nУстановить/обновить приложение или удалить его.\nВиртуальные машины и их диски сохраняются. Закройте окна прежней версии перед обновлением.",
                value0=__version__,
            )
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.buttons = []
        self.worker: Worker | None = None
        for title, action in (
            (tr("Установить / обновить"), "install"),
            (tr("Удалить приложение"), "remove"),
        ):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, operation=action: self.execute(operation))
            layout.addWidget(button)
            self.buttons.append(button)
        button = QPushButton(tr("Выйти"))
        button.clicked.connect(self.reject)
        layout.addWidget(button)
        self.buttons.append(button)

    def execute(self, action: str) -> None:
        if (
            action == "remove"
            and QMessageBox.question(
                self, tr("Удалить приложение?"), tr("Виртуальные машины сохранятся.")
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        for button in self.buttons:
            button.setEnabled(False)
        self.worker = Worker(lambda: maintain(action))

        def success(text: str) -> None:
            QMessageBox.information(self, tr("Установка"), text)
            if action == "install":
                self.accept()

        self.worker.signals.done.connect(success)
        self.worker.signals.error.connect(
            lambda text: QMessageBox.critical(self, tr("Ошибка"), text)
        )

        def complete() -> None:
            for button in self.buttons:
                button.setEnabled(True)

        self.worker.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(self.worker)


def launch(home: Path | None = None, maintenance: bool = False) -> int:
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    app.setApplicationName("Multilayer")
    app.setWindowIcon(QIcon(str(logo_path())))
    app.setDesktopFileName("multilayer")
    app.setOrganizationName("nookbat.ru")
    translate_qt(app)
    if maintenance:
        dialog = Maintenance()
        if not dialog.exec():
            return 0
    window = Window(Engine(Store(home)))
    window.show()
    return app.exec()
