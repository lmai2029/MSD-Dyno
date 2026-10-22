from dataclasses import dataclass
import struct

VERSION = 1
PRESSURE_SENSOR_MAX_PSI = 3000.0
SENSORS = (
    ("Pad temperature", "degC"),
    ("Caliper temperature", "degC"),
    ("Brake pressure", "psi"),
    ("Rotor IR temperature", "degC"),
    ("Load cell", "N"),
)
STATUS, DRIVE, CONTROL, POINT, ACK, GAIN = 0x610, 0x611, 0x620, 0x621, 0x622, 0x623
STOP, ESTOP, RESET, START, BEGIN, COMMIT, RATE, HEARTBEAT = range(8)
DISARMED, READY, RUNNING, ESTOPPED, FAULT, COMPLETE = range(6)
STATES = ("DISARMED", "READY", "RUNNING", "E-STOP", "FAULT", "COMPLETE")
ERRORS = (
    "OK",
    "Bad length/version",
    "Invalid value",
    "Invalid state",
    "Incomplete profile",
    "Fault active",
)
FAULTS = {
    1: "Calibration/control not commissioned",
    2: "Pressure invalid/over limit",
    4: "Thermocouple 1",
    8: "Thermocouple 2",
    16: "IR sensor",
    32: "Load cell",
    64: "CAN failure/overflow",
    128: "Heartbeat timeout",
    256: "Control timing overrun",
}


@dataclass(frozen=True)
class CANFrame:
    bus: int
    can_id: int
    data: bytes
    timestamp_us: int = 0
    fd: bool = False


def pack_mcan(frame):
    if (
        frame.bus not in (1, 2, 3, 5)
        or frame.fd
        or not 0 <= frame.can_id <= 0x7FF
        or len(frame.data) > 8
    ):
        raise ValueError(
            "Only standard classic CAN on MCAN buses 1, 2, 3 or 5 is supported for TX"
        )
    return (
        struct.pack("<BBHI", frame.bus, len(frame.data), 0, frame.can_id) + frame.data
    )


class MCANDecoder:
    def __init__(self):
        self.high = 0

    def decode(self, packet):

        pos, high, frames = 0, self.high, []
        while pos < len(packet):
            if len(packet) - pos < 8:
                raise ValueError("Truncated MCAN header")
            bus, flags, low, ident = struct.unpack_from("<BBHI", packet, pos)
            size, fd = flags & 127, bool(flags & 128)
            pos += 8
            if size > (64 if fd else 8) or pos + size > len(packet):
                raise ValueError("Invalid MCAN payload length")
            payload = packet[pos : pos + size]
            pos += size
            if bus == 4:
                if size != 4 or fd:
                    raise ValueError("Invalid MCAN timestamp record")
                high = struct.unpack("<I", payload)[0] << 16
            else:
                frames.append(CANFrame(bus, ident, payload, high | low, fd))
        self.high = high
        return frames


def command(op, token, argument=0):
    return CONTROL, struct.pack("<BBHI", op, VERSION, token, argument)


def profile_point(profile_id, index, time_ms, pressure):
    return POINT, struct.pack("<BBIH", profile_id, index, time_ms, round(pressure * 10))


def gain(term, token, value):
    return GAIN, struct.pack("<BBHf", term, VERSION, token, value)


def decode_dyno(frame, bus):
    if frame.bus != bus or frame.fd or len(frame.data) != 8:
        return None
    if 0x600 <= frame.can_id < 0x605:
        return "sample", (frame.can_id - 0x600, *struct.unpack("<If", frame.data))
    if frame.can_id == STATUS:
        version, state, faults, index, rate = struct.unpack("<BBHHH", frame.data)
        if version != VERSION or state >= len(STATES) or not 1 <= rate <= 200:
            raise ValueError("Incompatible firmware status")
        return "status", (state, faults, index, rate)
    if frame.can_id == DRIVE:
        return "drive", struct.unpack("<fhH", frame.data)[:2]
    if frame.can_id == ACK:
        return "ack", struct.unpack("<BBHI", frame.data)
    return None
