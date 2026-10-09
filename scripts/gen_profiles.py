"""Generate reproducible synthetic household load profiles."""

from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20261009
YEAR = 2025
ZONE = ZoneInfo("Australia/Brisbane")
OUTPUT = Path(__file__).parents[1] / "tests" / "profiles"
HEADER = ("interval_end", "register", "import_kwh", "export_kwh")


def _timestamps() -> list[datetime]:
    start = datetime(YEAR, 1, 1, tzinfo=ZONE)
    return [start + timedelta(minutes=30 * index) for index in range(365 * 48)]


def _normalise(values: list[float], annual_total: float) -> list[float]:
    scale = annual_total / sum(values)
    return [value * scale for value in values]


def _base_loads(starts: list[datetime], rng: random.Random) -> list[float]:
    shape = []
    for start in starts:
        hour = start.hour + start.minute / 60
        morning = math.exp(-((hour - 7.0) / 2.0) ** 2)
        evening = 2.2 * math.exp(-((hour - 19.0) / 3.0) ** 2)
        base = 0.16 + 0.13 * morning + 0.27 * evening
        shape.append(base * (0.85 + 0.3 * rng.random()))
    return _normalise(shape, 5000.0)


def _solar_generation(starts: list[datetime]) -> list[float]:
    shape = []
    for start in starts:
        hour = start.hour + start.minute / 60
        seasonal = 0.75 + 0.25 * math.cos(2 * math.pi * (start.timetuple().tm_yday - 15) / 365)
        sunlight = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
        shape.append(6.6 * sunlight * seasonal / 2)
    return _normalise(shape, 6500.0)


def _write(name: str, rows: list[tuple[datetime, str, float, float]]) -> None:
    with (OUTPUT / f"{name}.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(HEADER)
        for end, register, import_qty, export_qty in rows:
            writer.writerow(
                (
                    end.isoformat(timespec="minutes"),
                    register,
                    f"{import_qty:.4f}",
                    f"{export_qty:.4f}",
                )
            )


def _electric_rows(
    starts: list[datetime],
    loads: list[float],
    generation: list[float] | None = None,
    battery: bool = False,
) -> list[tuple[datetime, str, float, float]]:
    rows = []
    stored = 0.0
    capacity = 13.5
    for index, (start, load) in enumerate(zip(starts, loads, strict=True)):
        produced = generation[index] if generation is not None else 0.0
        net = load - produced
        if battery:
            if net < 0:
                charge = min(-net, 5.0 / 2, (capacity - stored) / 0.95)
                stored += charge * 0.95
                net += charge
            elif net > 0:
                discharge = min(net, 5.0 / 2, stored * 0.95)
                stored -= discharge / 0.95
                net -= discharge
        rows.append(
            (
                start + timedelta(minutes=30),
                "general",
                max(0.0, net),
                max(0.0, -net),
            )
        )
    return rows


def _ev_profile(
    starts: list[datetime], loads: list[float], rng: random.Random
) -> list[tuple[datetime, str, float, float]]:
    extra = [0.0] * len(starts)
    for day in range(365):
        chosen = rng.randrange(day * 48 + 44, day * 48 + 48)
        for index in range(chosen, min(chosen + 6, len(starts))):
            extra[index] = 1.0
    extra = _normalise(extra, 3000.0)
    return _electric_rows(starts, [base + ev for base, ev in zip(loads, extra, strict=True)])


def _controlled_profile(
    starts: list[datetime], loads: list[float]
) -> list[tuple[datetime, str, float, float]]:
    rows = _electric_rows(starts, loads)
    extra = [
        1.0 if start.hour >= 22 or start.hour < 6 else 0.0
        for start in starts
    ]
    extra = _normalise(extra, 2000.0)
    rows.extend(
        (
            start + timedelta(minutes=30),
            "controlled_load_1",
            amount,
            0.0,
        )
        for start, amount in zip(starts, extra, strict=True)
    )
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def _gas_profile() -> list[tuple[datetime, str, float, float]]:
    days = [date(YEAR, 1, 1) + timedelta(days=index) for index in range(365)]
    weighted = [
        0.35 + 1.65 * (0.5 - 0.5 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 200) / 365))
        for day in days
    ]
    m3_per_mj = 1 / (38.6 * 0.98)
    quantities = _normalise(weighted, 20000.0 * m3_per_mj)
    return [
        (
            datetime.combine(day + timedelta(days=1), time.min, ZONE),
            "general",
            quantity,
            0.0,
        )
        for day, quantity in zip(days, quantities, strict=True)
    ]


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    starts = _timestamps()
    rng = random.Random(SEED)
    loads = _base_loads(starts, rng)
    generation = _solar_generation(starts)
    _write("no_solar", _electric_rows(starts, loads))
    _write("solar_only", _electric_rows(starts, loads, generation))
    _write("solar_battery", _electric_rows(starts, loads, generation, battery=True))
    _write("ev_heavy", _ev_profile(starts, loads, random.Random(SEED)))
    _write("controlled_load", _controlled_profile(starts, loads))
    _write("gas_household", _gas_profile())


if __name__ == "__main__":
    main()
