import csv
import math
from pathlib import Path
from openpyxl import load_workbook, Workbook
from openpyxl.chart import ScatterChart, Series, Reference
from dyno_protocol import SENSORS, PRESSURE_SENSOR_MAX_PSI


def validate_profile(rows):
    points = []
    for line, row in enumerate(rows, 2):
        if not row or all(v is None or v == "" for v in row):
            continue
        if len(row) < 2 or any(isinstance(v, bool) for v in row[:2]):
            raise ValueError(f"Row {line}: expected time_s and pressure_psi")
        try:
            seconds, pressure = float(row[0]), float(row[1])
        except (ValueError, TypeError):
            raise ValueError(
                f"Row {line}: time and pressure values aren't numbers'"
            ) from None
        if not math.isfinite(seconds) or not math.isfinite(pressure):
            raise ValueError(f"Row {line}: unreadable text")
        ms = round(seconds * 1000)
        if (
            seconds < 0
            or ms > 3_600_000
            or not 0 <= pressure <= PRESSURE_SENSOR_MAX_PSI
        ):
            raise ValueError(
                f"Row {line}: time must be 0..3600 s, pressure 0..{PRESSURE_SENSOR_MAX_PSI:g} psi"
            )
        if (not points and ms != 0) or (points and ms <= points[-1][0]):
            raise ValueError(
                f"Row {line}: first time point must be 0s"
            )
        points.append((ms, round(pressure * 10) / 10))
        if len(points) > 256:
            raise ValueError("Maximum 256 profile points")
    if len(points) < 2:
        raise ValueError("At least two pressure profile points are required")
    return points


def read_profile(path):
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    elif path.suffix.lower() == ".xlsx":
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            rows = list(book.active.iter_rows(values_only=True))
        finally:
            book.close()
    else:
        raise ValueError("Choose an .xlsx or .csv profile")
    if not rows or tuple(str(v).strip().lower() for v in rows[0][:2]) != (
        "time_s",
        "pressure_psi",
    ):
        raise ValueError("First two column headings must be time_s, pressure_psi")
    return validate_profile(rows[1:])


def export_data(path, channels):
    if Path(path).suffix.lower() == ".csv":
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                ("sensor", "unit", "device_time_s", "received_utc", "value", "valid")
            )
            for (name, unit), samples in zip(SENSORS, channels):
                for ms, utc, value in samples:
                    valid = math.isfinite(value)
                    writer.writerow(
                        (name, unit, ms / 1000, utc, value if valid else "", valid)
                    )
        return
    book = Workbook()
    book.remove(book.active)
    for (name, unit), samples in zip(SENSORS, channels):
        sheet = book.create_sheet(name[:31])
        sheet.append(("Device time (s)", "Received UTC", f"Value ({unit})", "Valid"))
        for ms, utc, value in samples:
            valid = math.isfinite(value)
            sheet.append((ms / 1000, utc, value if valid else None, valid))
        sheet.freeze_panes = "A2"
        for column, width in (("A", 22), ("B", 34), ("C", 22), ("D", 12)):
            sheet.column_dimensions[column].width = width
        if samples:
            chart = ScatterChart()
            chart.title, chart.x_axis.title, chart.y_axis.title = (
                name,
                "Device time (s)",
                unit,
            )
            chart.series.append(
                Series(
                    Reference(sheet, min_col=3, min_row=2, max_row=sheet.max_row),
                    Reference(sheet, min_col=1, min_row=2, max_row=sheet.max_row),
                    title=name,
                )
            )
            chart.width, chart.height = 24, 12
            sheet.add_chart(chart, "F2")
    book.save(path)
