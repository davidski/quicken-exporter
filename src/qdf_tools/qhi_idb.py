"""Read-only parser and SQLite exporter for Quicken Home Inventory IDB files."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS category_groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    group_id INTEGER,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS insurance_policies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    insurer TEXT,
    coverage TEXT
);
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_defined INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    room_id INTEGER REFERENCES rooms(id),
    policy_id INTEGER REFERENCES insurance_policies(id),
    description TEXT,
    purchase_location TEXT,
    notes TEXT,
    make_model TEXT,
    serial_number TEXT,
    replacement_cost TEXT,
    original_price TEXT,
    resale_value TEXT,
    purchase_date TEXT
);
CREATE TABLE IF NOT EXISTS item_valuations (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL,
    valuation_date TEXT,
    resale_value TEXT NOT NULL,
    PRIMARY KEY (item_id, record_id)
);
CREATE INDEX IF NOT EXISTS ix_items_room ON items(room_id);
CREATE INDEX IF NOT EXISTS ix_items_policy ON items(policy_id);
CREATE INDEX IF NOT EXISTS ix_item_valuations_item_date
    ON item_valuations(item_id, valuation_date);
"""


@dataclass(frozen=True)
class Record:
    """Represent one raw record recovered from a QHI.IDB byte stream."""

    source_offset: int
    end_offset: int
    record_type: int
    record_id: int
    payload: bytes
    raw_record: bytes


@dataclass(frozen=True)
class Field:
    """Represent one typed field decoded from a QHI item payload."""

    tag: int
    value_type: str
    raw_value: bytes
    text: str | None = None
    integer: int | None = None
    real: float | None = None


def _records(data: bytes) -> list[Record]:
    markers = [match.start() for match in re.finditer(b"\xff\xff", data)]
    records = []
    for index, start in enumerate(markers[:-1]):
        if start + 9 >= len(data) or data[start + 4] != 0x80:
            continue
        end = markers[index + 1]
        payload = data[start + 2 : end]
        if len(payload) < 7:
            continue
        records.append(
            Record(
                source_offset=start,
                end_offset=end,
                record_type=payload[1],
                record_id=struct.unpack_from("<I", payload, 3)[0],
                payload=payload,
                raw_record=data[start:end],
            )
        )
    return records


def _first_text(payload: bytes, *, tag: int | None = None) -> str | None:
    if tag is None:
        matches = re.finditer(rb"[ -~]{2,}\x00", payload[8:])
    else:
        matches = re.finditer(rb"\x%02x\x00([ -~]{2,})\x00" % tag, payload[8:])
    match = next(iter(matches), None)
    if match is None:
        return None
    if tag is None:
        return match.group()[:-1].decode("cp1252", "replace")
    return match.group(1).decode("cp1252", "replace")


def _item_field_start(payload: bytes) -> int | None:
    for position in range(8, len(payload) - 8):
        if payload[position : position + 2] == b"\x01\x00":
            if payload[position + 6 : position + 8] == b"\x02\x00":
                return position
    return None


def _parse_item_fields(payload: bytes) -> list[Field]:
    position = _item_field_start(payload)
    if position is None:
        return []
    fields = []
    while position + 2 <= len(payload):
        tag = struct.unpack_from("<H", payload, position)[0]
        value_start = position + 2
        if tag in {1, 2, 3, 4, 7}:
            value_end = value_start + 4
            if value_end > len(payload):
                break
            raw = payload[value_start:value_end]
            fields.append(Field(tag, "integer", raw, integer=struct.unpack("<I", raw)[0]))
        elif tag in {5, 6}:
            value_end = value_start + 8
            if value_end > len(payload):
                break
            raw = payload[value_start:value_end]
            fields.append(Field(tag, "real", raw, real=struct.unpack("<d", raw)[0]))
        elif 8 <= tag <= 12:
            value_end = payload.find(b"\x00", value_start)
            if value_end < 0:
                break
            raw = payload[value_start:value_end]
            fields.append(Field(tag, "text", raw, text=raw.decode("cp1252", "replace")))
            value_end += 1
        else:
            break
        position = value_end
        if payload[position : position + 2] == b"\xff\xff":
            break
    return fields


def _fields_by_tag(fields: list[Field], tag: int) -> list[Field]:
    return [field for field in fields if field.tag == tag]


def _first_field(fields: list[Field], tag: int) -> Field | None:
    return next(iter(_fields_by_tag(fields, tag)), None)


def _date_from_packed(value: int | None) -> str | None:
    if value is None:
        return None
    day = value & 0xFF
    month = (value >> 8) & 0xFF
    year = (value >> 16) & 0xFF
    try:
        return dt.date(1900 + year, month, day).isoformat()
    except ValueError:
        return None


def _amount(value: float | None) -> str | None:
    if value is None:
        return None
    return format(value, ".17g")


def _item_valuation(record: Record) -> tuple[int, int, float] | None:
    """Decode a type-19 item resale valuation record, if present."""
    payload = record.payload
    if (
        record.record_type != 19
        or len(payload) != 37
        or payload[19:21] != b"\x01\x00"
        or payload[25:27] != b"\x02\x00"
        or payload[35:37] != b"\x03\x00"
    ):
        return None
    item_id = struct.unpack_from("<I", payload, 15)[0]
    valuation_date_raw = struct.unpack_from("<I", payload, 21)[0]
    resale_value = struct.unpack_from("<d", payload, 27)[0]
    return item_id, valuation_date_raw, resale_value


def _policy_coverage(record: Record) -> str | None:
    """Decode the coverage amount in a type-6 policy record, if present."""
    if record.record_type != 6 or len(record.payload) < 25 or record.payload[23:25] != b"\x01\x00":
        return None
    return _amount(struct.unpack_from("<d", record.payload, 15)[0])


def parse_qhi_idb(source: str | Path) -> tuple[bytes, list[Record]]:
    """Validate and parse a QHI.IDB file into raw bytes and records."""
    data = Path(source).read_bytes()
    if len(data) < 0x62 or data[0x5C:0x62] != b"QSTUFF":
        raise ValueError("not a recognized QHI.IDB file: missing QSTUFF marker")
    return data, _records(data)


def export_qhi_to_sqlite(source: str | Path, destination: str | Path) -> dict[str, int]:
    """Export a QHI.IDB file to SQLite and return row counts by entity."""
    source = Path(source)
    destination = Path(destination)
    data, records = parse_qhi_idb(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with sqlite3.connect(destination) as connection:
        connection.executescript(SCHEMA)
        for table in (
            "item_valuations",
            "items",
            "rooms",
            "insurance_policies",
            "categories",
            "category_groups",
            "metadata",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version", "9"),
                ("source_format", "qhi-idb"),
                ("source_name", source.name),
                ("source_size", str(len(data))),
                ("source_sha256", hashlib.sha256(data).hexdigest()),
            ],
        )
        named_categories: set[int] = set()
        for record in records:
            if record.record_type == 4:
                name = _first_text(record.payload)
                if name:
                    connection.execute(
                        "INSERT INTO category_groups(id, name) VALUES (?, ?)",
                        (record.record_id, name),
                    )
            elif record.record_type == 5:
                name = _first_text(record.payload)
                if name:
                    connection.execute(
                        "INSERT INTO rooms(id, name, is_defined) VALUES (?, ?, 1)",
                        (record.record_id, name),
                    )
            elif record.record_type == 6:
                values = [
                    match.group()[:-1].decode("cp1252", "replace")
                    for match in re.finditer(rb"[ -~]{2,}\x00", record.payload[8:])
                ]
                if values:
                    connection.execute(
                        "INSERT INTO insurance_policies(id, name, insurer, coverage) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            record.record_id,
                            values[0],
                            values[1] if len(values) > 1 else None,
                            _policy_coverage(record),
                        ),
                    )
            elif record.record_type == 19:
                name = _first_text(record.payload, tag=2)
                if name:
                    group_id = (
                        struct.unpack_from("<I", record.payload, 15)[0]
                        if len(record.payload) >= 19
                        else None
                    )
                    connection.execute(
                        "INSERT INTO categories(id, group_id, name) VALUES (?, ?, ?)",
                        (record.record_id, group_id, name),
                    )
                    named_categories.add(record.record_id)

        parsed_items = [
            (record, _parse_item_fields(record.payload))
            for record in records
            if record.record_type == 1
        ]
        item_ids = {record.record_id for record, _ in parsed_items}
        valuations_by_item: dict[int, list[tuple[Record, int, float]]] = {}
        for record in records:
            valuation = _item_valuation(record)
            if valuation is not None and valuation[0] in item_ids:
                valuations_by_item.setdefault(valuation[0], []).append(
                    (record, valuation[1], valuation[2])
                )
        room_ids = {
            field.integer
            for _, fields in parsed_items
            for field in _fields_by_tag(fields, 1)
            if field.integer is not None
        }
        known_rooms = {row[0] for row in connection.execute("SELECT id FROM rooms")}
        for room_id in sorted(room_ids - known_rooms):
            connection.execute(
                "INSERT INTO rooms(id, name, is_defined) VALUES (?, ?, 0)",
                (room_id, f"[unresolved QHI room {room_id}]"),
            )

        item_count = 0
        for record, fields in parsed_items:
            by_tag = {tag: _first_field(fields, tag) for tag in (1, 2, 5, 6, 7, 8, 9, 10, 11, 12)}
            latest_valuation = max(
                valuations_by_item.get(record.record_id, []),
                key=lambda valuation: (valuation[1], valuation[0].record_id),
                default=None,
            )
            row = (
                record.record_id,
                by_tag[1].integer if by_tag[1] else None,
                by_tag[2].integer if by_tag[2] else None,
                by_tag[9].text if by_tag[9] else None,
                by_tag[8].text if by_tag[8] else None,
                by_tag[10].text if by_tag[10] else None,
                by_tag[11].text if by_tag[11] else None,
                by_tag[12].text if by_tag[12] else None,
                _amount(by_tag[5].real if by_tag[5] else None),
                _amount(by_tag[6].real if by_tag[6] else None),
                _amount(latest_valuation[2] if latest_valuation else None),
                _date_from_packed(by_tag[7].integer if by_tag[7] else None),
            )
            connection.execute(
                "INSERT INTO items(id, room_id, policy_id, description, purchase_location, notes, "
                "make_model, serial_number, replacement_cost, "
                "original_price, resale_value, purchase_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            item_count += 1

        valuation_rows = [
            (
                item_id,
                record.record_id,
                _date_from_packed(valuation_date_raw),
                _amount(resale_value),
            )
            for item_id, valuations in valuations_by_item.items()
            for record, valuation_date_raw, resale_value in valuations
        ]
        connection.executemany(
            "INSERT INTO item_valuations(item_id, record_id, valuation_date, resale_value) "
            "VALUES (?, ?, ?, ?)",
            valuation_rows,
        )

        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("record_count", str(len(records))),
                ("room_count", str(connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0])),
                (
                    "category_count",
                    str(connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]),
                ),
                ("item_count", str(item_count)),
                ("item_valuation_count", str(len(valuation_rows))),
            ],
        )
    return {
        "records": len(records),
        "rooms": sum(record.record_type == 5 for record in records),
        "categories": len(named_categories),
        "items": item_count,
        "item_valuations": len(valuation_rows),
    }
