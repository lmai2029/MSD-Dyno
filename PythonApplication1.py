import sys
import math
import time
from collections import deque
from pathlib import Path
from PyQt6 import QtCore, QtWidgets
import pyqtgraph as pg
import dyno_protocol as P
from dyno_worker import DataWorker
from dyno_data import read_profile, export_data

PLOT_HISTORY_SAMPLES = 3000
MAX_SAMPLES_PER_CHANNEL = 1_000_000
STATUS_STALE_SECONDS = 0.6
REFRESH_INTERVAL_MS = 33
PLOT_COLORS = ("#d35400", "#8e44ad", "#00796b", "#c0392b", "#2471a3")


class DynoGUI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Formula Brake Dyno")
        self.resize(1350, 900)
        self.worker = None
        self.recording = False
        self.profile_points = None
        self.uploaded = False
        self.busy = False
        self.pending_action = None
        self.start_after_upload = False
        self.state = None
        self.status_time = 0
        self.full_telemetry = [[] for _ in P.SENSORS]
        self.display = [deque(maxlen=PLOT_HISTORY_SAMPLES) for _ in P.SENSORS]
        self.last_raw = [None] * len(P.SENSORS)
        self.clock_wraps = [0] * len(P.SENSORS)
        self.last_display = [-1e9] * len(P.SENSORS)
        self.maxima = [-math.inf] * len(P.SENSORS)
        self.dirty = True
        self.curves, self.labels, self.rate_inputs = [], [], []
        self._build_ui()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_INTERVAL_MS)
        self.refresh()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        title = QtWidgets.QLabel("BRAKE DYNAMOMETER")
        title.setStyleSheet("font-size:22px; font-weight:bold; color:#154360")
        layout.addWidget(title)
        connection = QtWidgets.QHBoxLayout()
        self.host = QtWidgets.QLineEdit("192.168.72.100")
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5001)
        self.local_port = QtWidgets.QSpinBox()
        self.local_port.setRange(1, 65535)
        self.local_port.setValue(40000)
        self.bus = QtWidgets.QComboBox()
        self.bus.addItems(["1", "2", "3", "5"])
        for label, widget in (
            ("MCAN IPv4", self.host),
            ("Gateway port", self.port),
            ("Listen port", self.local_port),
            ("CAN bus", self.bus),
        ):
            connection.addWidget(QtWidgets.QLabel(label))
            connection.addWidget(widget)
        layout.addLayout(connection)
        control_menu = self.menuBar().addMenu("Control")
        reset_action = control_menu.addAction("E-stop")
        reset_action.triggered.connect(self.reset_fault)
        self.status_label = QtWidgets.QLabel("Disconnected")
        layout.addWidget(self.status_label)
        body = QtWidgets.QHBoxLayout()
        layout.addLayout(body, 1)
        grid = QtWidgets.QGridLayout()
        body.addLayout(grid, 1)
        for row in range(3):
            grid.setRowStretch(row, 1)
        for col in range(2):
            grid.setColumnStretch(col, 1)
        pg.setConfigOptions(background="w", foreground="#263238", antialias=False)
        for index, (name, unit) in enumerate(P.SENSORS):
            panel = self._create_sensor_panel(index, name, unit)
            grid.addWidget(panel, index // 2, index % 2)
        grid.addWidget(self._create_profile_panel(), 2, 1)
        body.addLayout(self._create_controls())
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setMaximumHeight(90)
        layout.addWidget(self.log)

    def _create_sensor_panel(self, sensor_index, name, unit):
        box = QtWidgets.QGroupBox(name)
        box.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored
        )
        boxlayout = QtWidgets.QVBoxLayout(box)
        plot = pg.PlotWidget()
        plot.setMinimumHeight(90)
        plot.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored
        )
        plot.setLabel("left", unit)
        plot.setLabel("bottom", "Device time", units="s")
        plot.showGrid(x=True, y=True, alpha=0.15)
        self.curves.append(
            plot.plot(
                pen=pg.mkPen(PLOT_COLORS[sensor_index], width=2), connect="finite"
            )
        )
        boxlayout.addWidget(plot, 1)
        row = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel("Highest sampled value: --")
        label.setWordWrap(True)
        self.labels.append(label)
        row.addWidget(label, 1)
        spin = QtWidgets.QSpinBox()
        spin.setRange(1, 8 if sensor_index < 2 else 200)
        spin.setValue(8 if sensor_index < 2 else 50)
        self.rate_inputs.append(spin)
        spin.setSuffix(" Hz")
        row.addWidget(QtWidgets.QLabel("Sampling rate"))
        row.addWidget(spin)
        boxlayout.addLayout(row)
        return box

    def _create_profile_panel(self):
        profile_box = QtWidgets.QGroupBox("Pressure profile")
        profile_box.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored
        )
        profile_layout = QtWidgets.QVBoxLayout(profile_box)
        self.profile_plot = pg.PlotWidget()
        self.profile_plot.setMinimumHeight(90)
        self.profile_plot.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored
        )
        self.profile_plot.setLabel("bottom", "Profile time", units="s")
        self.profile_plot.setLabel("left", "psi")
        self.profile_curve = self.profile_plot.plot(pen=pg.mkPen("#00796b", width=2))
        profile_layout.addWidget(self.profile_plot, 1)
        self.profile_label = QtWidgets.QLabel("Import Excel")
        self.profile_label.setWordWrap(True)
        profile_layout.addWidget(self.profile_label)
        self.profile_label.setMaximumHeight(42)
        self.drive_label = QtWidgets.QLabel("Target: -- psi | PWM: -- %")
        profile_layout.addWidget(self.drive_label)
        self.drive_label.setMaximumHeight(24)
        return profile_box

    def _create_controls(self):
        sidebar = QtWidgets.QVBoxLayout()
        self.buttons = {}
        actions = (
            ("start", "Start", self.start_logging),
            ("pause", "Pause", self.pause_logging),
            ("clear", "Clear", self.clear_data),
            ("save", "Save", self.save_data),
            ("import", "Import", self.import_data),
            ("stop", "Stop", self.estop),
            ("close", "Close", self.close),
        )
        for key, text, callback in actions:
            button = QtWidgets.QPushButton(text)
            button.setMinimumHeight(38)
            button.clicked.connect(callback)
            sidebar.addWidget(button)
            self.buttons[key] = button
        self.buttons["stop"].setStyleSheet(
            "background:#b71c1c;color:white;font-weight:bold;min-height:50px"
        )
        self.gains = []
        for label in ("Kp (V/psi)", "Ki (V/psi/s)", "Kd (V s/psi)"):
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel(label))
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0, 10000)
            spin.setDecimals(5)
            spin.setSingleStep(0.01)
            self.gains.append(spin)
            row.addWidget(spin)
            sidebar.addLayout(row)
        sidebar.addStretch()
        return sidebar

    def tell(self, message):
        self.log.appendPlainText(message)

    def connect_gateway(self):
        if self.worker and self.worker.isRunning():
            return
        self.worker = DataWorker(
            self.host.text().strip(),
            self.port.value(),
            self.local_port.value(),
            int(self.bus.currentText()),
            self,
        )
        self.worker.samples.connect(self.update_samples)
        self.worker.status.connect(self.update_status)
        self.worker.drive.connect(self.update_drive)
        self.worker.message.connect(self.tell)
        self.worker.operation_done.connect(self.operation_done)
        self.worker.finished.connect(self.disconnected)
        self.state = None
        self.status_time = 0
        self.uploaded = False
        self.worker.start()

    def disconnected(self):
        self.state = None
        self.status_time = 0
        self.busy = False
        self.uploaded = False
        self.pending_action = None
        self.start_after_upload = False
        self.tell(
            "Disconnected. Press Start or Import Excel to reconnect"
        )

    def request(self, name, value=None):
        if self.worker and self.worker.isRunning() and not self.busy:
            self.busy = True
            self.worker.request(name, value)

    def operation_done(self, name, success):
        self.busy = False
        if name == "upload":
            self.uploaded = success
            if success:
                self.profile_label.setText(
                    "Excel profile uploaded. Press Start to record and run the PID profile."
                )
            if self.start_after_upload:
                self.start_after_upload = False
                if success:
                    self.run_profile()
        if name in ("run", "reset"):
            self.uploaded = False
        self.tell(f"{name}: {'acknowledged' if success else 'failed'}")

    def update_drive(self, data):
        target_psi, duty_permille = data
        self.drive_label.setText(
            f"Target: {target_psi:.1f} psi | PWM: {duty_permille / 10:.1f} %"
        )

    def update_status(self, data):
        self.state, faults, index, rate = data
        self.status_time = time.monotonic()
        faults_text = (
            ", ".join(text for bit, text in P.FAULTS.items() if faults & bit) or "none"
        )
        self.status_label.setText(
            f"{P.STATES[self.state]} | Faults: {faults_text} | ADC telemetry: {rate} Hz | Profile segment: {index}"
        )
        self.status_label.setWordWrap(True)


        if self.state in (P.RUNNING, P.ESTOPPED, P.FAULT, P.COMPLETE):
            self.uploaded = False

    def refresh(self):
        now = time.monotonic()
        connected = bool(self.worker and self.worker.isRunning())
        if connected:
            self.worker.ui_alive = now
        fresh = connected and now - self.status_time < STATUS_STALE_SECONDS
        if not fresh:
            self.status_label.setText("DISCONNECTED: no fresh firmware status")
        for widget in (self.host, self.port, self.local_port, self.bus):
            widget.setEnabled(not connected)
        self.buttons["start"].setEnabled(not self.busy)
        self.buttons["import"].setEnabled(not self.busy and self.state != P.RUNNING)
        if fresh and self.pending_action and not self.busy:
            action, self.pending_action = self.pending_action, None
            if action == "start":
                self.start_logging()
            elif action == "import":
                self.upload_profile()
        if self.dirty:
            for curve, points in zip(self.curves, self.display):
                if points:
                    x, y = zip(*points)
                    curve.setData(x, y)
                else:
                    curve.setData([], [])
            self.dirty = False

    def ensure_connection(self, action):
        if not self.worker or not self.worker.isRunning():
            self.pending_action = action
            self.connect_gateway()
            return False
        if time.monotonic() - self.status_time >= STATUS_STALE_SECONDS:
            self.pending_action = action
            self.tell("Waiting for fresh firmware status")
            return False
        return True

    def start_logging(self):
        self.recording = True
        if not self.ensure_connection("start"):
            return
        self.tell("Data acquisition started")
        if self.state == P.RUNNING or self.profile_points is None:
            return
        if self.state in (P.ESTOPPED, P.FAULT):
            self.tell(
                "PID is stopped"
            )
            return
        if self.gains[0].value() <= 0:
            self.tell(
                "Data acquisition is running. Enter commissioned PID gains, then press Start to run the imported profile."
            )
            return
        if self.uploaded:
            self.run_profile()
        else:
            self.start_after_upload = True
            self.upload_profile()

    def pause_logging(self):
        self.recording = False
        self.uploaded = False
        self.busy = False
        self.pending_action = None
        self.start_after_upload = False
        if self.worker:
            self.worker.stop_actuator()
        self.tell(
            "Data acquisition paused; actuator stopped. Start begins the imported profile again from time zero."
        )

    def clear_data(self):
        self.full_telemetry = [[] for _ in P.SENSORS]
        self.display = [deque(maxlen=PLOT_HISTORY_SAMPLES) for _ in P.SENSORS]
        self.last_display = [-1e9] * len(P.SENSORS)
        self.maxima = [-math.inf] * len(P.SENSORS)
        self.dirty = True
        for label in self.labels:
            label.setText("Highest sampled value: --")
        self.tell("Recorded data cleared")

    def estop(self):
        self.recording = False
        self.pending_action = None
        self.start_after_upload = False
        self.uploaded = False
        self.busy = False
        if self.worker and self.worker.isRunning():
            self.worker.stop_actuator(True)
        else:
            self.tell(
                "Actuator E-stop instruction didn't pass"
            )

    def reset_fault(self):
        self.request("reset")

    def import_data(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Pressure profile", "", "Excel workbook (*.xlsx)"
        )
        if not path:
            return
        try:
            points = read_profile(path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Profile not imported", str(exc))
            return
        self.profile_points = points
        self.uploaded = False
        self.profile_curve.setData(
            [p[0] / 1000 for p in points], [p[1] for p in points]
        )
        self.profile_label.setText(
            f"{Path(path).name}: {len(points)} points, {points[-1][0] / 1000:g} s, peak {max(p[1] for p in points):g} psi. Uploading..."
        )
        if self.ensure_connection("import"):
            self.upload_profile()

    def upload_profile(self):
        self.request("upload", self.profile_points)

    def run_profile(self):
        if self.gains[0].value() <= 0:
            self.tell(
                "Enter commissioned PID gains before starting"
            )
            return
        self.request("run", [g.value() for g in self.gains])

    def update_samples(self, batch):
        for i, raw_ms, value, utc in batch:
            previous = self.last_raw[i]
            if previous is not None and raw_ms < previous:
                if previous - raw_ms > 0x80000000:
                    self.clock_wraps[i] += 1
                elif raw_ms < 2000 and previous > 5000:
                    self.recording = False
                    self.uploaded = False
                    self.last_raw = [None] * len(P.SENSORS)
                    self.clock_wraps = [0] * len(P.SENSORS)
                    self.tell(
                        "Device clock reset detected. save/clear before starting a new recording."
                    )
                    return
                else:
                    continue
            if raw_ms == previous:
                continue
            self.last_raw[i] = raw_ms
            ms = raw_ms + self.clock_wraps[i] * (1 << 32)
            if not self.recording:
                continue
            if len(self.full_telemetry[i]) >= MAX_SAMPLES_PER_CHANNEL:
                self.pause_logging()
                self.tell(
                    "Recording limit reached."
                )
                return


            period = 1000 / self.rate_inputs[i].value()
            if ms - self.last_display[i] < period:
                continue
            self.last_display[i] += period
            if ms - self.last_display[i] >= period:
                self.last_display[i] = ms
            self.full_telemetry[i].append((ms, utc, value))
            self.display[i].append((ms / 1000, value))
            self.dirty = True
            if math.isfinite(value):
                self.maxima[i] = max(self.maxima[i], value)
            maximum = (
                f"{self.maxima[i]:.2f} {P.SENSORS[i][1]}"
                if math.isfinite(self.maxima[i])
                else "--"
            )
            current = (
                f"{value:.2f} {P.SENSORS[i][1]}"
                if math.isfinite(value)
                else "INVALID / UNCALIBRATED"
            )
            self.labels[i].setText(
                f"Highest sampled value: {maximum} | Current: {current}"
            )

    def save_data(self):
        self.pause_logging()
        path, selected = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export recording", "Dyno_recording.xlsx", "Excel workbook (*.xlsx)"
        )
        if not path:
            return
        if Path(path).suffix.lower() != ".xlsx":
            path += ".xlsx"
        try:
            export_data(path, self.full_telemetry)
            self.tell(f"Saved all recorded samples: {path}")
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Export failed", str(exc))

    def closeEvent(self, event):
        self.timer.stop()
        if self.worker and self.worker.isRunning():
            self.worker.stop_actuator()
            self.worker.requestInterruption()
            if not self.worker.wait(2000):
                event.ignore()
                self.timer.start(REFRESH_INTERVAL_MS)
                self.tell("Waiting for network worker to stop; retry Close")
                return
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    gui = DynoGUI()
    gui.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
