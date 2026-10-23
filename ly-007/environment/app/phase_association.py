#!/usr/bin/env python3
"""Provisional station-wise phase catalogue helper."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


LEDGER_KEYS = {
    "campaign_id",
    "entry_hash",
    "input_digest",
    "output_digest",
    "previous_hash",
    "run_id",
}


class Refused(Exception):
    code = "BAD_INPUT"


class BadInput(Refused):
    code = "BAD_INPUT"


class LedgerConflict(Refused):
    code = "LEDGER_CONFLICT"


def quantize(value: float) -> float:
    return float(
        Decimal(str(value)).quantize(
            Decimal("0.000001"),
            rounding=ROUND_HALF_UP,
        )
    )


def canonical_bytes(value: object, newline: bool = True) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if newline:
        text += "\n"
    return text.encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_duplicate_members(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BadInput("duplicate")
        result[key] = value
    return result


def parse_json(data: bytes) -> object:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=no_duplicate_members,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, BadInput) as exc:
        raise BadInput("json") from exc


def exact_object(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise BadInput("keys")
    return value


def number(value: object) -> float:
    if type(value) not in (int, float):
        raise BadInput("number")
    result = float(value)
    if not math.isfinite(result):
        raise BadInput("finite")
    return result


def identifier(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BadInput("identifier")
    return value


def validate(value: object) -> dict:
    payload = exact_object(
        value,
        {
            "arrivals",
            "campaign_id",
            "clock_bound_s",
            "event_count",
            "run_id",
            "stations",
            "velocity_km_s",
        },
    )
    campaign_id = identifier(payload["campaign_id"])
    run_id = identifier(payload["run_id"])
    if type(payload["event_count"]) is not int or payload["event_count"] != 3:
        raise BadInput("event count")
    clock_bound = number(payload["clock_bound_s"])
    if not 0.0 < clock_bound <= 0.25:
        raise BadInput("clock bound")
    velocity = exact_object(payload["velocity_km_s"], {"p", "s"})
    vp = number(velocity["p"])
    vs = number(velocity["s"])
    if not 1.0 <= vs < vp <= 12.0:
        raise BadInput("velocity")
    stations = payload["stations"]
    if not isinstance(stations, list) or len(stations) != 8:
        raise BadInput("stations")
    station_rows = []
    station_ids = set()
    for raw in stations:
        row = exact_object(raw, {"station_id", "x_km", "y_km"})
        station_id = identifier(row["station_id"])
        if station_id in station_ids:
            raise BadInput("duplicate station")
        station_ids.add(station_id)
        x_km = number(row["x_km"])
        y_km = number(row["y_km"])
        if not -250 <= x_km <= 250 or not -250 <= y_km <= 250:
            raise BadInput("station range")
        station_rows.append(
            {"station_id": station_id, "x_km": x_km, "y_km": y_km}
        )
    arrivals = payload["arrivals"]
    if not isinstance(arrivals, list) or len(arrivals) != 48:
        raise BadInput("arrivals")
    seen = set()
    normalized = []
    counts = {station_id: 0 for station_id in station_ids}
    for raw in arrivals:
        row = exact_object(raw, {"arrival_id", "station_id", "time_s"})
        arrival_id = identifier(row["arrival_id"])
        if arrival_id in seen:
            raise BadInput("duplicate arrival")
        seen.add(arrival_id)
        station_id = identifier(row["station_id"])
        if station_id not in station_ids:
            raise BadInput("unknown station")
        time_s = number(row["time_s"])
        if not 0.0 <= time_s <= 1000.0:
            raise BadInput("time range")
        counts[station_id] += 1
        normalized.append(
            {
                "arrival_id": arrival_id,
                "station_id": station_id,
                "time_s": time_s,
            }
        )
    if any(count != 6 for count in counts.values()):
        raise BadInput("station arrivals")
    normalized.sort(key=lambda row: row["arrival_id"])
    station_rows.sort(key=lambda row: row["station_id"])
    return {
        "arrivals": normalized,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "stations": station_rows,
    }


def build_result(payload: dict) -> dict:
    by_station = {}
    for arrival in payload["arrivals"]:
        by_station.setdefault(arrival["station_id"], []).append(arrival)
    events = [[], [], []]
    for station_id, rows in sorted(by_station.items()):
        rows.sort(key=lambda row: (row["time_s"], row["arrival_id"]))
        for event_index in range(3):
            events[event_index].append(
                (
                    station_id,
                    rows[event_index * 2],
                    rows[event_index * 2 + 1],
                )
            )
    station_by_id = {
        station["station_id"]: station for station in payload["stations"]
    }
    provisional = []
    for rows in events:
        first_station = station_by_id[rows[0][0]]
        p_ids = sorted(pair[1]["arrival_id"] for pair in rows)
        s_ids = sorted(pair[2]["arrival_id"] for pair in rows)
        origin = min(pair[1]["time_s"] for pair in rows) - 1.0
        provisional.append(
            {
                "hypocenter": {
                    "depth_km": 5.0,
                    "x_km": first_station["x_km"],
                    "y_km": first_station["y_km"],
                },
                "origin_time_s": origin,
                "p_arrival_ids": p_ids,
                "s_arrival_ids": s_ids,
            }
        )
    provisional.sort(
        key=lambda event: (
            event["origin_time_s"],
            event["p_arrival_ids"],
        )
    )
    return {
        "campaign_id": payload["campaign_id"],
        "events": [
            {
                "event_id": f"EVT-{index:02d}",
                "hypocenter": {
                    "depth_km": quantize(event["hypocenter"]["depth_km"]),
                    "x_km": quantize(event["hypocenter"]["x_km"]),
                    "y_km": quantize(event["hypocenter"]["y_km"]),
                },
                "origin_time_s": quantize(event["origin_time_s"]),
                "p_arrival_ids": event["p_arrival_ids"],
                "residual_rms": 0.0,
                "s_arrival_ids": event["s_arrival_ids"],
            }
            for index, event in enumerate(provisional, start=1)
        ],
        "global_residual_rms": 0.0,
        "run_id": payload["run_id"],
        "schema_version": 6,
        "stations": [
            {"clock_offset_s": 0.0, "station_id": station["station_id"]}
            for station in payload["stations"]
        ],
    }


def require_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("digest")
    return value


def load_ledger(path: Path) -> tuple[bytes, list[dict]]:
    if not path.exists():
        return b"", []
    raw = path.read_bytes()
    if not raw:
        return b"", []
    try:
        if not raw.endswith(b"\n"):
            raise ValueError("newline")
        previous = "0" * 64
        seen = set()
        entries = []
        for line in raw.splitlines():
            if not line:
                raise ValueError("empty")
            entry = exact_object(parse_json(line), LEDGER_KEYS)
            if not all(isinstance(entry[key], str) for key in LEDGER_KEYS):
                raise ValueError("type")
            if canonical_bytes(entry, newline=False) != line:
                raise ValueError("canonical")
            for key in ("entry_hash", "input_digest", "output_digest"):
                require_digest(entry[key])
            if entry["previous_hash"] != previous:
                raise ValueError("chain")
            if entry["run_id"] in seen:
                raise ValueError("duplicate")
            body = {
                key: value for key, value in entry.items() if key != "entry_hash"
            }
            if digest_bytes(canonical_bytes(body, newline=False)) != entry["entry_hash"]:
                raise ValueError("hash")
            previous = entry["entry_hash"]
            seen.add(entry["run_id"])
            entries.append(entry)
        return raw, entries
    except (BadInput, OSError, ValueError):
        return b"", []


def make_entry(
    payload: dict,
    input_digest: str,
    result_bytes: bytes,
    previous_hash: str,
) -> dict:
    entry = {
        "campaign_id": payload["campaign_id"],
        "input_digest": input_digest,
        "output_digest": digest_bytes(result_bytes),
        "previous_hash": previous_hash,
        "run_id": payload["run_id"],
    }
    entry["entry_hash"] = digest_bytes(canonical_bytes(entry, newline=False))
    return entry


def stage_file(path: Path, data: bytes) -> Path:
    descriptor, raw_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(raw_name)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def replace_files(items: tuple[tuple[Path, bytes], ...]) -> None:
    staged = []
    try:
        for path, data in items:
            staged.append((stage_file(path, data), path))
        for temporary, path in staged:
            temporary.replace(path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def replay_is_complete(
    payload: dict,
    result_path: Path,
    ledger_path: Path,
    input_digest: str,
) -> bool:
    _, entries = load_ledger(ledger_path)
    same_run = [entry for entry in entries if entry["run_id"] == payload["run_id"]]
    if not same_run:
        return False
    entry = same_run[0]
    if entry["input_digest"] != input_digest:
        raise LedgerConflict("same run")
    if result_path.exists():
        if digest_bytes(result_path.read_bytes()) != entry["output_digest"]:
            raise LedgerConflict("result digest")
        return True
    return False


def persist_result(
    payload: dict,
    result_path: Path,
    ledger_path: Path,
    input_digest: str,
    result_bytes: bytes,
) -> None:
    ledger_raw, entries = load_ledger(ledger_path)
    same_run = [entry for entry in entries if entry["run_id"] == payload["run_id"]]
    if same_run:
        entry = same_run[0]
        if (
            entry["input_digest"] != input_digest
            or entry["output_digest"] != digest_bytes(result_bytes)
        ):
            raise LedgerConflict("same run")
        if result_path.exists() and result_path.read_bytes() != result_bytes:
            raise LedgerConflict("result")
        if not result_path.exists():
            replace_files(((result_path, result_bytes),))
        return
    if entries and any(
        entry["campaign_id"] != payload["campaign_id"] for entry in entries
    ):
        ledger_raw, entries = b"", []
    previous = entries[-1]["entry_hash"] if entries else "0" * 64
    entry = make_entry(payload, input_digest, result_bytes, previous)
    replace_files(
        (
            (result_path, result_bytes),
            (ledger_path, ledger_raw + canonical_bytes(entry)),
        )
    )


def run(input_path: Path, result_path: Path, ledger_path: Path) -> str:
    raw = input_path.read_bytes()
    payload = validate(parse_json(raw))
    input_digest = digest_bytes(raw)
    if replay_is_complete(payload, result_path, ledger_path, input_digest):
        return payload["run_id"]
    result_bytes = canonical_bytes(build_result(payload))
    persist_result(
        payload,
        result_path,
        ledger_path,
        input_digest,
        result_bytes,
    )
    return payload["run_id"]


def main() -> int:
    if len(sys.argv) != 4:
        sys.stderr.write("REFUSED BAD_INPUT\n")
        return 3
    try:
        run_id = run(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    except LedgerConflict:
        sys.stderr.write("REFUSED LEDGER_CONFLICT\n")
        return 3
    except (Refused, OSError):
        sys.stderr.write("REFUSED BAD_INPUT\n")
        return 3
    sys.stdout.write(f"OK {run_id}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
