import queue
import secrets
import socket
import time
from datetime import datetime, timezone
from PyQt6.QtCore import QThread, pyqtSignal
import dyno_protocol as P


class Cancelled(Exception):
    pass


class DataWorker(QThread):
    samples = pyqtSignal(list)
    status = pyqtSignal(object)
    drive = pyqtSignal(object)
    message = pyqtSignal(str)
    operation_done = pyqtSignal(str, bool)

    def __init__(self, host, port=5001, local_port=40000, bus=1, parent=None):
        super().__init__(parent)
        self.host, self.port, self.local_port, self.bus = host, port, local_port, bus
        self.requests = queue.Queue(maxsize=16)
        self.urgent = queue.Queue()
        self.ui_alive = time.monotonic()
        self.token = secrets.randbelow(30000) + 1
        self.profile = secrets.randbelow(255) + 1
        self.lease = False
        self.last_status = 0
        self.last_hb = 0
        self.batch = []
        self.acks = []
        self.decoder = P.MCANDecoder()
        self.last_flush = time.monotonic()
        self.bad_packets = 0

    def request(self, name, value=None):
        try:
            self.requests.put_nowait((name, value))
        except queue.Full:
            self.message.emit("Command queue full")

    def stop_actuator(self, emergency=False):
        self.urgent.put(P.ESTOP if emergency else P.STOP)

    def next_token(self):
        self.token = (self.token + 1) & 65535
        return self.token

    def send(self, packet):
        ident, payload = packet
        self.sock.sendto(P.pack_mcan(P.CANFrame(self.bus, ident, payload)), self.peer)

    def emergency_pending(self):
        if self.urgent.empty():
            return False
        op = P.STOP
        while not self.urgent.empty():
            if self.urgent.get_nowait() == P.ESTOP:
                op = P.ESTOP
        self.lease = False
        while not self.requests.empty():
            cancelled_name, _ = self.requests.get_nowait()
            self.operation_done.emit(cancelled_name, False)
        packet = P.command(op, self.next_token())

        for _ in range(3):
            self.send(packet)
        self.message.emit(
            "E-stop pressed"
            if op == P.ESTOP
            else "Actuator stop requested"
        )
        return True

    def pump(self):
        now = time.monotonic()
        if self.isInterruptionRequested() or self.emergency_pending():
            raise Cancelled()
        if self.lease:
            if now - self.ui_alive > 0.35 or now - self.last_status > 0.5:
                self.lease = False
                self.send(P.command(P.STOP, self.next_token()))
                raise TimeoutError(
                    "UI or firmware status stale; actuator stopped"
                )
            if now - self.last_hb >= 0.1:
                self.send(P.command(P.HEARTBEAT, 0, self.profile))
                self.last_hb = now
        try:
            data, source = self.sock.recvfrom(65535)
        except socket.timeout:
            data = None
        if data is not None and source == self.peer:
            try:
                decoded = [
                    P.decode_dyno(f, self.bus) for f in self.decoder.decode(data)
                ]
                for item in decoded:
                    if item is None:
                        continue
                    kind, value = item
                    if kind == "sample":
                        self.batch.append(
                            (*value, datetime.now(timezone.utc).isoformat())
                        )
                    elif kind == "status":
                        self.last_status = time.monotonic()
                        self.status.emit(value)
                        if value[0] in (P.DISARMED, P.ESTOPPED, P.FAULT, P.COMPLETE):
                            self.lease = False
                    elif kind == "drive":
                        self.drive.emit(value)
                    else:
                        self.acks.append(value)
                        self.acks = self.acks[-32:]
            except ValueError as exc:
                self.bad_packets += 1
                if self.bad_packets == 1 or self.bad_packets % 100 == 0:
                    self.message.emit(
                        f"Rejected MCAN packet ({self.bad_packets}): {exc}"
                    )
        if time.monotonic() - self.last_flush >= 0.04:
            if self.batch:
                self.samples.emit(self.batch)
                self.batch = []
            self.last_flush = time.monotonic()

    def transact(self, packet, op, token, point_profile=None):
        self.acks.clear()
        for _ in range(4):
            if self.emergency_pending() or self.isInterruptionRequested():
                raise Cancelled()
            self.send(packet)
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                self.pump()
                for ack_op, result, ack_token, value in self.acks:
                    if (ack_op, ack_token) == (op, token) and (
                        point_profile is None or value == point_profile
                    ):
                        if result:
                            error = (
                                P.ERRORS[result]
                                if result < len(P.ERRORS)
                                else str(result)
                            )
                            raise RuntimeError(
                                f"Firmware rejected operation {op}: {error}"
                            )
                        return value
        raise TimeoutError(
            f"No firmware acknowledgement for operation {op}"
        )

    def command(self, op, arg=0):
        token = self.next_token()
        return self.transact(P.command(op, token, arg), op, token)

    def execute(self, name, value):
        if name == "rate":
            self.command(P.RATE, value)
        elif name == "reset":
            self.lease = False
            self.command(P.RESET)
        elif name == "upload":
            self.lease = False
            self.command(P.STOP)
            self.profile = self.profile % 255 + 1
            self.command(P.BEGIN, len(value) | self.profile << 16)
            for i, (ms, pressure) in enumerate(value):
                self.transact(
                    P.profile_point(self.profile, i, ms, pressure), 128, i, self.profile
                )
                if i % 16 == 0:
                    self.message.emit(
                        f"Uploading profile: {i + 1}/{len(value)} points acknowledged"
                    )
            self.command(P.COMMIT, self.profile)
        elif name == "run":
            if time.monotonic() - self.last_status > 0.5:
                raise RuntimeError("No fresh firmware status")
            for term, gain in enumerate(value):
                token = self.next_token()
                self.transact(P.gain(term, token, gain), 16 + term, token)
            self.lease = True
            self.send(P.command(P.HEARTBEAT, 0, self.profile))
            self.last_hb = time.monotonic()
            self.command(P.START, self.profile)
            self.lease = True
        else:
            raise ValueError(f"Unknown operation {name}")

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.peer = (socket.gethostbyname(self.host), self.port)
            self.sock.bind(("0.0.0.0", self.local_port))
            self.sock.settimeout(0.01)
            self.message.emit(
                f"Listening on UDP {self.local_port}; MCAN {self.peer[0]}:{self.port}, CAN bus {self.bus}"
            )

            self.command(P.STOP)
            self.command(P.RATE, 200)
            while not self.isInterruptionRequested():
                name = None
                try:
                    self.pump()
                    if not self.requests.empty():
                        name, value = self.requests.get_nowait()
                        self.execute(name, value)
                        self.operation_done.emit(name, True)
                except (Cancelled, RuntimeError, TimeoutError, ValueError) as exc:
                    self.lease = False
                    self.send(P.command(P.STOP, self.next_token()))
                    if name:
                        self.operation_done.emit(name, False)
                    if str(exc):
                        self.message.emit(str(exc))
        except (OSError, RuntimeError, TimeoutError, Cancelled) as exc:
            self.message.emit(f"Connection ended: {exc}")
        finally:
            self.lease = False
            if hasattr(self, "peer"):
                try:
                    for _ in range(3):
                        self.send(P.command(P.STOP, self.next_token()))
                except OSError:
                    pass
            self.sock.close()
