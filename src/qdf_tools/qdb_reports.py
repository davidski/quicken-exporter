"""Saved-report definitions exposed by Quicken's QDB item type 0x120."""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPORT_MAGIC = b"QRPT"
REPORT_EXTRACT_VERSION = 1
REPORT_HEADER_SIZE = 0x78
REPORT_COMPONENT_SIZE = 0x15A
REPORT_NAME_SIZE = 0x42
REPORT_TYPE_OFFSET = 0x46
REPORT_FILTER_SIZE_OFFSET = 0x112
FILTER_HEADER_SIZE = 0x98
FILTER_COUNT_OFFSET = 0x18
FILTER_GROUP_COUNT = 16
FILTER_PADDING_VALUES = {0, 0xFFFF}
FILTER_GROUP_KINDS = {6: "account", 7: "category"}
REPORT_SETTING_START = 0x40
REPORT_SETTING_WORD_SIZE = 2
REPORT_HEADER_SETTING_OFFSET = 0x42
REPORT_ORGANIZATION_OFFSET = REPORT_HEADER_SETTING_OFFSET
REPORT_DATE_RANGE_OFFSET = 0x52
REPORT_INTERVAL_OFFSET = 0x56
REPORT_ROUNDING_OFFSET = 0x9E
REPORT_TRANSFER_MODE_OFFSET = 0x96
REPORT_SUBCATEGORY_MODE_OFFSET = 0x9A
REPORT_STATUS_HEADER_WORD_OFFSET = 0x0C
REPORT_STATUS_SELECTION_WORD_OFFSET = 0x10
REPORT_CUSTOM_START_MONTH_DAY_OFFSET = 0x5A
REPORT_CUSTOM_START_YEAR_OFFSET = 0x5C
REPORT_CUSTOM_END_MONTH_DAY_OFFSET = 0x5E
REPORT_CUSTOM_END_YEAR_OFFSET = 0x60

REPORT_HEADER_SETTING_NAMES = {
    2: "Cash flow basis",
    10: "Sort by: Account/Date",
    18: "Year",
}
REPORT_ROUNDING_NAMES = {
    0: None,
    1: "Cents (no rounding)",
}
REPORT_DATE_RANGE_NAMES = {
    2: "Year to date",
    3: "Last month",
    23: "Last 12 months",
    0xFFFF: "Custom range",
}
REPORT_INTERVAL_NAMES = {
    18: "Month",
}
REPORT_TRANSFER_MODE_NAMES = {
    1: "Include all",
    2: "Exclude internal",
}
REPORT_SUBCATEGORY_MODE_NAMES = {
    3: "Show all",
    5: "Hide all",
}

# These labels are deliberately limited to values established by the current
# controlled report sample. Every numeric code is exported alongside the label,
# so a new Quicken build cannot silently turn an unseen value into a misleading
# interpretation.
REPORT_SETTING_FIELD_NAMES = {
    REPORT_HEADER_SETTING_OFFSET: "header_setting",
    REPORT_ROUNDING_OFFSET: "rounding",
    REPORT_DATE_RANGE_OFFSET: "date_range",
    REPORT_INTERVAL_OFFSET: "interval",
    REPORT_TRANSFER_MODE_OFFSET: "advanced_transfer_word",
    REPORT_SUBCATEGORY_MODE_OFFSET: "advanced_subcategory_word",
}


# Descriptive names for the QREPORT_TYPE values observed in the sample QDF.
# The itemized variants are confirmed by qreports.dll assertions and dispatch
# logic.  The other labels combine qreports.dll's implementation modules with
# the saved reports that exercise each value.  Unknown values remain lossless
# and receive an explicit numeric fallback.
REPORT_TYPE_NAMES = {
    1: "Net Worth",
    2: "Account Balances",
    4: "Transactions",
    7: "Itemized Categories",
    13: "Cash Flow (graph)",
    14: "Expense Summary (graph)",
    20: "Budget",
    29: "Cash Flow (table)",
    32: "Spending",
    47: "Itemized Payees",
    55: "Itemized Tags",
}


def report_type_name(report_type: int) -> str:
    """Return a readable label without discarding an unknown numeric value."""
    return REPORT_TYPE_NAMES.get(report_type, f"Unknown report type {report_type}")


@dataclass(frozen=True)
class QdbReportFilterGroup:
    """Represent one hashed account or category filter group."""

    index: int
    kind: str
    capacity: int
    raw_values: tuple[int, ...]
    entity_names: tuple[tuple[int, str], ...] = ()
    catalog_types: tuple[tuple[int, int | None], ...] = ()

    @property
    def values(self) -> tuple[tuple[int, int], ...]:
        """Return ``(slot, reference)`` pairs without empty hash-table slots."""
        return tuple(
            (slot, value)
            for slot, value in enumerate(self.raw_values)
            if value not in FILTER_PADDING_VALUES
        )


@dataclass(frozen=True)
class QdbReportFilter:
    """Represent a lossless saved-report filter payload."""

    stored_size: int
    header: bytes
    groups: tuple[QdbReportFilterGroup, ...]
    trailer: bytes

    raw: bytes


@dataclass(frozen=True)
class QdbReportComponent:
    """Represent one component of a saved report."""

    index: int
    name: str
    report_type: int
    filter_size: int
    settings: bytes
    report_filter: QdbReportFilter

    @property
    def report_type_name(self) -> str:
        """Return the readable report-type label for this component."""
        return report_type_name(self.report_type)

    @property
    def organization_code(self) -> int:
        """Return the serialized organization option code."""
        return struct.unpack_from("<H", self.settings, REPORT_ORGANIZATION_OFFSET)[0]

    @property
    def header_setting_code(self) -> int:
        """Return the serialized report header-setting code."""
        return struct.unpack_from("<H", self.settings, REPORT_HEADER_SETTING_OFFSET)[0]

    @property
    def header_setting(self) -> str | None:
        """Return a recognized header-setting label for its report family."""
        if self.header_setting_code == 2 and self.report_type not in (13, 29):
            return None
        return REPORT_HEADER_SETTING_NAMES.get(self.header_setting_code)

    @property
    def organization(self) -> str | None:
        """Return the organization label for validated cash-flow reports."""
        if self.report_type not in (13, 29):
            return None
        return "Cash flow basis" if self.organization_code == 2 else None

    def _setting_word(self, offset: int) -> int:
        return struct.unpack_from("<H", self.settings, offset)[0]

    @property
    def rounding_raw_code(self) -> int:
        """Return the raw word at the rounding-control offset."""
        return self._setting_word(REPORT_ROUNDING_OFFSET)

    @property
    def rounding_code(self) -> int:
        """Return the validated semantic cents/rounding code."""
        # The transaction-family encoding is inverted: raw zero means
        # the Cents (no rounding) checkbox is checked.
        if self.report_type == 4 and self.rounding_raw_code == 0:
            return 1
        return self.rounding_raw_code

    @property
    def rounding_checked(self) -> bool | None:
        """Return whether the cents/no-rounding checkbox is checked."""
        if self.rounding_code not in (0, 1):
            return None
        return bool(self.rounding_code)

    @property
    def rounding(self) -> str | None:
        """Return the cents/no-rounding label when checked."""
        return REPORT_ROUNDING_NAMES.get(self.rounding_code)

    @property
    def date_range_code(self) -> int:
        """Return the serialized date-range preset code."""
        return struct.unpack_from("<H", self.settings, REPORT_DATE_RANGE_OFFSET)[0]

    @property
    def date_range(self) -> str | None:
        """Return a recognized date-range label, if one is known."""
        return REPORT_DATE_RANGE_NAMES.get(self.date_range_code)

    @property
    def interval_code(self) -> int:
        """Return the serialized report interval/heading-column code."""
        return struct.unpack_from("<H", self.settings, REPORT_INTERVAL_OFFSET)[0]

    @property
    def interval(self) -> str | None:
        """Return a recognized report interval label, if one is known."""
        if (
            self.report_type in (1, 2)
            and self.interval_code == 0xFFFF
            and self._setting_word(0x4E) == 0x020F
        ):
            return "Year"
        return REPORT_INTERVAL_NAMES.get(self.interval_code)

    @property
    def budget(self) -> str | None:
        """Return the validated budget source label, when known."""
        if self.report_type != 20:
            return None
        if (
            self.date_range_code == 23
            and self.interval_code == 18
            and self._setting_word(0x4E) == 0x020D
        ) or (
            self.date_range_code == 3
            and self.interval_code == 0
            and self._setting_word(0x4E) == 0x0208
        ):
            return "Cyentia"
        return None

    @property
    def transfer_mode_code(self) -> int:
        """Return the raw word currently associated with transfer controls."""
        return self._setting_word(REPORT_TRANSFER_MODE_OFFSET)

    @property
    def transfer_mode(self) -> str | None:
        """Return a transfer label only for a validated report signature."""
        if self.report_type == 7 and self.transfer_mode_code == 2:
            return "Exclude internal"
        if (
            self.report_type == 4
            and self.date_range_code == 23
            and not self.report_filter.groups[7].values
            and self._setting_word(0x4E) == 0x020D
        ):
            return "Exclude self-transfers"
        if (
            self.report_type == 4
            and self.date_range_code == 0xFFFF
            and self.report_filter.groups[7].values
            and self._setting_word(0x4E) == 0x020B
        ):
            return "Include all"
        if (
            self.report_type == 32
            and self.report_filter.groups[9].values
            and self._setting_word(0x4E) == 0x020B
        ):
            return "Exclude self-transfers"
        if (
            self.report_type == 29
            and self.transfer_mode_code == 1
            and (
                self.report_filter.groups[7].values
                or (
                    self._setting_word(0x4E) == 0x0206
                    and self.date_range_code == 2
                    and self.interval_code == 18
                )
            )
        ):
            return (
                "Exclude internal"
                if self.report_filter.groups[7].values
                else "Exclude self-transfers"
            )
        if self.report_type == 13 and self.transfer_mode_code == 1:
            return "Exclude self-transfers"
        if (
            self.report_type == 14
            and self.report_filter.groups[7].values
            and self._setting_word(0x4E) == 0x020D
        ):
            return "Exclude all"
        return None

    @property
    def subcategory_mode_code(self) -> int:
        """Return the raw word currently associated with subcategories."""
        return self._setting_word(REPORT_SUBCATEGORY_MODE_OFFSET)

    @property
    def subcategory_mode(self) -> str | None:
        """Return a subcategory label only for a validated report signature."""
        if (
            self.report_type == 4
            and self.date_range_code == 0xFFFF
            and self.report_filter.groups[7].values
            and self._setting_word(0x4E) == 0x020B
        ):
            return "Show all"
        if (
            self.report_type == 4
            and self.date_range_code == 23
            and not self.report_filter.groups[7].values
            and self._setting_word(0x4E) == 0x020D
            and self.subcategory_mode_code == 3
        ):
            return "Show all"
        if (
            self.report_type == 7
            and self.report_filter.groups[7].values
            and self.subcategory_mode_code == 0
        ):
            return "Show all"
        if self.report_type == 29 and self.subcategory_mode_code == 5:
            return "Hide all"
        if (
            self.report_type == 32
            and self.report_filter.groups[9].values
            and self.subcategory_mode_code == 0
        ):
            return "Hide all"
        if (
            self.report_type == 14
            and self.report_filter.groups[7].values
            and self.subcategory_mode_code == 1
        ):
            return "Show all"
        return None

    @property
    def heading_row(self) -> str | None:
        """Return the validated heading row label, when known."""
        if self.report_type == 14 and self.report_filter.groups[7].values:
            return "Category"
        return None

    @property
    def heading_column_code(self) -> int | None:
        """Return the validated heading-column code, when known."""
        if self.header_setting_code == 18:
            return 18
        if self.report_type == 32 and self.report_filter.groups[9].values:
            return 18
        return None

    @property
    def heading_column(self) -> str | None:
        """Return the validated heading-column label, when known."""
        if self.report_type == 14 and self.report_filter.groups[7].values:
            return "Don't subtotal"
        return "Year" if self.heading_column_code == 18 else None

    @property
    def tag_filter_mode(self) -> str | None:
        """Return the validated tag-filter mode for Spending reports."""
        if self.report_type == 32 and self.report_filter.groups[9].values:
            return "Include only selected"
        return None

    @property
    def subtotal(self) -> str | None:
        """Return the confirmed subtotal label when its control is known."""
        if self.report_type == 14 and self.report_filter.groups[7].values:
            return "Don't subtotal"
        return (
            "Don't subtotal"
            if self.report_type == 4 and self.report_filter.groups[7].values
            else None
        )

    @property
    def sort_by(self) -> str | None:
        """Return the confirmed report sort label when its control is known."""
        if self.report_type == 4 and self.report_filter.groups[7].values:
            return "Date/Account"
        if self.header_setting_code == 10:
            return "Account/Date"
        return None

    def _filter_header_word(self, offset: int) -> int:
        return struct.unpack_from("<H", self.report_filter.header, offset)[0]

    @property
    def status_filter_words(self) -> tuple[int, int]:
        """Return the two raw Advanced-tab status-filter words."""
        return (
            self._filter_header_word(REPORT_STATUS_HEADER_WORD_OFFSET),
            self._filter_header_word(REPORT_STATUS_SELECTION_WORD_OFFSET),
        )

    @property
    def status_not_cleared(self) -> bool | None:
        """Return the validated Advanced-tab status checkbox state."""
        return self._status_value("not_cleared")

    @property
    def status_newly_cleared(self) -> bool | None:
        """Return the validated Advanced-tab status checkbox state."""
        return self._status_value("newly_cleared")

    @property
    def status_newly_reconciled(self) -> bool | None:
        """Return the validated Advanced-tab status checkbox state."""
        return self._status_value("newly_reconciled")

    @property
    def status_reconciled(self) -> bool | None:
        """Return the validated Advanced-tab status checkbox state."""
        return self._status_value("reconciled")

    def _status_value(self, name: str) -> bool | None:
        status_values = {
            (0x9FFE, 0x9FFF): {
                "not_cleared": True,
                "newly_cleared": True,
                "newly_reconciled": None,
                "reconciled": True,
            },
            (0x9FEE, 0x9FFF): {
                "not_cleared": True,
                "newly_cleared": False,
                "newly_reconciled": None,
                "reconciled": False,
            },
            (0xFF7F, 0xFFFF): {
                "not_cleared": True,
                "newly_cleared": True,
                "newly_reconciled": True,
                "reconciled": True,
            },
            (0xFFFE, 0xFFFF): {
                "not_cleared": None,
                "newly_cleared": True,
                "newly_reconciled": True,
                "reconciled": True,
            },
        }
        if self.report_type not in (4, 7, 14, 29, 32):
            return None
        if self.report_type == 29:
            if (
                not self.report_filter.groups[7].values
                and self._setting_word(0x4E) == 0x0206
                and self.date_range_code == 2
                and self.interval_code == 18
                and self.status_filter_words == (0xFFFE, 0xFFFF)
            ):
                return {
                    "not_cleared": True,
                    "newly_cleared": True,
                    "newly_reconciled": None,
                    "reconciled": True,
                }.get(name)
            if not self.report_filter.groups[7].values:
                return None
            if self.status_filter_words == (0xFFFE, 0xFFFF):
                return True
        values = status_values.get(self.status_filter_words)
        return values.get(name) if values else None

    @property
    def custom_date_words(self) -> tuple[tuple[int, int], ...]:
        """Return raw words used by custom date ranges."""
        return tuple(
            (offset, struct.unpack_from("<H", self.settings, offset)[0])
            for offset in (
                REPORT_CUSTOM_START_MONTH_DAY_OFFSET,
                REPORT_CUSTOM_START_YEAR_OFFSET,
                REPORT_CUSTOM_END_MONTH_DAY_OFFSET,
                REPORT_CUSTOM_END_YEAR_OFFSET,
            )
        )

    @property
    def custom_start_date(self) -> date | None:
        """Decode the custom-range start date when this report uses one."""
        if self.date_range_code != 0xFFFF:
            return None
        return _decode_custom_date(
            self.settings,
            REPORT_CUSTOM_START_MONTH_DAY_OFFSET,
            REPORT_CUSTOM_START_YEAR_OFFSET,
        )

    @property
    def custom_end_date(self) -> date | None:
        """Decode the custom-range end date when this report uses one."""
        if self.date_range_code != 0xFFFF:
            return None
        return _decode_custom_date(
            self.settings,
            REPORT_CUSTOM_END_MONTH_DAY_OFFSET,
            REPORT_CUSTOM_END_YEAR_OFFSET,
        )

    @property
    def category_filter_mode_code(self) -> int | None:
        """Return no raw mode code until the report-type controls are mapped."""
        return None

    @property
    def category_filter_mode(self) -> str | None:
        """Interpret category values using the confirmed report controls."""
        category_values = self.report_filter.groups[7].values
        if not category_values:
            return "Include values with any categories"
        if self.report_type == 4:
            return "Include only selected categories"
        if self.report_type == 7:
            return "Include all categories"
        if self.report_type == 32:
            return "Include only transactions with selected categories"
        if self.report_type == 29:
            return "All categories except selected"
        if self.report_type == 14:
            return "Include only selected categories"
        if self.report_type == 13:
            return "Selected categories only"
        return None

    @property
    def account_filter_mode(self) -> str:
        """Interpret the account filter from the confirmed report controls."""
        if self.report_type == 7 or (self.report_type == 4 and self.report_filter.groups[7].values):
            return "Include all accounts"
        return "Only selected accounts" if self.report_filter.groups[6].values else "All accounts"

    @property
    def setting_words(self) -> tuple[tuple[int, int], ...]:
        """Return every fixed settings word outside the component name."""
        return tuple(
            (offset, struct.unpack_from("<H", self.settings, offset)[0])
            for offset in range(
                REPORT_SETTING_START,
                len(self.settings),
                REPORT_SETTING_WORD_SIZE,
            )
        )


@dataclass(frozen=True)
class QdbSavedReport:
    """Represent one losslessly parsed saved-report record."""

    qdb_index: int
    qdb_qid: int
    format_word_04: int
    format_word_08: int
    format_word_0c: int
    declared_size: int
    header: bytes
    components: tuple[QdbReportComponent, ...]
    trailing_data: bytes
    raw: bytes

    @property
    def name(self) -> str:
        """Return the first component name, or an empty string if absent."""
        return self.components[0].name if self.components else ""

    @property
    def report_type(self) -> int | None:
        """Return the first component report type, or ``None`` if absent."""
        return self.components[0].report_type if self.components else None


def _cstring(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("cp1252", errors="replace").strip()


def _decode_custom_date(settings: bytes, month_day_offset: int, year_offset: int) -> date | None:
    month_day = struct.unpack_from("<H", settings, month_day_offset)[0]
    year_code = struct.unpack_from("<H", settings, year_offset)[0]
    month = month_day >> 8
    day = month_day & 0xFF
    if not (1 <= month <= 12 and 1 <= day <= 31 and year_code != 0xFFFF):
        return None
    try:
        return date(1900 + year_code, month, day)
    except ValueError:
        return None


def _parse_filter(data: bytes, stored_size: int) -> QdbReportFilter:
    if len(data) != stored_size:
        raise ValueError(f"report filter length {len(data)} != declared size {stored_size}")
    if len(data) < FILTER_HEADER_SIZE + 1:
        raise ValueError(f"report filter is too short: {len(data)}")
    counts = struct.unpack_from(f"<{FILTER_GROUP_COUNT}H", data, FILTER_COUNT_OFFSET)
    cursor = FILTER_HEADER_SIZE
    groups = []
    for index, capacity in enumerate(counts):
        end = cursor + capacity * 2
        if end > len(data):
            raise ValueError(f"report filter group {index} overruns its {stored_size}-byte storage")
        values = struct.unpack_from(f"<{capacity}H", data, cursor) if capacity else ()
        groups.append(
            QdbReportFilterGroup(
                index=index,
                kind=FILTER_GROUP_KINDS.get(index, "unknown"),
                capacity=capacity,
                raw_values=tuple(values),
            )
        )
        cursor = end
    return QdbReportFilter(
        stored_size=stored_size,
        header=data[:FILTER_HEADER_SIZE],
        groups=tuple(groups),
        trailer=data[cursor:],
        raw=data,
    )


def _parse_report(qdb_index: int, record: bytes, base_size: int) -> QdbSavedReport:
    if len(record) < REPORT_HEADER_SIZE:
        raise ValueError(f"report {qdb_index} is shorter than its fixed header")
    component_count = struct.unpack_from("<I", record, 0x10)[0]
    declared_size = struct.unpack_from("<I", record, 0x14)[0]
    if declared_size != len(record):
        raise ValueError(
            f"report {qdb_index} length {len(record)} != declared size {declared_size}"
        )
    fixed_size = REPORT_HEADER_SIZE + component_count * REPORT_COMPONENT_SIZE
    if fixed_size < base_size or fixed_size > len(record):
        raise ValueError(f"report {qdb_index} has invalid component count {component_count}")

    filter_cursor = fixed_size
    components = []
    for component_index in range(component_count):
        start = REPORT_HEADER_SIZE + component_index * REPORT_COMPONENT_SIZE
        settings = record[start : start + REPORT_COMPONENT_SIZE]
        filter_size = struct.unpack_from("<I", settings, REPORT_FILTER_SIZE_OFFSET)[0]
        filter_end = filter_cursor + filter_size
        if filter_end > len(record):
            raise ValueError(
                f"report {qdb_index} component {component_index} filter overruns record"
            )
        report_filter = _parse_filter(record[filter_cursor:filter_end], filter_size)
        components.append(
            QdbReportComponent(
                index=component_index,
                name=_cstring(settings[:REPORT_NAME_SIZE]),
                report_type=struct.unpack_from("<I", settings, REPORT_TYPE_OFFSET)[0],
                filter_size=filter_size,
                settings=settings,
                report_filter=report_filter,
            )
        )
        filter_cursor = filter_end

    return QdbSavedReport(
        qdb_index=qdb_index,
        qdb_qid=struct.unpack_from("<I", record, 0)[0],
        format_word_04=struct.unpack_from("<I", record, 0x04)[0],
        format_word_08=struct.unpack_from("<I", record, 0x08)[0],
        format_word_0c=struct.unpack_from("<I", record, 0x0C)[0],
        declared_size=declared_size,
        header=record[:REPORT_HEADER_SIZE],
        components=tuple(components),
        trailing_data=record[filter_cursor:],
        raw=record,
    )


def parse_qdb_reports(source: str | Path) -> list[QdbSavedReport]:
    """Parse the lossless ``qdb-reports.bin`` emitted by the Windows helper."""
    data = Path(source).read_bytes()
    if len(data) < 16:
        raise ValueError("QDB report extract is truncated")
    magic, version, base_size, count = struct.unpack_from("<4sIII", data)
    if magic != REPORT_MAGIC or version != REPORT_EXTRACT_VERSION:
        raise ValueError("unsupported QDB report extract header")
    if base_size < REPORT_HEADER_SIZE + REPORT_COMPONENT_SIZE:
        raise ValueError(f"invalid QDB report base size: {base_size}")

    cursor = 16
    reports = []
    for _ in range(count):
        if cursor + 8 > len(data):
            raise ValueError("QDB report extract ends before a record header")
        qdb_index, record_size = struct.unpack_from("<II", data, cursor)
        cursor += 8
        end = cursor + record_size
        if end > len(data):
            raise ValueError(f"QDB report {qdb_index} is truncated")
        reports.append(_parse_report(qdb_index, data[cursor:end], base_size))
        cursor = end
    if cursor != len(data):
        raise ValueError(f"QDB report extract has {len(data) - cursor} trailing bytes")
    return reports


def _parse_catalog(source: Path | None) -> dict[int, tuple[int, str]]:
    if source is None or not source.is_file():
        return {}
    data = source.read_bytes()
    if len(data) < 8:
        raise ValueError("QDB catalog is truncated")
    size, count = struct.unpack_from("<II", data)
    if size != 850 or len(data) != 8 + size * count:
        raise ValueError("unexpected QDB type-0x80 catalog layout")
    result = {}
    for index in range(count):
        record = data[8 + index * size : 8 + (index + 1) * size]
        reference = struct.unpack_from("<I", record)[0]
        name = _cstring(record[5:])
        if name:
            result[reference] = (record[4], name)
    return result


REPORT_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    qdb_index INTEGER NOT NULL UNIQUE,
    qdb_qid INTEGER NOT NULL,
    name TEXT NOT NULL,
    report_type INTEGER,
    report_type_name TEXT,
    component_count INTEGER NOT NULL,
    record_size INTEGER NOT NULL,
    format_word_04 INTEGER NOT NULL,
    format_word_08 INTEGER NOT NULL,
    format_word_0c INTEGER NOT NULL
);
CREATE TABLE report_components (
    id INTEGER PRIMARY KEY,
    report_definition_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    component_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    report_type INTEGER NOT NULL,
    report_type_name TEXT NOT NULL,
    filter_size INTEGER NOT NULL,
    organization_code INTEGER NOT NULL,
    organization TEXT,
    header_setting_code INTEGER NOT NULL,
    header_setting TEXT,
    heading_column_code INTEGER,
    heading_column TEXT,
    rounding_code INTEGER NOT NULL,
    rounding TEXT,
    rounding_checked INTEGER,
    date_range_code INTEGER NOT NULL,
    date_range TEXT,
    custom_start_date TEXT,
    custom_end_date TEXT,
    interval_code INTEGER NOT NULL,
    interval TEXT,
    budget TEXT,
    subtotal TEXT,
    sort_by TEXT,
    transfer_mode_code INTEGER NOT NULL,
    transfer_mode TEXT,
    subcategory_mode_code INTEGER NOT NULL,
    subcategory_mode TEXT,
    status_not_cleared INTEGER,
    status_newly_cleared INTEGER,
    status_newly_reconciled INTEGER,
    status_reconciled INTEGER,
    account_filter_mode TEXT NOT NULL,
    category_filter_mode_code INTEGER,
    category_filter_mode TEXT,
    tag_filter_mode TEXT,
    UNIQUE(report_definition_id, component_index)
);
CREATE TABLE report_component_setting_words (
    id INTEGER PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES report_components(id) ON DELETE CASCADE,
    setting_offset INTEGER NOT NULL,
    value_u16 INTEGER NOT NULL,
    field_name TEXT,
    UNIQUE(component_id, setting_offset)
);
CREATE TABLE report_filter_header_words (
    id INTEGER PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES report_components(id) ON DELETE CASCADE,
    header_offset INTEGER NOT NULL,
    value_u16 INTEGER NOT NULL,
    UNIQUE(component_id, header_offset)
);
CREATE INDEX ix_report_setting_words_field
    ON report_component_setting_words(field_name);
CREATE INDEX ix_report_setting_words_value
    ON report_component_setting_words(value_u16);
CREATE TABLE report_filter_groups (
    id INTEGER PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES report_components(id) ON DELETE CASCADE,
    group_index INTEGER NOT NULL,
    semantic_kind TEXT NOT NULL,
    slot_count INTEGER NOT NULL,
    selected_count INTEGER NOT NULL,
    UNIQUE(component_id, group_index)
);
CREATE TABLE report_filter_values (
    id INTEGER PRIMARY KEY,
    filter_group_id INTEGER NOT NULL REFERENCES report_filter_groups(id) ON DELETE CASCADE,
    slot INTEGER NOT NULL,
    entity_ref INTEGER NOT NULL,
    entity_name TEXT,
    catalog_type INTEGER,
    UNIQUE(filter_group_id, slot)
);
CREATE INDEX ix_reports_name ON reports(name);
CREATE INDEX ix_report_filter_values_ref ON report_filter_values(entity_ref);
CREATE VIEW report_readable AS
SELECT
    definition.qdb_index,
    definition.qdb_qid,
    definition.name AS report_name,
    definition.report_type,
    definition.report_type_name,
    component.component_index,
    component.name AS component_name,
    component.report_type AS component_report_type,
    component.report_type_name AS component_report_type_name,
    component.organization_code,
    component.organization,
    component.header_setting_code,
    component.header_setting,
    component.heading_column_code,
    component.heading_column,
    component.rounding_code,
    component.rounding,
    component.rounding_checked,
    component.date_range_code,
    component.date_range,
    component.custom_start_date,
    component.custom_end_date,
    component.interval_code,
    component.interval,
    component.budget,
    component.subtotal,
    component.sort_by,
    component.transfer_mode_code,
    component.transfer_mode,
    component.subcategory_mode_code,
    component.subcategory_mode,
    component.status_not_cleared,
    component.status_newly_cleared,
    component.status_newly_reconciled,
    component.status_reconciled,
    component.account_filter_mode,
    component.category_filter_mode_code,
    component.category_filter_mode,
    component.tag_filter_mode,
    (
        SELECT COUNT(*)
        FROM report_filter_values AS value
        JOIN report_filter_groups AS filter_group
          ON filter_group.id = value.filter_group_id
        WHERE filter_group.component_id = component.id
          AND filter_group.semantic_kind = 'account'
    ) AS selected_account_count,
    COALESCE((
        SELECT group_concat(label, ', ')
        FROM (
            SELECT COALESCE(value.entity_name, printf('#%d', value.entity_ref)) AS label
            FROM report_filter_values AS value
            JOIN report_filter_groups AS filter_group
              ON filter_group.id = value.filter_group_id
            WHERE filter_group.component_id = component.id
              AND filter_group.semantic_kind = 'account'
            ORDER BY filter_group.group_index, value.slot
        )
    ), '') AS selected_accounts,
    (
        SELECT COUNT(*)
        FROM report_filter_values AS value
        JOIN report_filter_groups AS filter_group
          ON filter_group.id = value.filter_group_id
        WHERE filter_group.component_id = component.id
          AND filter_group.semantic_kind = 'category'
    ) AS selected_category_count,
    COALESCE((
        SELECT group_concat(label, ', ')
        FROM (
            SELECT COALESCE(value.entity_name, printf('#%d', value.entity_ref)) AS label
            FROM report_filter_values AS value
            JOIN report_filter_groups AS filter_group
              ON filter_group.id = value.filter_group_id
            WHERE filter_group.component_id = component.id
              AND filter_group.semantic_kind = 'category'
            ORDER BY filter_group.group_index, value.slot
        )
    ), '') AS selected_categories,
    (
        SELECT COUNT(*)
        FROM report_filter_values AS value
        JOIN report_filter_groups AS filter_group
          ON filter_group.id = value.filter_group_id
        WHERE filter_group.component_id = component.id
          AND filter_group.semantic_kind = 'category'
    ) AS category_filter_value_count,
    COALESCE((
        SELECT group_concat(label, ', ')
        FROM (
            SELECT COALESCE(value.entity_name, printf('#%d', value.entity_ref)) AS label
            FROM report_filter_values AS value
            JOIN report_filter_groups AS filter_group
              ON filter_group.id = value.filter_group_id
            WHERE filter_group.component_id = component.id
              AND filter_group.semantic_kind = 'category'
            ORDER BY filter_group.group_index, value.slot
        )
    ), '') AS category_filter_values
FROM reports AS definition
JOIN report_components AS component
  ON component.report_definition_id = definition.id;
"""

REPORT_SCHEMA_DROP = """
DROP VIEW IF EXISTS report_readable;
DROP TABLE IF EXISTS report_filter_values;
DROP TABLE IF EXISTS report_filter_header_words;
DROP TABLE IF EXISTS report_component_setting_words;
DROP TABLE IF EXISTS report_filter_groups;
DROP TABLE IF EXISTS report_components;
DROP TABLE IF EXISTS reports;
DROP TABLE IF EXISTS report_definitions;
DROP TABLE IF EXISTS saved_report_filter_values;
DROP TABLE IF EXISTS saved_report_filter_groups;
DROP TABLE IF EXISTS saved_report_components;
DROP TABLE IF EXISTS saved_reports;
"""


def write_qdb_reports_to_sqlite(
    source: str | Path,
    destination: str | Path,
    catalog_source: str | Path | None = None,
) -> int:
    """Replace the report tables in an existing financial SQLite export."""
    reports = parse_qdb_reports(source)
    catalog_path = Path(catalog_source) if catalog_source is not None else None
    catalog = _parse_catalog(catalog_path)
    with sqlite3.connect(destination) as connection:
        connection.executescript(REPORT_SCHEMA_DROP)
        connection.executescript(REPORT_SCHEMA)
        for report in reports:
            report_cursor = connection.execute(
                """INSERT INTO reports(
                    qdb_index, qdb_qid, name, report_type, report_type_name,
                    component_count, record_size,
                    format_word_04, format_word_08, format_word_0c
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.qdb_index,
                    report.qdb_qid,
                    report.name,
                    report.report_type,
                    (
                        report_type_name(report.report_type)
                        if report.report_type is not None
                        else None
                    ),
                    len(report.components),
                    report.declared_size,
                    report.format_word_04,
                    report.format_word_08,
                    report.format_word_0c,
                ),
            )
            for component in report.components:
                component_cursor = connection.execute(
                    """INSERT INTO report_components(
                        report_definition_id, component_index, name, report_type,
                        report_type_name, filter_size,
                        organization_code, organization,
                        header_setting_code, header_setting,
                        heading_column_code, heading_column,
                        rounding_code, rounding, rounding_checked,
                        date_range_code, date_range,
                        custom_start_date, custom_end_date,
                        interval_code, interval, budget, subtotal, sort_by,
                        transfer_mode_code, transfer_mode,
                        subcategory_mode_code, subcategory_mode,
                        status_not_cleared, status_newly_cleared, status_newly_reconciled,
                        status_reconciled,
                        account_filter_mode,
                        category_filter_mode_code, category_filter_mode,
                        tag_filter_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        report_cursor.lastrowid,
                        component.index,
                        component.name,
                        component.report_type,
                        report_type_name(component.report_type),
                        component.filter_size,
                        component.organization_code,
                        component.organization,
                        component.header_setting_code,
                        component.header_setting,
                        component.heading_column_code,
                        component.heading_column,
                        component.rounding_code,
                        component.rounding,
                        component.rounding_checked,
                        component.date_range_code,
                        component.date_range,
                        component.custom_start_date.isoformat()
                        if component.custom_start_date
                        else None,
                        component.custom_end_date.isoformat()
                        if component.custom_end_date
                        else None,
                        component.interval_code,
                        component.interval,
                        component.budget,
                        component.subtotal,
                        component.sort_by,
                        component.transfer_mode_code,
                        component.transfer_mode,
                        component.subcategory_mode_code,
                        component.subcategory_mode,
                        component.status_not_cleared,
                        component.status_newly_cleared,
                        component.status_newly_reconciled,
                        component.status_reconciled,
                        component.account_filter_mode,
                        component.category_filter_mode_code,
                        component.category_filter_mode,
                        component.tag_filter_mode,
                    ),
                )
                for setting_offset, value_u16 in component.setting_words:
                    connection.execute(
                        """INSERT INTO report_component_setting_words(
                            component_id, setting_offset, value_u16, field_name
                        ) VALUES (?, ?, ?, ?)""",
                        (
                            component_cursor.lastrowid,
                            setting_offset,
                            value_u16,
                            REPORT_SETTING_FIELD_NAMES.get(setting_offset),
                        ),
                    )
                for header_offset in range(0, len(component.report_filter.header), 2):
                    value_u16 = struct.unpack_from(
                        "<H", component.report_filter.header, header_offset
                    )[0]
                    connection.execute(
                        """INSERT INTO report_filter_header_words(
                            component_id, header_offset, value_u16
                        ) VALUES (?, ?, ?)""",
                        (component_cursor.lastrowid, header_offset, value_u16),
                    )
                for group in component.report_filter.groups:
                    values = group.values
                    group_cursor = connection.execute(
                        """INSERT INTO report_filter_groups(
                            component_id, group_index, semantic_kind, slot_count,
                            selected_count
                        ) VALUES (?, ?, ?, ?, ?)""",
                        (
                            component_cursor.lastrowid,
                            group.index,
                            group.kind,
                            group.capacity,
                            len(values),
                        ),
                    )
                    for slot, reference in values:
                        catalog_item = catalog.get(reference)
                        connection.execute(
                            """INSERT INTO report_filter_values(
                                filter_group_id, slot, entity_ref, entity_name, catalog_type
                            ) VALUES (?, ?, ?, ?, ?)""",
                            (
                                group_cursor.lastrowid,
                                slot,
                                reference,
                                catalog_item[1] if catalog_item else None,
                                catalog_item[0] if catalog_item else None,
                            ),
                        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('saved_report_count', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(len(reports)),),
        )
        connection.execute(
            "UPDATE metadata SET value = '38' WHERE key = 'schema_version' "
            "AND CAST(value AS INTEGER) < 38"
        )
    return len(reports)


def load_qdb_report_component(
    connection: sqlite3.Connection, report_name: str
) -> QdbReportComponent | None:
    """Reconstruct one decoded report component from the SQLite export."""
    try:
        row = connection.execute(
            """
            SELECT component.id, component.component_index, component.name,
                   component.report_type, component.filter_size
            FROM report_components AS component
            JOIN reports AS definition
              ON definition.id = component.report_definition_id
            WHERE component.name = ?
            ORDER BY definition.qdb_index, component.component_index
            LIMIT 1
            """,
            (report_name,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None

    component_id, component_index, name, report_type, filter_size = row
    settings = bytearray(REPORT_COMPONENT_SIZE)
    for offset, value in connection.execute(
        """
        SELECT setting_offset, value_u16
        FROM report_component_setting_words
        WHERE component_id = ?
        """,
        (component_id,),
    ):
        if 0 <= offset <= REPORT_COMPONENT_SIZE - 2:
            struct.pack_into("<H", settings, offset, value)

    header = bytearray(FILTER_HEADER_SIZE)
    for offset, value in connection.execute(
        """
        SELECT header_offset, value_u16
        FROM report_filter_header_words
        WHERE component_id = ?
        """,
        (component_id,),
    ):
        if 0 <= offset <= FILTER_HEADER_SIZE - 2:
            struct.pack_into("<H", header, offset, value)

    groups: list[QdbReportFilterGroup] = []
    for group_index, kind, capacity in connection.execute(
        """
        SELECT group_index, semantic_kind, slot_count
        FROM report_filter_groups
        WHERE component_id = ?
        ORDER BY group_index
        """,
        (component_id,),
    ):
        raw_values = [0] * capacity
        entity_names: list[tuple[int, str]] = []
        catalog_types: list[tuple[int, int | None]] = []
        for slot, reference, entity_name, catalog_type in connection.execute(
            """
            SELECT value.slot, value.entity_ref, value.entity_name, value.catalog_type
            FROM report_filter_values AS value
            JOIN report_filter_groups AS filter_group
              ON filter_group.id = value.filter_group_id
            WHERE filter_group.component_id = ?
              AND filter_group.group_index = ?
            ORDER BY value.slot
            """,
            (component_id, group_index),
        ):
            if 0 <= slot < capacity:
                raw_values[slot] = reference
                if entity_name is not None:
                    entity_names.append((reference, entity_name))
                catalog_types.append((reference, catalog_type))
        groups.append(
            QdbReportFilterGroup(
                index=group_index,
                kind=kind,
                capacity=capacity,
                raw_values=tuple(raw_values),
                entity_names=tuple(entity_names),
                catalog_types=tuple(catalog_types),
            )
        )
        if group_index < FILTER_GROUP_COUNT:
            struct.pack_into("<H", header, FILTER_COUNT_OFFSET + group_index * 2, capacity)

    filter_parts = [bytes(header)]
    filter_cursor = FILTER_HEADER_SIZE
    for group in groups:
        filter_parts.append(
            struct.pack(f"<{group.capacity}H", *group.raw_values) if group.capacity else b""
        )
        filter_cursor += group.capacity * 2
    filter_raw = b"".join(filter_parts)
    if len(filter_raw) < filter_size:
        filter_raw += bytes(filter_size - len(filter_raw))
    elif len(filter_raw) > filter_size:
        filter_raw = filter_raw[:filter_size]

    report_filter = QdbReportFilter(
        stored_size=filter_size,
        header=filter_raw[:FILTER_HEADER_SIZE],
        groups=tuple(groups),
        trailer=filter_raw[filter_cursor:],
        raw=filter_raw,
    )
    return QdbReportComponent(
        index=component_index,
        name=name,
        report_type=report_type,
        filter_size=filter_size,
        settings=bytes(settings),
        report_filter=report_filter,
    )
