from __future__ import annotations

import sqlite3
import struct

from qdf_tools.qhi_idb import export_qhi_to_sqlite


def _payload(record_type: int, record_id: int, body: bytes) -> bytes:
    return bytes((0x40, record_type, 0x80)) + struct.pack("<I", record_id) + b"\x00" * 12 + body


def _valuation_payload(record_id: int, item_id: int, date_raw: int, value: float) -> bytes:
    return (
        bytes((0x26, 0x13, 0x80))
        + struct.pack("<I", record_id)
        + b"\x00" * 8
        + struct.pack("<IHIHdH", item_id, 1, date_raw, 2, value, 3)
    )


def _policy_payload(record_id: int, coverage: float) -> bytes:
    return (
        bytes((0x32, 0x06, 0x80))
        + struct.pack("<I", record_id)
        + b"\x00\x58\xee\x6a\xbb\x06\x00\x00"
        + struct.pack("<d", coverage)
        + b"\x01\x00Homeowner/Renter\x00\x08\x00Acme\x00"
    )


def _fixture() -> bytes:
    header = bytearray(0x62)
    header[0x5C:0x62] = b"QSTUFF"
    room = _payload(5, 10, b"Kitchen\x00")
    policy = _policy_payload(20, 529500.0)
    item = _payload(
        1,
        30,
        b"".join(
            (
                struct.pack("<HI", 1, 10),
                struct.pack("<HI", 2, 20),
                struct.pack("<HI", 3, 1),
                struct.pack("<HI", 4, 1),
                struct.pack("<Hd", 5, 1100.0),
                struct.pack("<Hd", 6, 840.0),
                struct.pack("<HI", 7, 0x7B0606),
                b"\x08\x00Amazon\x00",
                b"\x09\x00Synology NAS\x00",
                b"\x0a\x00Black\x00",
                b"\x0b\x00Model X\x00",
                b"\x0c\x002330TRR06V0B7\x00",
            )
        ),
    )
    older_valuation = _valuation_payload(31, 30, 0x7B0605, 600.0)
    latest_valuation = _valuation_payload(32, 30, 0x7B0606, 670.0)
    return (
        bytes(header)
        + b"\xff\xff"
        + room
        + b"\xff\xff"
        + policy
        + b"\xff\xff"
        + item
        + b"\xff\xff"
        + older_valuation
        + b"\xff\xff"
        + latest_valuation
        + b"\xff\xff"
    )


def test_qhi_idb_export_preserves_rooms_items_and_fields(tmp_path):
    source = tmp_path / "QHI.IDB"
    destination = tmp_path / "qhi.sqlite"
    source.write_bytes(_fixture())

    counts = export_qhi_to_sqlite(source, destination)

    assert counts["rooms"] == 1
    assert counts["items"] == 1
    with sqlite3.connect(destination) as connection:
        item = connection.execute(
            "SELECT r.name, i.description, i.purchase_location, i.make_model, "
            "i.serial_number, i.replacement_cost, i.original_price, i.resale_value, "
            "i.purchase_date, "
            "p.name, p.insurer, p.coverage FROM items i "
            "JOIN rooms r ON r.id = i.room_id "
            "JOIN insurance_policies p ON p.id = i.policy_id"
        ).fetchone()
        assert item == (
            "Kitchen",
            "Synology NAS",
            "Amazon",
            "Model X",
            "2330TRR06V0B7",
            "1100",
            "840",
            "670",
            "2023-06-06",
            "Homeowner/Renter",
            "Acme",
            "529500",
        )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("9",)
        item_columns = {row[1] for row in connection.execute("PRAGMA table_info(items)")}
        assert not {"field_3_integer", "field_4_integer"} & item_columns
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "item_fields" not in tables
        assert "raw_records" not in tables
        for table in tables:
            assert "source_offset" not in {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
        assert "suggested_value" not in {
            row[1] for row in connection.execute("PRAGMA table_info(categories)")
        }
        assert connection.execute(
            "SELECT valuation_date, resale_value FROM item_valuations"
        ).fetchall() == [("2023-06-05", "600"), ("2023-06-06", "670")]
        assert "valuation_date_raw" not in {
            row[1] for row in connection.execute("PRAGMA table_info(item_valuations)")
        }
