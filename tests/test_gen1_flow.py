"""Gen1 (HT25 fw0085) flow-sample decode + accumulation tests.

Covers BHyveHT25Device.apply_gen1_flow_frame — the RX side of the undocumented
0x89 flow subscription — plus reset_gen1_flow and the subscribe-frame builder.
Frame guards, u16 counter wrap, the increment/dt sanity windows and the
counts/litre calibration are all exercised with synthetic frames. No hardware
or Home Assistant required.
"""
from __future__ import annotations

import time

import pytest

from orbit_bhyve.devices.ht25 import BHyveHT25Device

MESH_ID = 0x47D7
MESH = MESH_ID.to_bytes(2, "little")  # d7 47
SEQ_FLOW = 0x0B
ROUTING = 0x40
CAL = 112  # counts/litre used throughout, set explicitly on the fixture


def _dev(counts_per_litre: int = CAL) -> BHyveHT25Device:
    # Key-less record -> no BLE connection, no hass needed (same trick as
    # test_mesh_status.py::_dev).
    record = {
        "cloud_id": "abc", "name": "Deck", "mac": "AA:BB:CC:DD:EE:FF",
        "hardware": "HT25-0000", "firmware": "0085", "stations": 1,
        "network_key": "", "mesh_device_id": MESH_ID,
    }
    dev = BHyveHT25Device(None, record)
    dev.flow_counts_per_litre = counts_per_litre
    dev.reset_gen1_flow()
    return dev


def _flow_frame(ctr: int, mesh: bytes = MESH, seq: int = SEQ_FLOW) -> bytes:
    """[mesh:2][type:1][seq=0x0b][routing:1][pad:4][counter u16 LE @9:11]."""
    return (
        mesh
        + bytes([0x8B, seq, ROUTING])
        + b"\x00\x00\x00\x00"
        + (ctr & 0xFFFF).to_bytes(2, "little")
    )


# --- frame guards ----------------------------------------------------------

def test_short_frame_is_rejected():
    dev = _dev()
    assert dev.apply_gen1_flow_frame(_flow_frame(100)[:10]) is False
    assert dev.state.water_used_gen1_l is None


def test_foreign_mesh_address_is_rejected():
    # A neighbour's timer on the same channel must not move our counters.
    dev = _dev()
    other = (0x1234).to_bytes(2, "little")
    assert dev.apply_gen1_flow_frame(_flow_frame(100, mesh=other)) is False
    assert dev.apply_gen1_flow_frame(_flow_frame(212, mesh=other)) is False
    assert dev.state.water_used_gen1_l is None


def test_non_flow_seq_byte_is_rejected():
    dev = _dev()
    assert dev.apply_gen1_flow_frame(_flow_frame(100, seq=0x02)) is False
    assert dev.state.water_used_gen1_l is None


def test_frame_ignored_when_mesh_id_unknown():
    # A cloud record without a mesh_device_id must make flow frames a no-op,
    # not raise out of the mesh_address property — _observe_plaintext calls
    # this on every inbound frame, so raising here breaks all status decode.
    dev = _dev()
    dev.mesh_device_id = None
    assert dev.apply_gen1_flow_frame(_flow_frame(100)) is False


    # --- baseline + accumulation ----------------------------------------------

def test_first_frame_only_establishes_baseline():
    # Accepted, but there is no previous counter to difference against, so
    # nothing is booked and the sensors stay unset.
    dev = _dev()
    assert dev.apply_gen1_flow_frame(_flow_frame(500)) is True
    assert dev.state.water_used_gen1_l is None
    assert dev.state.flow_lpm_gen1 is None


def test_second_frame_books_litres_at_calibration():
    dev = _dev()
    dev.apply_gen1_flow_frame(_flow_frame(1000))
    dev.apply_gen1_flow_frame(_flow_frame(1000 + CAL))
    # Exactly one litre's worth of ticks.
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


def test_accumulation_is_cumulative_across_frames():
    dev = _dev()
    for ctr in (0, CAL, 2 * CAL, 3 * CAL):
        dev.apply_gen1_flow_frame(_flow_frame(ctr))
    assert dev.state.water_used_gen1_l == pytest.approx(3.0)


def test_calibration_value_scales_the_conversion():
    # Same tick delta, different counts/litre -> proportional litres.
    dev = _dev(counts_per_litre=224)
    dev.apply_gen1_flow_frame(_flow_frame(0))
    dev.apply_gen1_flow_frame(_flow_frame(224))
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


def test_counter_wraps_at_65536():
    # 65530 -> 10 is a 16-tick increment, not a 65520-tick rewind.
    dev = _dev()
    dev.apply_gen1_flow_frame(_flow_frame(65530))
    dev.apply_gen1_flow_frame(_flow_frame(10))
    assert dev.state.water_used_gen1_l == pytest.approx(round(16 / CAL, 2))


# --- increment sanity window ----------------------------------------------

def test_zero_increment_books_nothing():
    # Valve open, no flow: the device keeps sampling but the counter is static.
    dev = _dev()
    dev.apply_gen1_flow_frame(_flow_frame(4242))
    dev.apply_gen1_flow_frame(_flow_frame(4242))
    assert dev.state.water_used_gen1_l is None


def test_absurd_increment_is_discarded():
    # >= 30000 ticks between samples is not physically reachable at a 3 s
    # interval; treat as a desynced/garbage frame rather than book ~268 L.
    dev = _dev()
    dev.apply_gen1_flow_frame(_flow_frame(0))
    dev.apply_gen1_flow_frame(_flow_frame(30000))
    assert dev.state.water_used_gen1_l is None
    # The rejected counter still re-anchors the baseline, so the next
    # plausible delta is measured from it and not from the stale origin.
    dev.apply_gen1_flow_frame(_flow_frame(30000 + CAL))
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


# --- instantaneous rate ----------------------------------------------------

def test_rate_computed_inside_dt_window(monkeypatch):
    dev = _dev()
    ticks = iter([100.0, 102.0])  # 2 s apart, inside the 0.5-5.0 s window
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    dev.apply_gen1_flow_frame(_flow_frame(0))
    dev.apply_gen1_flow_frame(_flow_frame(CAL))
    # 1 L in 2 s = 30 L/min.
    assert dev.state.flow_lpm_gen1 == pytest.approx(30.0)
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


def test_rate_suppressed_outside_dt_window_but_volume_still_books(monkeypatch):
    # A long gap (dropped notifications, reconnect) makes the derived rate
    # meaningless, but the tick delta is still real water.
    dev = _dev()
    ticks = iter([100.0, 130.0])  # 30 s apart
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    dev.apply_gen1_flow_frame(_flow_frame(0))
    dev.apply_gen1_flow_frame(_flow_frame(CAL))
    assert dev.state.flow_lpm_gen1 is None
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


# --- lifecycle -------------------------------------------------------------

def test_accumulation_notifies_coordinator():
    dev = _dev()
    pokes: list[int] = []
    dev.set_state_changed_callback(lambda: pokes.append(1))
    dev.apply_gen1_flow_frame(_flow_frame(0))
    assert pokes == []              # baseline frame books nothing
    dev.apply_gen1_flow_frame(_flow_frame(CAL))
    assert pokes == [1]


def test_reset_clears_totals_and_baseline():
    dev = _dev()
    dev.apply_gen1_flow_frame(_flow_frame(0))
    dev.apply_gen1_flow_frame(_flow_frame(CAL))
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)
    dev.reset_gen1_flow()
    assert dev.state.water_used_gen1_l is None
    assert dev.state.flow_lpm_gen1 is None
    # Baseline is dropped too: the next run starts from zero rather than
    # booking the whole inter-run counter gap as water used.
    dev.apply_gen1_flow_frame(_flow_frame(50000))
    assert dev.state.water_used_gen1_l is None
    dev.apply_gen1_flow_frame(_flow_frame(50000 + CAL))
    assert dev.state.water_used_gen1_l == pytest.approx(1.0)


# --- subscribe frame -------------------------------------------------------

def test_subscribe_frame_carries_interval_and_sample_budget():
    dev = _dev()
    frame = dev._flow_subscribe_frame()
    assert frame[0:2] == MESH
    assert frame[2] == 0x89
    assert frame[3] == 0x0E
    interval = int.from_bytes(frame[5:7], "little")
    samples = int.from_bytes(frame[7:9], "little")
    assert (interval, samples) == (3000, 700)
    # The budget must outlast the longest supported program (30 min).
    assert interval * samples / 1000 >= 30 * 60
