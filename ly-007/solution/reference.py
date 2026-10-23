#!/usr/bin/env python3
"""Reference association and location solver for the phase catalogue."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


EVENT_COUNT = 3
STATION_COUNT = 8
ARRIVAL_COUNT = 48
PHASES_PER_EVENT = 16
BEAM_WIDTH = 420
MAX_EVALUATED_STATES = 420
RESIDUAL_LIMIT = 0.030
MIN_RESIDUAL_GAP = 0.0015
MAX_DISTANCE_KM = 100.0
MIN_ONSET_DIFFERENCE = 0.05
PAIR_EPSILON = 1e-10
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


class AssociationAmbiguous(Refused):
    code = "AMBIGUOUS_ASSOCIATION"


class ResidualTooHigh(Refused):
    code = "RESIDUAL_LIMIT"


class LedgerConflict(Refused):
    code = "LEDGER_CONFLICT"


@dataclass(frozen=True)
class Station:
    station_id: str
    x_km: float
    y_km: float


@dataclass(frozen=True)
class Arrival:
    arrival_id: str
    station_index: int
    time_s: float


@dataclass(frozen=True)
class PhasePair:
    station_index: int
    p_id: str
    s_id: str
    distance_km: float


@dataclass(frozen=True)
class SourceFit:
    x_km: float
    y_km: float
    depth_km: float
    distance_rms: float


@dataclass(frozen=True)
class Track:
    pairs: tuple[PhasePair, ...]
    fit: SourceFit | None
    score: float


@dataclass(frozen=True)
class State:
    tracks: tuple[Track, Track, Track]
    score: float


@dataclass(frozen=True)
class EventSolution:
    event_name: str
    p_arrival_ids: tuple[str, ...]
    s_arrival_ids: tuple[str, ...]
    x_km: float
    y_km: float
    depth_km: float
    origin_time_s: float
    residual_rms: float


@dataclass(frozen=True)
class CatalogueSolution:
    events: tuple[EventSolution, ...]
    clocks: tuple[tuple[str, float], ...]
    global_residual_rms: float
    key: tuple[Any, ...]


def quantize(value: float) -> float:
    rounded = Decimal(str(value)).quantize(
        Decimal("0.000001"),
        rounding=ROUND_HALF_UP,
    )
    return float(rounded)


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


def no_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BadInput("duplicate member")
        result[key] = value
    return result


def parse_json(data: bytes) -> object:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=no_duplicate_members,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, BadInput) as exc:
        raise BadInput("invalid json") from exc


def exact_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BadInput("object keys")
    return value


def finite_number(value: object) -> float:
    if type(value) not in (int, float):
        raise BadInput("number type")
    number = float(value)
    if not math.isfinite(number):
        raise BadInput("not finite")
    return number


def nonempty_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise BadInput("identifier")
    return value


def validate_payload(value: object) -> dict[str, Any]:
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
    campaign_id = nonempty_string(payload["campaign_id"])
    run_id = nonempty_string(payload["run_id"])
    if type(payload["event_count"]) is not int or payload["event_count"] != EVENT_COUNT:
        raise BadInput("event count")
    clock_bound = finite_number(payload["clock_bound_s"])
    if not 0.0 < clock_bound <= 0.25:
        raise BadInput("clock bound")

    velocity = exact_object(payload["velocity_km_s"], {"p", "s"})
    vp = finite_number(velocity["p"])
    vs = finite_number(velocity["s"])
    if not 1.0 <= vs < vp <= 12.0:
        raise BadInput("velocity")

    raw_stations = payload["stations"]
    if not isinstance(raw_stations, list) or len(raw_stations) != STATION_COUNT:
        raise BadInput("station count")
    stations = []
    station_ids: set[str] = set()
    for raw in raw_stations:
        row = exact_object(raw, {"station_id", "x_km", "y_km"})
        station_id = nonempty_string(row["station_id"])
        if station_id in station_ids:
            raise BadInput("duplicate station")
        station_ids.add(station_id)
        x_km = finite_number(row["x_km"])
        y_km = finite_number(row["y_km"])
        if not -250.0 <= x_km <= 250.0 or not -250.0 <= y_km <= 250.0:
            raise BadInput("station range")
        stations.append(Station(station_id, x_km, y_km))
    station_index = {
        station.station_id: index for index, station in enumerate(stations)
    }

    raw_arrivals = payload["arrivals"]
    if not isinstance(raw_arrivals, list) or len(raw_arrivals) != ARRIVAL_COUNT:
        raise BadInput("arrival count")
    arrivals = []
    arrival_ids: set[str] = set()
    per_station = [0] * STATION_COUNT
    for raw in raw_arrivals:
        row = exact_object(raw, {"arrival_id", "station_id", "time_s"})
        arrival_id = nonempty_string(row["arrival_id"])
        if arrival_id in arrival_ids:
            raise BadInput("duplicate arrival")
        arrival_ids.add(arrival_id)
        station_id = nonempty_string(row["station_id"])
        if station_id not in station_index:
            raise BadInput("unknown station")
        time_s = finite_number(row["time_s"])
        if not 0.0 <= time_s <= 1000.0:
            raise BadInput("arrival range")
        index = station_index[station_id]
        per_station[index] += 1
        arrivals.append(Arrival(arrival_id, index, time_s))
    if any(count != EVENT_COUNT * 2 for count in per_station):
        raise BadInput("station arrivals")
    return {
        "arrivals": arrivals,
        "campaign_id": campaign_id,
        "clock_bound_s": clock_bound,
        "run_id": run_id,
        "stations": stations,
        "v": {"p": vp, "s": vs},
    }


def solve_linear_3(matrix: list[list[float]], vector: list[float]) -> list[float]:
    augmented = [
        [float(matrix[row][column]) for column in range(3)] + [float(vector[row])]
        for row in range(3)
    ]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise BadInput("singular sphere system")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for position in range(column, 4):
            augmented[column][position] /= divisor
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            for position in range(column, 4):
                augmented[row][position] -= factor * augmented[column][position]
    return [augmented[index][3] for index in range(3)]


def fit_track(track: Track, stations: list[Station]) -> Track:
    if not track.pairs:
        return track
    pairs = track.pairs
    if len(pairs) < 3:
        score = 0.35 * (3 - len(pairs))
        return Track(pairs, None, score)

    normal = [[0.0] * 3 for _ in range(3)]
    right = [0.0] * 3
    rows = []
    for pair in pairs:
        station = stations[pair.station_index]
        row = [2.0 * station.x_km, 2.0 * station.y_km, -1.0]
        target = (
            station.x_km * station.x_km
            + station.y_km * station.y_km
            - pair.distance_km * pair.distance_km
        )
        rows.append((row, target))
        for left in range(3):
            right[left] += row[left] * target
            for right_index in range(3):
                normal[left][right_index] += row[left] * row[right_index]
    try:
        x_km, y_km, q = solve_linear_3(normal, right)
    except BadInput:
        return Track(pairs, None, math.inf)
    depth_squared = q - x_km * x_km - y_km * y_km
    if depth_squared < -0.25:
        return Track(pairs, None, math.inf)
    depth_km = math.sqrt(max(0.0, depth_squared))
    if not (-20.0 <= x_km <= 90.0 and -20.0 <= y_km <= 60.0):
        return Track(pairs, None, math.inf)
    if not 0.1 <= depth_km <= 35.0:
        return Track(pairs, None, math.inf)

    squared = 0.0
    for pair in pairs:
        station = stations[pair.station_index]
        predicted = math.sqrt(
            (x_km - station.x_km) ** 2
            + (y_km - station.y_km) ** 2
            + depth_km * depth_km
        )
        squared += (predicted - pair.distance_km) ** 2
    distance_rms = math.sqrt(squared / len(pairs))
    fit = SourceFit(x_km, y_km, depth_km, distance_rms)
    return Track(pairs, fit, distance_rms)


def perfect_matchings(rows: tuple[Arrival, ...]) -> tuple[tuple[tuple[Arrival, Arrival], ...], ...]:
    if not rows:
        return ((),)
    first = rows[0]
    result = []
    for index in range(1, len(rows)):
        second = rows[index]
        remaining = rows[1:index] + rows[index + 1 :]
        p_arrival, s_arrival = sorted((first, second), key=lambda row: row.time_s)
        for tail in perfect_matchings(remaining):
            result.append(((p_arrival, s_arrival),) + tail)
    return tuple(result)


def station_pairings(
    station_index: int,
    arrivals: list[Arrival],
    difference_scale: float,
) -> tuple[tuple[PhasePair, PhasePair, PhasePair], ...]:
    rows = tuple(
        sorted(arrivals, key=lambda row: (row.time_s, row.arrival_id))
    )
    result = []
    for matching in perfect_matchings(rows):
        pairs = []
        valid = True
        for p_arrival, s_arrival in matching:
            difference = s_arrival.time_s - p_arrival.time_s
            if not MIN_ONSET_DIFFERENCE <= difference <= difference_scale * MAX_DISTANCE_KM:
                valid = False
                break
            pairs.append(
                PhasePair(
                    station_index=station_index,
                    p_id=p_arrival.arrival_id,
                    s_id=s_arrival.arrival_id,
                    distance_km=difference / difference_scale,
                )
            )
        if valid:
            result.append(
                tuple(sorted(pairs, key=lambda pair: pair.p_id))
            )
    return tuple(result)


def track_key(track: Track) -> tuple[Any, ...]:
    return tuple(
        sorted((pair.p_id, pair.s_id) for pair in track.pairs)
    )


def extend_states(
    states: list[State],
    pairings: tuple[tuple[PhasePair, PhasePair, PhasePair], ...],
    stations: list[Station],
    permutations: tuple[tuple[int, int, int], ...],
) -> list[State]:
    expanded = []
    for state in states:
        for pairing in pairings:
            for permutation in permutations:
                tracks = []
                valid = True
                score = 0.0
                for track_index in range(3):
                    pair = pairing[permutation[track_index]]
                    updated = fit_track(
                        Track(
                            state.tracks[track_index].pairs + (pair,),
                            None,
                            math.inf,
                        ),
                        stations,
                    )
                    if updated.fit is not None and updated.score > 0.12:
                        valid = False
                        break
                    tracks.append(updated)
                    score += updated.score
                if valid:
                    expanded.append(
                        State(
                            (tracks[0], tracks[1], tracks[2]),
                            score,
                        )
                    )
    expanded.sort(
        key=lambda state: (
            state.score,
            tuple(track_key(track) for track in state.tracks),
        )
    )
    return expanded[:BEAM_WIDTH]


def recover_station_tracks(
    payload: dict[str, Any],
) -> list[State]:
    stations = payload["stations"]
    by_station = [[] for _ in stations]
    for arrival in payload["arrivals"]:
        by_station[arrival.station_index].append(arrival)
    difference_scale = 1.0 / payload["v"]["s"] - 1.0 / payload["v"]["p"]
    pairings = [
        station_pairings(index, rows, difference_scale)
        for index, rows in enumerate(by_station)
    ]
    initial = []
    for pairing in pairings[0]:
        ordered = sorted(pairing, key=lambda pair: (pair.distance_km, pair.p_id))
        tracks = tuple(
            fit_track(Track((pair,), None, math.inf), stations)
            for pair in ordered
        )
        initial.append(
            State(
                (tracks[0], tracks[1], tracks[2]),
                sum(track.score for track in tracks),
            )
        )
    states = initial[:BEAM_WIDTH]
    permutations = tuple(itertools.permutations(range(3)))
    for station_index in range(1, len(stations)):
        states = extend_states(
            states,
            pairings[station_index],
            stations,
            permutations,
        )
    return states


def travel_time(distance: float, velocity: float) -> float:
    return distance / velocity


def solve_joint_times(
    payload: dict[str, Any],
    tracks: tuple[Track, Track, Track],
    stations: list[Station],
    velocities: dict[str, float],
    clock_bound: float,
) -> tuple[tuple[EventSolution, ...], tuple[tuple[str, float], ...], float] | None:
    if any(track.fit is None for track in tracks):
        return None
    station_count = len(stations)
    by_event_station: list[list[tuple[PhasePair, float, float]]] = []
    for track in tracks:
        assert track.fit is not None
        row = []
        for pair in sorted(track.pairs, key=lambda value: value.station_index):
            station = stations[pair.station_index]
            distance = math.sqrt(
                (track.fit.x_km - station.x_km) ** 2
                + (track.fit.y_km - station.y_km) ** 2
                + track.fit.depth_km * track.fit.depth_km
            )
            row.append(
                (
                    pair,
                    travel_time(distance, velocities["p"]),
                    travel_time(distance, velocities["s"]),
                )
            )
        by_event_station.append(row)
    if any(len(row) != station_count for row in by_event_station):
        return None

    # Each event contributes one station mean; equal weighting makes the
    # origin/clock normal equations separable.
    ybar = [[0.0] * station_count for _ in range(EVENT_COUNT)]
    for event_index, row in enumerate(by_event_station):
        for pair, travel_p, travel_s in row:
            station_index = pair.station_index
            raw_times = observation_times(payload, pair)
            ybar[event_index][station_index] = (
                (raw_times[0] - travel_p) + (raw_times[1] - travel_s)
            ) / 2.0

    origins = [
        sum(ybar[event_index]) / station_count
        for event_index in range(EVENT_COUNT)
    ]
    clocks = [
        sum(ybar[event_index][station_index] - origins[event_index]
            for event_index in range(EVENT_COUNT)) / EVENT_COUNT
        for station_index in range(station_count)
    ]
    mean_clock = sum(clocks) / station_count
    clocks = [clock - mean_clock for clock in clocks]
    origins = [
        sum(
            ybar[event_index][station_index] - clocks[station_index]
            for station_index in range(station_count)
        )
        / station_count
        for event_index in range(EVENT_COUNT)
    ]
    if any(abs(clock) > clock_bound + 1e-8 for clock in clocks):
        return None

    squared = 0.0
    count = 0
    events = []
    for event_index, row in enumerate(by_event_station):
        event_squared = 0.0
        event_count = 0
        p_ids = []
        s_ids = []
        for pair, travel_p, travel_s in row:
            p_ids.append(pair.p_id)
            s_ids.append(pair.s_id)
            raw_p, raw_s = observation_times(payload, pair)
            residual_p = raw_p - (
                origins[event_index] + clocks[pair.station_index] + travel_p
            )
            residual_s = raw_s - (
                origins[event_index] + clocks[pair.station_index] + travel_s
            )
            event_squared += residual_p * residual_p + residual_s * residual_s
            event_count += 2
        squared += event_squared
        count += event_count
        track = tracks[event_index]
        assert track.fit is not None
        events.append(
            EventSolution(
                event_name="",
                p_arrival_ids=tuple(sorted(p_ids)),
                s_arrival_ids=tuple(sorted(s_ids)),
                x_km=track.fit.x_km,
                y_km=track.fit.y_km,
                depth_km=track.fit.depth_km,
                origin_time_s=origins[event_index],
                residual_rms=math.sqrt(event_squared / event_count),
            )
        )
    events.sort(
        key=lambda event: (
            event.origin_time_s,
            event.p_arrival_ids,
            event.s_arrival_ids,
        )
    )
    normalized_events = tuple(
        EventSolution(
            event_name=f"EVT-{index:02d}",
            p_arrival_ids=event.p_arrival_ids,
            s_arrival_ids=event.s_arrival_ids,
            x_km=event.x_km,
            y_km=event.y_km,
            depth_km=event.depth_km,
            origin_time_s=event.origin_time_s,
            residual_rms=event.residual_rms,
        )
        for index, event in enumerate(events, start=1)
    )
    clock_rows = tuple(
        sorted(
            (
                (station.station_id, clocks[index])
                for index, station in enumerate(stations)
            ),
            key=lambda row: row[0],
        )
    )
    global_rms = math.sqrt(squared / count)
    key = tuple(
        sorted(
            tuple(event.p_arrival_ids) + tuple(event.s_arrival_ids)
            for event in normalized_events
        )
    )
    return normalized_events, clock_rows, global_rms


def observation_times(payload: dict[str, Any], pair: PhasePair) -> tuple[float, float]:
    rows = payload["arrival_by_id"]
    return rows[pair.p_id].time_s, rows[pair.s_id].time_s


def solve_catalogue(payload: dict[str, Any]) -> CatalogueSolution:
    states = recover_station_tracks(payload)
    solutions: dict[tuple[Any, ...], CatalogueSolution] = {}
    for state in states[:MAX_EVALUATED_STATES]:
        solved = solve_joint_times(
            payload,
            state.tracks,
            payload["stations"],
            payload["v"],
            payload["clock_bound_s"],
        )
        if solved is None:
            continue
        events, clocks, residual = solved
        key = tuple(
            sorted(
                tuple(event.p_arrival_ids) + tuple(event.s_arrival_ids)
                for event in events
            )
        )
        candidate = CatalogueSolution(events, clocks, residual, key)
        previous = solutions.get(key)
        if previous is None or residual < previous.global_residual_rms:
            solutions[key] = candidate
    if not solutions:
        raise ResidualTooHigh("no time-consistent association")
    ordered = sorted(
        solutions.values(),
        key=lambda solution: (
            solution.global_residual_rms,
            solution.key,
        ),
    )
    best = ordered[0]
    if best.global_residual_rms > RESIDUAL_LIMIT:
        raise ResidualTooHigh("global residual")
    if len(ordered) > 1:
        gap = ordered[1].global_residual_rms - best.global_residual_rms
        if gap < MIN_RESIDUAL_GAP:
            raise AssociationAmbiguous("near-equal support")
    return best


def build_result(payload: dict[str, Any]) -> dict[str, Any]:
    payload["arrival_by_id"] = {
        arrival.arrival_id: arrival for arrival in payload["arrivals"]
    }
    solution = solve_catalogue(payload)
    return {
        "campaign_id": payload["campaign_id"],
        "events": [
            {
                "event_id": event.event_name,
                "hypocenter": {
                    "depth_km": quantize(event.depth_km),
                    "x_km": quantize(event.x_km),
                    "y_km": quantize(event.y_km),
                },
                "origin_time_s": quantize(event.origin_time_s),
                "p_arrival_ids": list(event.p_arrival_ids),
                "residual_rms": quantize(event.residual_rms),
                "s_arrival_ids": list(event.s_arrival_ids),
            }
            for event in solution.events
        ],
        "global_residual_rms": quantize(solution.global_residual_rms),
        "run_id": payload["run_id"],
        "schema_version": 6,
        "stations": [
            {
                "clock_offset_s": quantize(offset),
                "station_id": station_id,
            }
            for station_id, offset in solution.clocks
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


def load_ledger(path: Path) -> tuple[bytes, list[dict[str, str]]]:
    if not path.exists():
        return b"", []
    raw = path.read_bytes()
    if not raw:
        return b"", []
    try:
        if not raw.endswith(b"\n"):
            raise ValueError("newline")
        previous = "0" * 64
        seen: set[str] = set()
        entries = []
        for line in raw.splitlines():
            if not line:
                raise ValueError("empty")
            entry = parse_json(line)
            entry = exact_object(entry, LEDGER_KEYS)
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
    payload: dict[str, Any],
    input_digest: str,
    result_bytes: bytes,
    previous_hash: str,
) -> dict[str, str]:
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
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def replace_files(items: tuple[tuple[Path, bytes], ...]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for path, data in items:
            staged.append((stage_file(path, data), path))
        for temporary, path in staged:
            temporary.replace(path)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def persist_result(
    payload: dict[str, Any],
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
            raise LedgerConflict("same run digest")
        if result_path.exists() and result_path.read_bytes() != result_bytes:
            raise LedgerConflict("existing result")
        if not result_path.exists():
            replace_files(((result_path, result_bytes),))
        return
    if entries and any(
        entry["campaign_id"] != payload["campaign_id"] for entry in entries
    ):
        ledger_raw, entries = b"", []
    previous_hash = entries[-1]["entry_hash"] if entries else "0" * 64
    entry = make_entry(payload, input_digest, result_bytes, previous_hash)
    replace_files(
        (
            (result_path, result_bytes),
            (ledger_path, ledger_raw + canonical_bytes(entry)),
        )
    )


def replay_is_complete(
    payload: dict[str, Any],
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
        raise LedgerConflict("same run input")
    if result_path.exists():
        if digest_bytes(result_path.read_bytes()) != entry["output_digest"]:
            raise LedgerConflict("existing result digest")
        return True
    return False


def run(input_path: Path, result_path: Path, ledger_path: Path) -> str:
    raw = input_path.read_bytes()
    payload = validate_payload(parse_json(raw))
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


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3:
        sys.stderr.write("REFUSED BAD_INPUT\n")
        return 3
    try:
        run_id = run(Path(arguments[0]), Path(arguments[1]), Path(arguments[2]))
    except Refused as exc:
        sys.stderr.write(f"REFUSED {exc.code}\n")
        return 3
    except OSError:
        sys.stderr.write("REFUSED BAD_INPUT\n")
        return 3
    sys.stdout.write(f"OK {run_id}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
