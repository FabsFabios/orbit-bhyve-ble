"""hub_mesh_overrides option parsing + the magic2 hub-address fallback.

Replaces the hardcoded _HUB_MESH_BY_NETWORK_KEY table that used to live in
ht25.py. Covers the parse format, malformed-input tolerance, and the three-tier
resolution in hub_mesh_address (cloud record -> override -> 0x0000 placeholder).
No hardware or Home Assistant required.
"""
from __future__ import annotations

from orbit_bhyve.devices.ht25 import BHyveHT25Device, parse_hub_mesh_overrides

MAC = "AA:BB:CC:DD:EE:FF"


def _dev(hub_mesh_device_id=None, mac: str = MAC) -> BHyveHT25Device:
    record = {
        "cloud_id": "abc", "name": "Deck", "mac": mac,
        "hardware": "HT25-0000", "firmware": "0085", "stations": 1,
        "network_key": "", "mesh_device_id": 0x47D7,
        "hub_mesh_device_id": hub_mesh_device_id,
    }
    return BHyveHT25Device(None, record)


# --- parsing ---------------------------------------------------------------

def test_parses_hex_and_decimal():
    got = parse_hub_mesh_overrides("AA:BB:CC:DD:EE:FF=0xEB42, 11:22:33:44:55:66=9021")
    assert got == {"AA:BB:CC:DD:EE:FF": 0xEB42, "11:22:33:44:55:66": 9021}


def test_empty_and_blank_yield_empty_map():
    assert parse_hub_mesh_overrides("") == {}
    assert parse_hub_mesh_overrides("   ,  , ") == {}


def test_mac_is_uppercased_to_match_device_mac():
    # BHyveBleDeviceBase.mac is upper-case, so lookup must not be case-sensitive
    # on what the user typed into the options box.
    assert parse_hub_mesh_overrides("aa:bb:cc:dd:ee:ff=1") == {MAC: 1}


def test_malformed_pairs_are_skipped_not_fatal():
    # One typo must not take the whole entry down at setup.
    got = parse_hub_mesh_overrides(
        f"nonsense, {MAC}=0xEB42, 11:22:33:44:55:66=notanumber"
    )
    assert got == {MAC: 0xEB42}


def test_out_of_range_ids_are_skipped():
    # The value is written into a 2-byte field; anything wider is a typo.
    assert parse_hub_mesh_overrides(f"{MAC}=65536") == {}
    assert parse_hub_mesh_overrides(f"{MAC}=-1") == {}
    assert parse_hub_mesh_overrides(f"{MAC}=65535") == {MAC: 0xFFFF}


# --- hub_mesh_address resolution -------------------------------------------

def test_cloud_record_wins_over_override():
    dev = _dev(hub_mesh_device_id=0x1234)
    dev.hub_mesh_override = 0xEB42
    assert dev.hub_mesh_address == (0x1234).to_bytes(2, "little")


def test_override_used_when_cloud_record_lacks_bridge():
    dev = _dev(hub_mesh_device_id=None)
    dev.hub_mesh_override = 0xEB42
    assert dev.hub_mesh_address == (0xEB42).to_bytes(2, "little")


def test_placeholder_when_neither_available():
    # Init still completes with 0x0000 — the pre-existing documented behaviour
    # for an unresolved hub, now also the path when no override is configured.
    dev = _dev(hub_mesh_device_id=None)
    assert dev.hub_mesh_override is None
    assert dev.hub_mesh_address == b"\x00\x00"
