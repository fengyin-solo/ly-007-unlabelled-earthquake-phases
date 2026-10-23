from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest


TASK_APP = os.environ.get("TASK_APP", "/app/phase_association.py")
DATA_DIR = Path(os.environ.get("TASK_DATA_DIR", "/app/data"))
TRUTH_PATH = Path(os.environ.get("TASK_TRUTH", "/tests/truth.json"))
PUBLIC_LIMIT = 12.0
WATCHDOG = 16
STATIONS = [
    {"station_id": "ST-ALPHA", "x_km": 0.0, "y_km": 0.0},
    {"station_id": "ST-BRAVO", "x_km": 18.0, "y_km": 2.0},
    {"station_id": "ST-CHARLIE", "x_km": 35.0, "y_km": 0.0},
    {"station_id": "ST-DELTA", "x_km": 52.0, "y_km": 1.0},
    {"station_id": "ST-ECHO", "x_km": 8.0, "y_km": 24.0},
    {"station_id": "ST-FOXTROT", "x_km": 24.0, "y_km": 28.0},
    {"station_id": "ST-GOLF", "x_km": 42.0, "y_km": 26.0},
    {"station_id": "ST-HOTEL", "x_km": 58.0, "y_km": 22.0},
]
EVENTS = [
    {"x_km": 13.0, "y_km": 10.0, "depth_km": 6.0, "origin_time_s": 2.0},
    {"x_km": 36.0, "y_km": 13.0, "depth_km": 11.0, "origin_time_s": 8.0},
    {"x_km": 27.0, "y_km": 20.0, "depth_km": 3.5, "origin_time_s": 14.0},
]
CLOCKS = [-0.09, 0.04, -0.03, 0.11, -0.07, 0.02, 0.08, -0.06]


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def default_payload() -> dict:
    return json.loads((DATA_DIR / "catalogue.json").read_text(encoding="utf-8"))


def hidden_truth() -> dict:
    return json.loads(TRUTH_PATH.read_text(encoding="utf-8"))


def write_payload(path: Path, payload: object) -> None:
    path.write_bytes(canonical_bytes(payload))


def station_indexes(payload: dict) -> dict[str, int]:
    return {
        station["station_id"]: index
        for index, station in enumerate(payload["stations"])
    }


def build_case(
    events: list[dict] | None = None,
    clocks: list[float] | None = None,
    *,
    noise_scale: float = 0.0035,
    joint_residual_scale: float = 0.0,
    shuffle_seed: int = 20261023,
    campaign_id: str = "AFL-DYNAMIC",
    run_id: str = "RUN-AFL-DYNAMIC-01",
) -> tuple[dict, dict]:
    event_rows = EVENTS if events is None else events
    clock_rows = CLOCKS if clocks is None else clocks
    arrivals = []
    truth_events = []
    for event_index, event in enumerate(event_rows):
        p_ids = []
        s_ids = []
        for station_index, station in enumerate(STATIONS):
            distance = math.sqrt(
                (event["x_km"] - station["x_km"]) ** 2
                + (event["y_km"] - station["y_km"]) ** 2
                + event["depth_km"] ** 2
            )
            for phase_index, phase in enumerate(("P", "S")):
                arrival_id = (
                    f"AR-{phase}-{event_index + 1}-{station_index + 1:02d}"
                )
                if phase == "P":
                    p_ids.append(arrival_id)
                else:
                    s_ids.append(arrival_id)
                velocity = 6.0 if phase == "P" else 3.5
                noise = noise_scale * math.sin(
                    (event_index + 1)
                    * (station_index + 2)
                    * (phase_index + 3)
                    * 0.47
                )
                joint_noise = joint_residual_scale * math.sin(
                    (event_index + 2) * (station_index + 3) * 0.83
                )
                time_s = (
                    event["origin_time_s"]
                    + clock_rows[station_index]
                    + distance / velocity
                    + noise
                    + joint_noise
                )
                arrivals.append(
                    {
                        "arrival_id": arrival_id,
                        "station_id": station["station_id"],
                        "time_s": round(time_s, 6),
                    }
                )
        truth_events.append(
            {
                "event_name": f"DYNAMIC-{event_index + 1}",
                "hypocenter": {
                    "depth_km": event["depth_km"],
                    "x_km": event["x_km"],
                    "y_km": event["y_km"],
                },
                "origin_time_s": event["origin_time_s"],
                "p_arrival_ids": sorted(p_ids),
                "s_arrival_ids": sorted(s_ids),
            }
        )
    random.Random(shuffle_seed).shuffle(arrivals)
    payload = {
        "arrivals": arrivals,
        "campaign_id": campaign_id,
        "clock_bound_s": 0.12,
        "event_count": 3,
        "run_id": run_id,
        "stations": copy.deepcopy(STATIONS),
        "velocity_km_s": {"p": 6.0, "s": 3.5},
    }
    return payload, {"events": truth_events}


def invoke(
    input_path: Path,
    result_path: Path,
    ledger_path: Path,
    timeout: int = WATCHDOG,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            TASK_APP,
            str(input_path),
            str(result_path),
            str(ledger_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed, time.monotonic() - started


def run_payload(
    root: Path,
    payload: dict,
    tag: str,
) -> tuple[dict | None, Path, Path, subprocess.CompletedProcess[str], float]:
    work = root / tag
    work.mkdir(parents=True)
    input_path = work / "catalogue.json"
    result_path = work / "result.json"
    ledger_path = work / "ledger.jsonl"
    write_payload(input_path, payload)
    completed, elapsed = invoke(input_path, result_path, ledger_path)
    result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.exists()
        else None
    )
    return result, result_path, ledger_path, completed, elapsed


def seed_world(
    root: Path,
    baseline: tuple[dict | None, Path, Path, subprocess.CompletedProcess[str], float],
    tag: str,
    payload: dict | None = None,
) -> tuple[Path, Path, Path]:
    work = root / tag
    work.mkdir(parents=True)
    input_path = work / "catalogue.json"
    result_path = work / "result.json"
    ledger_path = work / "ledger.jsonl"
    write_payload(input_path, default_payload() if payload is None else payload)
    result_path.write_bytes(baseline[1].read_bytes())
    ledger_path.write_bytes(baseline[2].read_bytes())
    return input_path, result_path, ledger_path


def event_memberships(result: dict) -> dict[frozenset[str], dict]:
    return {
        frozenset(event["p_arrival_ids"] + event["s_arrival_ids"]): event
        for event in result["events"]
    }


def truth_memberships(truth: dict) -> dict[frozenset[str], dict]:
    return {
        frozenset(event["p_arrival_ids"] + event["s_arrival_ids"]): event
        for event in truth["events"]
    }


def phase_pairs(result: dict, payload: dict) -> set[frozenset[str]]:
    station_by_arrival = {
        row["arrival_id"]: row["station_id"] for row in payload["arrivals"]
    }
    pairs = set()
    for event in result["events"]:
        by_station: dict[str, dict[str, str]] = {}
        for phase, ids in (
            ("P", event["p_arrival_ids"]),
            ("S", event["s_arrival_ids"]),
        ):
            for arrival_id in ids:
                station_id = station_by_arrival[arrival_id]
                by_station.setdefault(station_id, {})[phase] = arrival_id
        for phases in by_station.values():
            pairs.add(frozenset((phases["P"], phases["S"])))
    return pairs


def truth_phase_pairs(truth: dict, payload: dict) -> set[frozenset[str]]:
    station_by_arrival = {
        row["arrival_id"]: row["station_id"] for row in payload["arrivals"]
    }
    pairs = set()
    for event in truth["events"]:
        by_station: dict[str, dict[str, str]] = {}
        for phase, ids in (
            ("P", event["p_arrival_ids"]),
            ("S", event["s_arrival_ids"]),
        ):
            for arrival_id in ids:
                station_id = station_by_arrival[arrival_id]
                by_station.setdefault(station_id, {})[phase] = arrival_id
        for phases in by_station.values():
            pairs.add(frozenset((phases["P"], phases["S"])))
    return pairs


def invalid_runs(
    root: Path,
    payloads: list[dict],
    tag: str,
) -> list[tuple[Path, Path, subprocess.CompletedProcess[str]]]:
    outcomes = []
    for index, payload in enumerate(payloads):
        work = root / f"{tag}-{index}"
        work.mkdir()
        input_path = work / "catalogue.json"
        result_path = work / "result.json"
        ledger_path = work / "ledger.jsonl"
        write_payload(input_path, payload)
        completed, _ = invoke(input_path, result_path, ledger_path)
        outcomes.append((result_path, ledger_path, completed))
    return outcomes


@pytest.fixture(scope="session")
def default_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict | None, Path, Path, subprocess.CompletedProcess[str], float]:
    return run_payload(tmp_path_factory.mktemp("default"), default_payload(), "run")


def test_global_association_matches_hidden_truth(default_run):
    """题面：“三个事件在每站各出现一次 P 和一次 S；48 条到时必须全部使用一次”。"""
    result = default_run[0]
    actual = set(event_memberships(result))
    expected = set(truth_memberships(hidden_truth()))
    assert actual == expected


def test_phase_pairing_matches_hidden_truth(default_run):
    """题面：“同一事件同一台站的 P、S 共用一个钟差。”"""
    payload = default_payload()
    actual = phase_pairs(default_run[0], payload)
    expected = truth_phase_pairs(hidden_truth(), payload)
    assert actual == expected


def test_all_arrivals_are_used_once(default_run):
    """题面：“三个事件在每站各出现一次 P 和一次 S；48 条到时必须全部使用一次”。"""
    result = default_run[0]
    actual = [
        arrival_id
        for event in result["events"]
        for arrival_id in event["p_arrival_ids"] + event["s_arrival_ids"]
    ]
    expected = sorted(row["arrival_id"] for row in default_payload()["arrivals"])
    assert sorted(actual) == expected
    assert len(actual) == len(set(actual)) == 48


def test_arrival_input_order_does_not_change_result(
    tmp_path: Path,
    default_run,
):
    """题面：“现场不提供事件号、相位名、源参数、钟差或到时归属，输入顺序也不代表事件或相位。”"""
    payload = default_payload()
    payload["arrivals"].reverse()
    changed, _, _, completed, _ = run_payload(tmp_path, payload, "reversed")
    assert completed.returncode == 0
    assert changed == default_run[0]


def test_event_order_follows_origin_time(default_run):
    """题面：“较早发生的事件先编号：发震时刻由早到晚依次写为 `EVT-01`、`EVT-02`、`EVT-03`。”"""
    result = default_run[0]
    actual = [
        (event["event_id"], event["origin_time_s"])
        for event in result["events"]
    ]
    expected = [
        ("EVT-01", 2.0),
        ("EVT-02", 8.0),
        ("EVT-03", 14.0),
    ]
    assert [row[0] for row in actual] == [row[0] for row in expected]
    assert all(
        set(event)
        == {
            "event_id",
            "hypocenter",
            "origin_time_s",
            "p_arrival_ids",
            "residual_rms",
            "s_arrival_ids",
        }
        for event in result["events"]
    )
    assert all(
        left[1] < right[1] for left, right in zip(actual, actual[1:])
    )


def test_station_pairing_cancels_clock_offsets(tmp_path: Path, default_run):
    """题面：“每站先由 P/S 同源对得到可检验的 S-P 残差，跨站关联不得依赖输入先后或未给出的标签。”"""
    payload = default_payload()
    shifted_clock = [0.05, -0.02, 0.01, -0.04, 0.03, -0.01, -0.06, 0.04]
    indexes = station_indexes(payload)
    for arrival in payload["arrivals"]:
        arrival["time_s"] = round(
            arrival["time_s"] + shifted_clock[indexes[arrival["station_id"]]],
            6,
        )
    changed, _, _, completed, _ = run_payload(tmp_path, payload, "clock-shift")
    assert completed.returncode == 0
    assert set(event_memberships(changed)) == set(event_memberships(default_run[0]))


def test_hypocenter_xy_matches_hidden_truth(default_run):
    """题面：“从震源到台站的直线距离除以对应速度就是走时。”"""
    actual = {
        frozenset(event["p_arrival_ids"] + event["s_arrival_ids"]): event
        for event in default_run[0]["events"]
    }
    expected = truth_memberships(hidden_truth())
    assert all(
        set(event["hypocenter"]) == {"depth_km", "x_km", "y_km"}
        for event in actual.values()
    )
    assert all(
        math.hypot(
            actual[key]["hypocenter"]["x_km"]
            - expected[key]["hypocenter"]["x_km"],
            actual[key]["hypocenter"]["y_km"]
            - expected[key]["hypocenter"]["y_km"],
        )
        <= 0.25
        for key in expected
    )


def test_hypocenter_depth_matches_hidden_truth(default_run):
    """题面：“从震源到台站的直线距离除以对应速度就是走时。”"""
    actual = {
        frozenset(event["p_arrival_ids"] + event["s_arrival_ids"]): event
        for event in default_run[0]["events"]
    }
    expected = truth_memberships(hidden_truth())
    assert all(
        abs(
            actual[key]["hypocenter"]["depth_km"]
            - expected[key]["hypocenter"]["depth_km"]
        )
        <= 0.40
        for key in expected
    )


def test_origin_times_match_hidden_truth(default_run):
    """题面：“一条到时满足“发震时刻 + 台站钟差 + 该相位走时””。"""
    actual = {
        frozenset(event["p_arrival_ids"] + event["s_arrival_ids"]): event
        for event in default_run[0]["events"]
    }
    expected = truth_memberships(hidden_truth())
    assert all(
        abs(
            actual[key]["origin_time_s"]
            - expected[key]["origin_time_s"]
        )
        <= 0.035
        for key in expected
    )


def test_clock_offsets_match_hidden_truth(default_run):
    """题面：“钟差取八站均值为零的规范解。”"""
    actual = {
        row["station_id"]: row["clock_offset_s"]
        for row in default_run[0]["stations"]
    }
    expected = {
        row["station_id"]: row["clock_offset_s"]
        for row in hidden_truth()["clock_offsets"]
    }
    assert all(abs(actual[key] - expected[key]) <= 0.03 for key in expected)


def test_clock_offsets_have_zero_mean(default_run):
    """题面：“钟差取八站均值为零的规范解。”"""
    values = [row["clock_offset_s"] for row in default_run[0]["stations"]]
    assert abs(sum(values) / len(values)) <= 2e-6
    assert all(abs(value) <= default_payload()["clock_bound_s"] for value in values)
    station_ids = [row["station_id"] for row in default_run[0]["stations"]]
    assert station_ids == sorted(station_ids)
    assert all(
        set(row) == {"clock_offset_s", "station_id"}
        for row in default_run[0]["stations"]
    )


def test_global_residual_matches_direct_recomputation(default_run):
    """题面：“共同最小化 48 条到时的全局均方根残差。”"""
    payload = default_payload()
    result = default_run[0]
    station_by_id = {
        row["station_id"]: row for row in payload["stations"]
    }
    arrival_by_id = {
        row["arrival_id"]: row for row in payload["arrivals"]
    }
    clock_by_id = {
        row["station_id"]: row["clock_offset_s"] for row in result["stations"]
    }
    squared = 0.0
    count = 0
    for event in result["events"]:
        source = event["hypocenter"]
        for phase, ids, velocity in (
            ("P", event["p_arrival_ids"], payload["velocity_km_s"]["p"]),
            ("S", event["s_arrival_ids"], payload["velocity_km_s"]["s"]),
        ):
            for arrival_id in ids:
                arrival = arrival_by_id[arrival_id]
                station = station_by_id[arrival["station_id"]]
                distance = math.sqrt(
                    (source["x_km"] - station["x_km"]) ** 2
                    + (source["y_km"] - station["y_km"]) ** 2
                    + source["depth_km"] ** 2
                )
                predicted = (
                    event["origin_time_s"]
                    + clock_by_id[station["station_id"]]
                    + distance / velocity
                )
                squared += (arrival["time_s"] - predicted) ** 2
                count += 1
    recomputed = math.sqrt(squared / count)
    assert count == 48
    assert abs(recomputed - result["global_residual_rms"]) <= 3e-6


def test_residual_below_public_limit_is_accepted(tmp_path: Path):
    """题面：“全局残差不得超过 `0.030000` 秒”。"""
    payload, truth = build_case(
        noise_scale=0.0035,
        joint_residual_scale=0.018,
    )
    result, _, _, completed, _ = run_payload(tmp_path, payload, "near-limit")
    assert completed.returncode == 0
    assert result is not None
    assert 0.010 <= result["global_residual_rms"] <= 0.030
    assert len(result["events"]) == len(truth["events"]) == 3


def test_residual_over_public_limit_is_refused(tmp_path: Path):
    """题面：“全局残差不得超过 `0.030000` 秒”。"""
    payload, _ = build_case(
        noise_scale=0.0035,
        joint_residual_scale=0.070,
    )
    _, result_path, ledger_path, completed, _ = run_payload(
        tmp_path,
        payload,
        "over-limit",
    )
    assert completed.returncode == 3
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED RESIDUAL_LIMIT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_ambiguous_association_is_refused(tmp_path: Path):
    """题面：“残差最低的两个不同关联相差不足 `0.001500` 秒时按 `AMBIGUOUS_ASSOCIATION` 拒收。”"""
    events = [
        {"x_km": 13.0, "y_km": 10.0, "depth_km": 6.0, "origin_time_s": 2.0},
        {"x_km": 13.0, "y_km": 10.0, "depth_km": 6.0, "origin_time_s": 2.0},
        {"x_km": 27.0, "y_km": 20.0, "depth_km": 3.5, "origin_time_s": 14.0},
    ]
    payload, _ = build_case(events=events, noise_scale=0.0)
    _, result_path, ledger_path, completed, _ = run_payload(
        tmp_path,
        payload,
        "ambiguous",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED AMBIGUOUS_ASSOCIATION\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_candidate_association_space_is_large(default_run):
    """题面：“单是台内配对就有 `15^8 = 2562890625` 种；完整六组相位关联下界为 `7.34e28` 量级。”"""
    pairings = 15**8
    full_lower_bound = (
        math.factorial(48)
        // (math.factorial(8) ** 6 * math.factorial(3) * 3**8)
    )
    assert pairings == 2_562_890_625
    assert 7.33e28 <= full_lower_bound <= 7.35e28
    assert default_run[0] is not None


def test_public_upper_case_completes_within_limit(default_run):
    """题面：“任何拒收都不得改动输入、旧结果或旧台账；整次运行须在 12 秒内结束。”"""
    result, result_path, ledger_path, completed, elapsed = default_run
    replay, _ = invoke(
        result_path.parent / "catalogue.json",
        result_path,
        ledger_path,
        timeout=16,
    )
    assert completed.returncode == 0
    assert replay.returncode == 0
    assert result is not None
    assert len(result["events"]) == 3
    assert elapsed < 12.0
    assert math.isfinite(elapsed)


def test_close_events_remain_distinct(tmp_path: Path):
    """题面：“三个事件在每站各出现一次 P 和一次 S；48 条到时必须全部使用一次”。"""
    events = [
        {"x_km": 18.0, "y_km": 11.0, "depth_km": 6.0, "origin_time_s": 2.0},
        {"x_km": 21.0, "y_km": 12.0, "depth_km": 8.0, "origin_time_s": 5.0},
        {"x_km": 38.0, "y_km": 20.0, "depth_km": 4.0, "origin_time_s": 12.0},
    ]
    payload, truth = build_case(events=events, noise_scale=0.0035)
    result, _, _, completed, _ = run_payload(tmp_path, payload, "close-events")
    assert completed.returncode == 0
    assert set(event_memberships(result)) == set(truth_memberships(truth))
    assert len({event["event_id"] for event in result["events"]}) == 3


def test_success_channel_is_exact(default_run):
    """题面：“成功收口时，标准输出仅回显运行号（`OK <run_id>\\n`），标准错误保持空白。”"""
    result, _, _, completed, _ = default_run
    assert result is not None
    assert completed.returncode == 0
    assert completed.stdout == f"OK {default_payload()['run_id']}\n"
    assert completed.stderr == ""


def test_refusal_channel_is_exact(tmp_path: Path):
    """题面：“拒收时 stdout 为空，stderr 只写 `REFUSED BAD_INPUT\\n`”。"""
    work = tmp_path / "case-01"
    work.mkdir()
    input_path = work / "catalogue.json"
    input_path.write_text('{"campaign_id":"x"}\n', encoding="utf-8")
    completed, _ = invoke(
        input_path,
        work / "result.json",
        work / "ledger.jsonl",
    )
    assert completed.returncode == 3
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not (work / "result.json").exists()


def test_result_schema_is_exact(default_run):
    """题面：“结果顶层字段集合恰好是 `{campaign_id,events,global_residual_rms,run_id,schema_version,stations}`，`schema_version` 是整数 6。”"""
    result = default_run[0]
    assert set(result) == {
        "campaign_id",
        "events",
        "global_residual_rms",
        "run_id",
        "schema_version",
        "stations",
    }
    assert result["schema_version"] == 6
    assert all(
        set(event)
        == {
            "event_id",
            "hypocenter",
            "origin_time_s",
            "p_arrival_ids",
            "residual_rms",
            "s_arrival_ids",
        }
        for event in result["events"]
    )
    assert all(
        set(event["hypocenter"]) == {"depth_km", "x_km", "y_km"}
        for event in result["events"]
    )
    assert all(
        set(station) == {"clock_offset_s", "station_id"}
        for station in result["stations"]
    )


def test_result_serialization_is_canonical(default_run):
    """题面：“数值按十进制 half-up 保留 6 位，结果用排序键紧凑 JSON 并以一个换行结束。”"""
    result, result_path, _, _, _ = default_run
    raw = result_path.read_bytes()
    assert raw == canonical_bytes(result)
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert result["events"][0]["hypocenter"]["x_km"] == round(
        result["events"][0]["hypocenter"]["x_km"],
        6,
    )


def test_duplicate_json_members_are_refused(tmp_path: Path):
    """题面：“现场文件是 UTF-8 JSON 对象”。"""
    work = tmp_path / "duplicate-json"
    work.mkdir()
    input_path = work / "catalogue.json"
    raw = canonical_bytes(default_payload())
    input_path.write_bytes(b'{"campaign_id":"dup",' + raw[1:])
    completed, _ = invoke(
        input_path,
        work / "result.json",
        work / "ledger.jsonl",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not (work / "result.json").exists()
    assert not (work / "ledger.jsonl").exists()


def test_root_field_set_is_enforced(tmp_path: Path):
    """题面：“顶层字段集合恰好是 `{arrivals,campaign_id,clock_bound_s,event_count,run_id,stations,velocity_km_s}`”。"""
    missing = default_payload()
    missing.pop("clock_bound_s")
    extra = default_payload()
    extra["extra"] = 1
    outcomes = invalid_runs(tmp_path, [missing, extra], "root-field")
    assert all(completed.returncode == 3 for _, _, completed in outcomes)
    assert all(
        completed.stderr == "REFUSED BAD_INPUT\n"
        for _, _, completed in outcomes
    )
    assert all(not result_path.exists() for result_path, _, _ in outcomes)
    assert all(not ledger_path.exists() for _, ledger_path, _ in outcomes)


def test_station_and_arrival_counts_are_enforced(tmp_path: Path):
    """题面：“`stations` 恰有 8 项，`arrivals` 恰有 48 项。”"""
    short_arrivals = default_payload()
    short_arrivals["arrivals"].pop()
    short_station = default_payload()
    short_station["stations"].pop()
    _, first_result, first_ledger, first_run, _ = run_payload(
        tmp_path,
        short_arrivals,
        "short-arrivals",
    )
    _, second_result, second_ledger, second_run, _ = run_payload(
        tmp_path,
        short_station,
        "short-stations",
    )
    assert first_run.returncode == second_run.returncode == 3
    assert first_run.stderr == second_run.stderr == "REFUSED BAD_INPUT\n"
    assert not first_result.exists()
    assert not second_result.exists()
    assert not first_ledger.exists()
    assert not second_ledger.exists()


def test_station_ids_are_unique_and_known(tmp_path: Path):
    """题面：“站号和到时号都必须非空且各自不重复。”"""
    duplicate_station = default_payload()
    duplicate_station["stations"][1]["station_id"] = "ST-ALPHA"
    unknown_station = default_payload()
    unknown_station["arrivals"][0]["station_id"] = "ST-UNKNOWN"
    duplicate_arrival = default_payload()
    duplicate_arrival["arrivals"][1]["arrival_id"] = duplicate_arrival["arrivals"][0][
        "arrival_id"
    ]
    extra_station_field = default_payload()
    extra_station_field["stations"][0]["elevation_km"] = 0.1
    extra_arrival_field = default_payload()
    extra_arrival_field["arrivals"][0]["phase"] = "P"
    outcomes = []
    for index, payload in enumerate(
        (
            duplicate_station,
            unknown_station,
            duplicate_arrival,
            extra_station_field,
            extra_arrival_field,
        )
    ):
        work = tmp_path / f"identifier-{index}"
        work.mkdir()
        input_path = work / "catalogue.json"
        write_payload(input_path, payload)
        completed, _ = invoke(
            input_path,
            work / "result.json",
            work / "ledger.jsonl",
        )
        outcomes.append(completed)
    assert all(run.returncode == 3 for run in outcomes)
    assert all(run.stderr == "REFUSED BAD_INPUT\n" for run in outcomes)
    assert all(
        not (tmp_path / f"identifier-{index}" / "result.json").exists()
        for index in range(5)
    )


def test_arrival_types_and_ranges_are_enforced(tmp_path: Path):
    """题面：“每条到时都应落在 0 到 1000 秒之间，且不能是无穷或 NaN。”"""
    text_time = default_payload()
    text_time["arrivals"][0]["time_s"] = "1.0"
    negative = default_payload()
    negative["arrivals"][0]["time_s"] = -0.1
    boolean = default_payload()
    boolean["arrivals"][0]["time_s"] = True
    outcomes = invalid_runs(
        tmp_path,
        [text_time, negative, boolean],
        "arrival-value",
    )
    assert all(completed.returncode == 3 for _, _, completed in outcomes)
    assert all(
        completed.stderr == "REFUSED BAD_INPUT\n"
        for _, _, completed in outcomes
    )
    assert all(not result_path.exists() for result_path, _, _ in outcomes)
    assert all(not ledger_path.exists() for _, ledger_path, _ in outcomes)


def test_velocity_and_clock_bounds_are_enforced(tmp_path: Path):
    """题面：“`clock_bound_s` 是大于 0 且不超过 0.25 的有限数”；同时 p、s 必须形成可分辨的走时。"""
    bad_velocity = default_payload()
    bad_velocity["velocity_km_s"]["p"] = 3.5
    bad_clock = default_payload()
    bad_clock["clock_bound_s"] = 0.26
    outcomes = invalid_runs(
        tmp_path,
        [bad_velocity, bad_clock],
        "physical-bound",
    )
    assert all(completed.returncode == 3 for _, _, completed in outcomes)
    assert all(
        completed.stderr == "REFUSED BAD_INPUT\n"
        for _, _, completed in outcomes
    )
    assert all(not result_path.exists() for result_path, _, _ in outcomes)
    assert all(not ledger_path.exists() for _, ledger_path, _ in outcomes)
    endpoint = default_payload()
    endpoint["clock_bound_s"] = 0.25
    result, result_path, ledger_path, completed, _ = run_payload(
        tmp_path,
        endpoint,
        "clock-bound-endpoint",
    )
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert result is not None
    assert result_path.is_file()
    assert ledger_path.is_file()


def test_event_count_is_exactly_three(tmp_path: Path):
    """题面：“`event_count` 是整数 3”。"""
    payload = default_payload()
    payload["event_count"] = True
    _, result_path, ledger_path, completed, _ = run_payload(
        tmp_path,
        payload,
        "event-count",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_ledger_hash_chain_is_canonical(default_run):
    """题面：“去掉 `entry_hash` 后按排序键紧凑编码的 SHA-256 即本行哈希。”"""
    ledger_path = default_run[2]
    entry = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    body = {key: value for key, value in entry.items() if key != "entry_hash"}
    assert set(entry) == {
        "campaign_id",
        "entry_hash",
        "input_digest",
        "output_digest",
        "previous_hash",
        "run_id",
    }
    assert entry["previous_hash"] == "0" * 64
    assert entry["entry_hash"] == hashlib.sha256(
        canonical_bytes(body)[:-1]
    ).hexdigest()


def test_new_run_appends_without_rewriting_prefix(tmp_path: Path, default_run):
    """题面：“新运行号只追加，缺失或损坏台账直接初始化。”"""
    input_path, result_path, ledger_path = seed_world(
        tmp_path,
        default_run,
        "append",
    )
    prefix = ledger_path.read_bytes()
    changed = default_payload()
    changed["run_id"] = "RUN-AFL-2026-10-23-02"
    write_payload(input_path, changed)
    completed, _ = invoke(input_path, result_path, ledger_path)
    assert completed.returncode == 0
    assert ledger_path.read_bytes().startswith(prefix)
    entries = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(entries) == 2
    assert set(json.loads(entries[-1])) == {
        "campaign_id",
        "entry_hash",
        "input_digest",
        "output_digest",
        "previous_hash",
        "run_id",
    }


def test_same_run_replay_is_idempotent(tmp_path: Path, default_run):
    """题面：“相同输入重放时结果与台账字节不变”。"""
    input_path, result_path, ledger_path = seed_world(
        tmp_path,
        default_run,
        "idempotent",
    )
    result_before = result_path.read_bytes()
    ledger_before = ledger_path.read_bytes()
    completed, _ = invoke(input_path, result_path, ledger_path)
    assert completed.returncode == 0
    assert result_path.read_bytes() == result_before
    assert ledger_path.read_bytes() == ledger_before


def test_same_run_different_input_conflicts(tmp_path: Path, default_run):
    """题面：“同号不同输入或结果摘要按 `LEDGER_CONFLICT` 拒收”。"""
    input_path, result_path, ledger_path = seed_world(
        tmp_path,
        default_run,
        "conflict",
    )
    changed = default_payload()
    changed["campaign_id"] = "AFL-CHANGED"
    write_payload(input_path, changed)
    result_before = result_path.read_bytes()
    ledger_before = ledger_path.read_bytes()
    completed, _ = invoke(input_path, result_path, ledger_path)
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED LEDGER_CONFLICT\n"
    assert result_path.read_bytes() == result_before
    assert ledger_path.read_bytes() == ledger_before


def test_corrupt_ledger_is_reinitialized(tmp_path: Path, default_run):
    """题面：“新运行号只追加，缺失或损坏台账直接初始化。”"""
    input_path, result_path, ledger_path = seed_world(
        tmp_path,
        default_run,
        "corrupt",
    )
    ledger_path.write_bytes(b'{"broken":true}\n')
    completed, _ = invoke(input_path, result_path, ledger_path)
    entries = ledger_path.read_text(encoding="utf-8").splitlines()
    assert completed.returncode == 0
    assert len(entries) == 1
    assert set(json.loads(entries[0])) == {
        "campaign_id",
        "entry_hash",
        "input_digest",
        "output_digest",
        "previous_hash",
        "run_id",
    }


def test_rejection_preserves_inputs_and_outputs(tmp_path: Path, default_run):
    """题面：“任何拒收都不得改动输入、旧结果或旧台账”。"""
    input_path, result_path, ledger_path = seed_world(
        tmp_path,
        default_run,
        "preserve",
    )
    changed = default_payload()
    changed["clock_bound_s"] = 0.30
    write_payload(input_path, changed)
    input_before = input_path.read_bytes()
    result_before = result_path.read_bytes()
    ledger_before = ledger_path.read_bytes()
    completed, _ = invoke(input_path, result_path, ledger_path)
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert input_path.read_bytes() == input_before
    assert result_path.read_bytes() == result_before
    assert ledger_path.read_bytes() == ledger_before

    write_failure = tmp_path / "write-failure"
    write_failure.mkdir()
    write_input = write_failure / "catalogue.json"
    write_result = write_failure / "result.json"
    missing_ledger = write_failure / "missing-ledger" / "ledger.jsonl"
    write_payload(write_input, default_payload())
    write_result.write_bytes(result_before)
    write_result_before = write_result.read_bytes()
    write_run, _ = invoke(write_input, write_result, missing_ledger)
    assert write_run.returncode == 3
    assert write_run.stderr == "REFUSED BAD_INPUT\n"
    assert write_result.read_bytes() == write_result_before
    assert not missing_ledger.exists()
