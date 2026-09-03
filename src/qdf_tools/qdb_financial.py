"""Parser for the structured financial transaction family exposed by qdb.dll."""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
import struct
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .qif import QifSplit, QifTransaction

TYPE_13C = 0x13C
TAG_CATALOG_TYPE = 0x7F
TAG_RECORD_SIZE = 135
RECORD_SIZE = 961
TYPE_80 = 0x80
PENDING_TRANSACTION_TYPE = 0x8E
PENDING_PAYEE_TYPE = 0xB7
ACCOUNT_INDEX_TYPE = 0x134
ACCOUNT_INDEX_SIZE = 211
REGISTER_TRANSACTION_TYPE = 0xF7
QDB_LINK_TYPE = 0x96
STRING_MAP_NAME = "qdb-string-map.tsv"
SECURITIES_NAME = "qdb-securities.tsv"
PRICE_HISTORY_NAME = "qdb-price-history.tsv"
INVESTMENT_TRANSACTIONS_NAME = "qdb-investment-transactions.tsv"
ACCOUNT_STATUS_NAME = "qdb-account-status.tsv"
REGISTER_CLEAR_STATUS_NAME = "qdb-register-clear-status.tsv"
NATIVE_INVESTMENT_SIDECAR_FIELDS = [
    "register_ref",
    "account",
    "key",
    "security_ref",
    "shares",
    "price",
    "investment_amount",
    "transaction_amount",
    "backfill_pair",
    "is_backfill_cash",
    "transfer_qid",
    "xfer_account",
    "start_location",
    "destination_location",
    "native_cash_balance",
    "inv_txn_type",
    "inv_txn_type_name",
    "is_cash",
    "transaction_date",
]
ACCOUNT_TYPE_NAMES = {
    3: "Banking",
    4: "Credit Card",
    5: "Cash",
    # The type-0x80 catalog uses 6 for the asset family.  A separate native
    # IsHouseAccount predicate distinguishes the House subset.
    6: "Asset",
    7: "Loan",
    8: "Investing",
}
NATIVE_ACCOUNT_TYPE_NAMES = {
    0: "Banking",
    1: "Cash",
    2: "Asset",
    3: "Credit Card",
    4: "Loan",
    5: "Investing",
}
QDB_INTERNAL_ACCOUNT_NAMES = frozenset(
    {
        "Tax Impact of 401(k) Accounts",
        "Unspecified Bill Presentment Account",
    }
)


def _cstring(record: bytes, start: int, end: int) -> str | None:
    value = record[start:end].split(b"\x00", 1)[0]
    text = value.decode("cp1252", errors="replace").strip()
    return text or None


def _date_word(word: int) -> dt.date | None:
    year = (word >> 24) + 1900
    month = (word >> 16) & 0xFF
    day = (word >> 8) & 0xFF
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_qdb_type13c(source: str | Path) -> list[QifTransaction]:
    """Parse the extracted qdb type 0x13c transaction records.

    The 0x13c family is a fixed-size, API-decoded record stream.  It contains
    33,489 records in the current disposable financial QDF.  Fields not yet
    assigned a stable Quicken meaning remain in ``raw`` as exact hex.
    """
    source = Path(source)
    data = source.read_bytes()
    if len(data) < 8:
        raise ValueError("qdb extract is truncated")
    size, count = struct.unpack_from("<II", data)
    if size != RECORD_SIZE:
        raise ValueError(f"unexpected 0x13c record size: {size}")
    expected = 8 + size * count
    if len(data) != expected:
        raise ValueError(f"qdb extract length {len(data)} != expected {expected}")

    transactions: list[QifTransaction] = []
    for key in range(1, count + 1):
        record = data[8 + (key - 1) * size : 8 + key * size]
        date_word = struct.unpack_from("<I", record, 0x74)[0]
        date = _date_word(date_word) or _date_word(struct.unpack_from("<I", record, 0x78)[0])
        cents = struct.unpack_from("<q", record, 0x61)[0]
        # A leading sentinel byte precedes the fixed-width FITID string.
        fit_id = _cstring(record, 0x81, 0x184)
        payee = _cstring(record, 0x15, 0x5C)
        raw = {
            "qdb_type": f"0x{TYPE_13C:x}",
            "key": key,
            "qdb_internal_id": struct.unpack_from("<I", record, 0)[0],
            "date_word": f"0x{date_word:08x}",
            "amount_cents": cents,
            "fit_id": fit_id,
            "flags_5c": record[0x5C:0x61].hex(),
            "raw_hex": record.hex(),
        }
        transactions.append(
            QifTransaction(
                account="QDB financial transactions (0x13c)",
                section="QDB:0x13c",
                date=date,
                amount=Decimal(cents) / Decimal(100),
                payee=payee,
                number=fit_id,
                fit_id=fit_id,
                raw=raw,
            )
        )
    return transactions


def parse_qdb_tag_catalog(source: str | Path) -> dict[int, str]:
    """Read native tag names from the fixed-size type-0x7f catalog."""
    records = _read_fixed_records(Path(source), TAG_RECORD_SIZE)
    result: dict[int, str] = {}
    for record in records:
        handle = struct.unpack_from("<I", record, 0)[0]
        name = _cstring(record, 4, 68)
        if handle and name:
            result[handle] = name
    return result


def _read_fixed_records(source: Path, expected_size: int) -> list[bytes]:
    data = source.read_bytes()
    if len(data) < 8:
        raise ValueError(f"qdb extract is truncated: {source.name}")
    size, count = struct.unpack_from("<II", data)
    if size != expected_size:
        raise ValueError(f"unexpected {source.name} record size: {size}")
    expected = 8 + size * count
    if len(data) != expected:
        raise ValueError(f"{source.name} length {len(data)} != expected {expected}")
    return [data[8 + i * size : 8 + (i + 1) * size] for i in range(count)]


def parse_qdb_account_names(source: str | Path) -> dict[int, str]:
    """Read the decoded account catalog (item type 0x80)."""
    names: dict[int, str] = {}
    for record in _read_fixed_records(Path(source), 850):
        handle = struct.unpack_from("<I", record, 0)[0]
        # Type 0x80 stores a one-byte text-length marker before the name.
        name = _cstring(record, 5, len(record))
        if name:
            names[handle] = name
    return names


def parse_qdb_accounts(source: str | Path) -> dict[int, tuple[str, str | None]]:
    """Read account names and Quicken's high-level account kind from type 0x80."""
    accounts = {}
    for record in _read_fixed_records(Path(source), 850):
        handle = struct.unpack_from("<I", record, 0)[0]
        name = _cstring(record, 5, len(record))
        if name and record[4] in ACCOUNT_TYPE_NAMES:
            accounts[handle] = (name, ACCOUNT_TYPE_NAMES.get(record[4]))
    return accounts


def write_qdb_account_status_to_sqlite(
    source: str | Path,
    destination: str | Path,
    *,
    exclude_names: set[str] | None = None,
) -> int:
    """Apply native QDB account types and status flags to ``accounts``.

    The native sidecar keeps Quicken's two independent hide settings, while
    ``is_hidden`` is the analytics-friendly OR of those display settings.
    ``is_separate`` comes from QDB's separate-account status bit (the native
    accessor is unfortunately named ``ACCT_IsHidden``).  Native-listed
    accounts are inserted from the type-0x80 name catalog even when they have
    no transaction rows.
    """
    sidecar = Path(source) / ACCOUNT_STATUS_NAME
    if not sidecar.is_file():
        return 0
    updated = 0
    exclude_names = exclude_names or set()
    with (
        sqlite3.connect(destination) as connection,
        sidecar.open(encoding="utf-8-sig", newline="") as source_file,
    ):
        reader = csv.DictReader(source_file, delimiter="\t")
        legacy_expected = [
            "qdb_handle",
            "is_closed",
            "is_separate",
            "is_hidden_in_bar",
            "is_hidden_in_list",
        ]
        native_expected = [
            "qdb_handle",
            "account_type",
            "is_house",
            "is_closed",
            "is_separate",
            "is_hidden_in_bar",
            "is_hidden_in_list",
        ]
        native_subtype_expected = [
            "qdb_handle",
            "account_type",
            "account_subtype",
            "is_closed",
            "is_separate",
            "is_hidden_in_bar",
            "is_hidden_in_list",
        ]
        native_legacy_expected = [
            "qdb_handle",
            "account_type",
            "is_closed",
            "is_separate",
            "is_hidden_in_bar",
            "is_hidden_in_list",
        ]
        if reader.fieldnames not in (
            legacy_expected,
            native_legacy_expected,
            native_expected,
            native_subtype_expected,
        ):
            raise ValueError(f"unexpected {ACCOUNT_STATUS_NAME} header: {reader.fieldnames}")
        has_native_type = reader.fieldnames in (
            native_legacy_expected,
            native_expected,
            native_subtype_expected,
        )
        has_house_predicate = reader.fieldnames == native_expected
        has_native_subtype = reader.fieldnames == native_subtype_expected
        catalog_names = (
            parse_qdb_account_names(Path(source) / "qdb-type-080.bin")
            if has_native_type and (Path(source) / "qdb-type-080.bin").is_file()
            else {}
        )
        for row in reader:
            handle = int(row["qdb_handle"])
            account_type = None
            if has_native_type:
                native_code = int(row["account_type"])
                account_type = NATIVE_ACCOUNT_TYPE_NAMES.get(native_code, f"QDB:{native_code}")
                if has_native_subtype:
                    account_subtype = int(row["account_subtype"])
                    if not 0 <= account_subtype <= 255:
                        raise ValueError(f"invalid account subtype for handle {handle}")
                    if native_code == 2:
                        if account_subtype == 3:
                            account_type = "House"
                        elif account_subtype == 5:
                            account_type = "Vehicle"
                elif has_house_predicate and int(row["is_house"]) not in (0, 1):
                    raise ValueError(f"invalid house predicate for handle {handle}")
                elif has_house_predicate and native_code == 2 and int(row["is_house"]):
                    account_type = "House"
            closed = int(row["is_closed"])
            separate = int(row["is_separate"])
            hidden_bar = int(row["is_hidden_in_bar"])
            hidden_list = int(row["is_hidden_in_list"])
            if any(value not in (0, 1) for value in (closed, separate, hidden_bar, hidden_list)):
                raise ValueError(f"invalid account status for handle {handle}")
            if account_type is not None and handle in catalog_names:
                if catalog_names[handle] in exclude_names:
                    continue
                connection.execute(
                    "INSERT INTO accounts(name, account_type, qdb_handle) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "account_type = excluded.account_type, "
                    "qdb_handle = COALESCE(accounts.qdb_handle, excluded.qdb_handle)",
                    (catalog_names[handle], account_type, handle),
                )
            cursor = connection.execute(
                "UPDATE accounts SET "
                "account_type = COALESCE(?, account_type), "
                "is_closed = ?, is_separate = ?, is_hidden = ?, "
                "is_hidden_in_bar = ?, is_hidden_in_list = ? WHERE qdb_handle = ?",
                (
                    account_type,
                    closed,
                    separate,
                    int(bool(hidden_bar or hidden_list)),
                    hidden_bar,
                    hidden_list,
                    handle,
                ),
            )
            updated += cursor.rowcount
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("account_status_source", "qdb-access-native-status-bits"),
        )
        if has_native_type:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("account_type_source", "qdb-access-native-account-type"),
            )
        if has_house_predicate:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("account_house_source", "qdb-access-native-is-house-account"),
            )
        if has_native_subtype:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("account_subtype_source", "qdb-access-native-account-subtype"),
            )
    return updated


def _read_account_index(source: Path) -> dict[int, int]:
    data = source.read_bytes()
    if len(data) < 16:
        raise ValueError("account index extract is truncated")
    magic, version, size, count = struct.unpack_from("<4sIII", data)
    if magic != b"QATM" or version != 1 or size != ACCOUNT_INDEX_SIZE:
        raise ValueError("unexpected account index header")
    expected = 16 + count * (8 + size)
    if len(data) != expected:
        raise ValueError(f"account index length {len(data)} != expected {expected}")
    result: dict[int, int] = {}
    for index in range(count):
        offset = 16 + index * (8 + size)
        handle, _ = struct.unpack_from("<II", data, offset)
        internal_id = struct.unpack_from("<I", data, offset + 8)[0]
        if internal_id in result and result[internal_id] != handle:
            raise ValueError(f"duplicate account index key {internal_id}")
        result[internal_id] = handle
    return result


def parse_qdb_string_map(source: str | Path) -> dict[int, str]:
    """Read the reversible QDB string-pool sidecar emitted by the extractor."""
    result: dict[int, str] = {}
    path = Path(source)
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        key, separator, text = line.partition("\t")
        if not separator:
            continue
        try:
            result[int(key)] = text
        except ValueError:
            continue
    return result


def _qdb_memo_overrides(
    directory: Path, strings: dict[int, str], transactions: list[QifTransaction]
) -> dict[str, str]:
    """Read memo string references from legacy 0x86 transaction rows.

    The first dword in a legacy row is overloaded: for rows with a memo it is
    a string-pool ID, while rows without a memo use the transaction's internal
    ID.  Because those integer spaces overlap, a blind string lookup can turn
    an internal ID into an unrelated memo.  Require the reference to differ
    from the canonical transaction ID before accepting it as memo text.
    """
    source = directory / "qdb-type-086.bin"
    if not source.is_file():
        return {}
    strings = parse_qdb_string_map(directory / STRING_MAP_NAME)
    internal_by_fit = {
        transaction.number: int(transaction.raw["qdb_internal_id"])
        for transaction in transactions
        if transaction.number
    }
    result: dict[str, str] = {}
    for record in _read_fixed_records(source, RECORD_SIZE):
        fit_id = _cstring(record, 0x81, 0x184)
        reference = struct.unpack_from("<I", record, 0)[0]
        if not fit_id or reference == internal_by_fit.get(fit_id):
            continue
        memo = strings.get(reference)
        if memo and fit_id:
            result[fit_id] = memo
    return result


def _qdb_native_memo_overrides(
    directory: Path, transactions: list[QifTransaction]
) -> dict[str, str]:
    """Join memo text returned by Quicken's Transaction API to 0x13c rows.

    The native API is called against account-register (0x0f7) records, whose
    internal IDs are also present in the legacy 0x086 stream.  FITID is the
    stable bridge from that stream to the canonical 0x13c transaction.  This
    avoids guessing from overlapping string-pool integer IDs and preserves
    the exact displayed memo text (including capitalization and punctuation).
    """
    source = directory / "qdb-register-memo.tsv"
    legacy_path = directory / "qdb-type-086.bin"
    if not source.is_file() or not legacy_path.is_file():
        return {}
    refs_by_fit: dict[str, list[int]] = {}
    for record in _read_fixed_records(legacy_path, RECORD_SIZE):
        fit_id = _cstring(record, 0x81, 0x184)
        if fit_id:
            refs_by_fit.setdefault(fit_id, []).append(struct.unpack_from("<I", record, 0)[0])
    native_by_ref: dict[int, str] = {}
    for line in source.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        try:
            register_ref = int(fields[0])
        except ValueError:
            continue
        memo = fields[2].strip()
        if memo:
            native_by_ref[register_ref] = memo
    result: dict[str, str] = {}
    register_by_canonical: dict[int, int] = {}
    link_path = directory / "qdb-type-096.bin"
    if link_path.is_file():
        groups: dict[bytes, dict[int, int]] = {}
        for record in _read_fixed_records(link_path, 50):
            item_type = struct.unpack_from("<H", record, 8)[0]
            if item_type in (ACCOUNT_INDEX_TYPE, REGISTER_TRANSACTION_TYPE):
                groups.setdefault(record[0x22:0x32], {})[item_type] = struct.unpack_from(
                    "<I", record, 4
                )[0]
        for linked in groups.values():
            canonical_id = linked.get(ACCOUNT_INDEX_TYPE)
            register_id = linked.get(REGISTER_TRANSACTION_TYPE)
            if canonical_id is not None and register_id is not None:
                register_by_canonical[canonical_id] = register_id
    for transaction in transactions:
        fit_id = transaction.number
        if not fit_id:
            continue
        refs = refs_by_fit.get(fit_id, [])
        if not refs:
            linked_register = register_by_canonical.get(int(transaction.raw["qdb_internal_id"]))
            if linked_register is not None:
                refs = [linked_register]
        memos = {native_by_ref[ref] for ref in refs if ref in native_by_ref}
        if len(memos) == 1:
            result[fit_id] = next(iter(memos))
    return result


def _qdb_native_split_overrides(
    directory: Path,
    transactions: list[QifTransaction],
    catalog_names: dict[int, str],
    account_catalog: dict[int, tuple[str, str | None]],
    *,
    include_unsplit: bool = False,
) -> tuple[dict[str, list[QifSplit]], list[QifTransaction]]:
    """Read split lines returned by qaccess's native Transaction object."""
    source = directory / "qdb-register-splits.tsv"
    legacy_path = directory / "qdb-type-086.bin"
    if not source.is_file():
        return {}, transactions if include_unsplit else []
    strings = parse_qdb_string_map(directory / STRING_MAP_NAME)
    by_register: dict[int, list[QifSplit]] = {}
    for line in source.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) < 9:
            continue
        try:
            register_ref = int(fields[0])
            split_index = int(fields[1])
            category_handle = int(fields[2])
            transfer_handle = int(fields[3])
            memo_ref = int(fields[4])
            amount_cents = int(fields[5])
        except ValueError:
            continue
        category_name = catalog_names.get(category_handle) if category_handle else None
        # GetSplit returns two handle-sized references.  The first reference is
        # the split's category-or-transfer value.  The second reference is also
        # populated on ordinary categorized splits in the observed QDF (for
        # example, the Springfield Food Co-op split has category handles for
        # Groceries and Gifts & Donations:Gift but a nonzero second handle).
        # Treating that second reference as authoritative changes valid
        # categories into unrelated transfer accounts.  Use it only when the
        # primary reference is absent or cannot resolve to a category/account.
        if category_handle in account_catalog:
            category = f"[{catalog_names.get(category_handle, str(category_handle))}]"
        elif category_name is not None:
            category = category_name
        elif transfer_handle:
            category = f"[{catalog_names.get(transfer_handle, str(transfer_handle))}]"
        else:
            category = None
        memo = None
        if memo_ref not in (0, 0xFFFFFFFF):
            memo = strings.get(memo_ref)
            if memo is None:
                memo = catalog_names.get(memo_ref)
        by_register.setdefault(register_ref, []).append(
            QifSplit(
                category=category,
                memo=memo,
                amount=Decimal(amount_cents) / Decimal(100),
                raw={
                    "qdb_type": "native transaction split",
                    "qdb_register_ref": register_ref,
                    "qdb_split_index": split_index,
                    "qdb_category_handle": category_handle,
                    "qdb_transfer_handle": transfer_handle,
                    "qdb_memo_ref": memo_ref,
                    "raw_hex": fields[6],
                },
            )
        )
    if not by_register and not include_unsplit:
        return {}, []

    refs_by_fit: dict[str, list[int]] = {}
    if legacy_path.is_file():
        for record in _read_fixed_records(legacy_path, RECORD_SIZE):
            fit_id = _cstring(record, 0x81, 0x184)
            if fit_id:
                refs_by_fit.setdefault(fit_id, []).append(struct.unpack_from("<I", record, 0)[0])

    register_by_canonical: dict[int, int] = {}
    link_path = directory / "qdb-type-096.bin"
    if link_path.is_file():
        groups: dict[bytes, dict[int, int]] = {}
        for record in _read_fixed_records(link_path, 50):
            item_type = struct.unpack_from("<H", record, 8)[0]
            if item_type in (ACCOUNT_INDEX_TYPE, REGISTER_TRANSACTION_TYPE):
                groups.setdefault(record[0x22:0x32], {})[item_type] = struct.unpack_from(
                    "<I", record, 4
                )[0]
        for linked in groups.values():
            canonical_id = linked.get(ACCOUNT_INDEX_TYPE)
            register_id = linked.get(REGISTER_TRANSACTION_TYPE)
            if canonical_id is not None and register_id is not None:
                register_by_canonical[canonical_id] = register_id

    result: dict[str, list[QifSplit]] = {}
    used_register_refs: set[int] = set()
    for transaction in transactions:
        fit_id = transaction.number
        if not fit_id:
            continue
        refs = list(refs_by_fit.get(fit_id, []))
        linked_register = register_by_canonical.get(int(transaction.raw["qdb_internal_id"]))
        if linked_register is not None and linked_register not in refs:
            refs.append(linked_register)
        candidates = [by_register[ref] for ref in refs if ref in by_register]
        if not candidates:
            continue
        # A transaction should have one register representation.  If legacy
        # and identity joins both find it, prefer the first identical set.
        result[fit_id] = candidates[0]
        used_register_refs.update(ref for ref in refs if ref in by_register)

    native_memos: dict[int, str] = {}
    clear_status_by_register = _read_qdb_register_clear_status(directory)
    native_check_numbers = _read_qdb_register_check_numbers(directory)
    memo_path = directory / "qdb-register-memo.tsv"
    if memo_path.is_file():
        for line in memo_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) >= 3 and fields[2].strip():
                try:
                    native_memos[int(fields[0])] = fields[2].strip()
                except ValueError:
                    continue

    register_only: list[QifTransaction] = []
    register_path = directory / "qdb-type-0f7.bin"
    fit_ids_by_register = _read_qdb_register_fit_ids(directory)
    if register_path.is_file():
        for record in _read_fixed_records(register_path, ACCOUNT_INDEX_SIZE):
            register_ref = struct.unpack_from("<I", record, 0)[0]
            if (
                not include_unsplit and register_ref not in by_register
            ) or register_ref in used_register_refs:
                continue
            try:
                register_date = dt.date(record[8] + 1900, record[7], record[6])
            except ValueError:
                continue
            account_handle = struct.unpack_from("<H", record, 4)[0]
            payee = strings.get(struct.unpack_from("<I", record, 0x63)[0])
            amount_cents = struct.unpack_from("<q", record, 0x24)[0]
            category_handle = struct.unpack_from("<I", record, 0x4E)[0]
            category = catalog_names.get(category_handle)
            if category_handle in account_catalog:
                category = f"[{catalog_names.get(category_handle, str(category_handle))}]"
            fit_ids = fit_ids_by_register.get(register_ref, set())
            fit_id = next(iter(fit_ids)) if len(fit_ids) == 1 else None
            register_only.append(
                QifTransaction(
                    account=catalog_names.get(
                        account_handle, f"QDB account handle {account_handle}"
                    ),
                    account_type=(account_catalog.get(account_handle) or (None, None))[1],
                    section="QDB:0xf7",
                    date=register_date,
                    amount=Decimal(amount_cents) / Decimal(100),
                    payee=payee,
                    memo=native_memos.get(register_ref),
                    cleared=_decode_qdb_clear_status(clear_status_by_register.get(register_ref)),
                    number=native_check_numbers.get(register_ref),
                    fit_id=fit_id,
                    category="--Split--" if register_ref in by_register else category,
                    splits=by_register.get(register_ref, []),
                    raw={
                        "qdb_type": "0xf7 register split",
                        "qdb_register_ref": register_ref,
                        "qdb_account_handle": account_handle,
                        "fit_id": fit_id,
                        "check_number": native_check_numbers.get(register_ref),
                        "qdb_clear_status": clear_status_by_register.get(register_ref),
                        "qdb_split_count": len(by_register.get(register_ref, [])),
                        "raw_hex": record.hex(),
                    },
                )
            )
    return result, register_only


def parse_qdb_register_transactions(source: str | Path) -> list[QifTransaction]:
    """Parse the complete native account-register transaction stream."""
    directory = Path(source)
    names_path = directory / "qdb-type-080.bin"
    if not names_path.is_file():
        return []
    catalog_names = parse_qdb_account_names(names_path)
    account_catalog = parse_qdb_accounts(names_path)
    _, transactions = _qdb_native_split_overrides(
        directory,
        [],
        catalog_names,
        account_catalog,
        include_unsplit=True,
    )
    transactions = [
        transaction
        for transaction in transactions
        if transaction.raw.get("qdb_account_handle") not in (None, 0)
    ]
    investment_rows = _read_qdb_investment_register_rows(directory)
    for transaction in transactions:
        register_ref = transaction.raw.get("qdb_register_ref")
        row = investment_rows.get(register_ref)
        if row is None or int(row["security_ref"]) == 0:
            continue
        transaction.security = row["security_name"]
        transaction.price = Decimal(row["price"])
        transaction.quantity = abs(Decimal(row["shares"]))
        transaction.amount = abs(Decimal(row["transaction_amount"]) / Decimal(100))
        transaction.raw["qdb_investment_action"] = row["action"]
        transaction.category = None

    return transactions


def _read_qdb_investment_register_rows(directory: Path) -> dict[int, dict[str, str]]:
    path = directory / INVESTMENT_TRANSACTIONS_NAME
    securities_path = directory / SECURITIES_NAME
    if not path.is_file() or not securities_path.is_file():
        return {}
    securities: dict[int, str] = {}
    with securities_path.open(encoding="utf-8-sig", newline="") as source_file:
        for row in csv.DictReader(source_file, delimiter="\t"):
            securities[int(row["qdb_security_ref"])] = row["name"]
    result: dict[int, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as source_file:
        for row in csv.DictReader(source_file, delimiter="\t"):
            security_ref = int(row["security_ref"])
            if security_ref not in securities:
                continue
            result[int(row["register_ref"])] = {
                **row,
                "security_name": securities[security_ref],
                "action": row["inv_txn_type_name"].strip(),
            }
    return result


def write_qdb_tag_catalog_to_sqlite(source: str | Path, destination: str | Path) -> int:
    """Materialize the native type-0x7f tag catalog."""
    path = Path(source) / "qdb-type-07f.bin"
    if not path.is_file():
        return 0
    tags = parse_qdb_tag_catalog(path)
    with sqlite3.connect(destination) as connection:
        connection.executemany(
            "INSERT INTO tags(name, qdb_handle) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET qdb_handle = COALESCE(tags.qdb_handle, excluded.qdb_handle)",
            ((name, handle) for handle, name in tags.items()),
        )
        from .sqlite_export import _materialize_tags

        _materialize_tags(connection)
    return len(tags)


def write_qdb_category_catalog_to_sqlite(source: str | Path, destination: str | Path) -> int:
    """Add native categories that have no transaction in the export window.

    QIF carries a standalone ``!Type:Cat`` catalog.  The equivalent QDF
    catalog is stored in type 0x80 beside account names.  ``:ZZZZZ`` entries
    are Quicken's internal category-group sentinels and are not emitted by a
    QIF export, so they are intentionally excluded.
    """
    directory = Path(source)
    names_path = directory / "qdb-type-080.bin"
    if not names_path.is_file():
        return 0
    account_handles = set(parse_qdb_accounts(names_path))
    category_rows = sorted(
        (
            qdb_handle,
            name,
        )
        for qdb_handle, name in parse_qdb_account_names(names_path).items()
        if (qdb_handle not in account_handles or name == "Cash") and not name.endswith(":ZZZZZ")
    )
    with sqlite3.connect(destination) as connection:
        connection.executemany(
            "INSERT INTO categories(name, qdb_handle) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "qdb_handle = COALESCE(categories.qdb_handle, excluded.qdb_handle)",
            ((name, qdb_handle) for qdb_handle, name in category_rows),
        )
        from .sqlite_export import _materialize_categories

        _materialize_categories(connection)
    return len(category_rows)


def _qdb_register_memo_candidates(
    directory: Path,
    strings: dict[int, str],
    transactions: list[QifTransaction],
    account_handles: dict[int, int],
) -> dict[str, str]:
    """Recover memo text held by decoded register (0x0f7) records.

    The register family is the only decoded family that carries Quicken's
    displayed memo reference.  Its rows are dated on the register date rather
    than the posted date, so they are joined conservatively by account,
    amount, displayed payee, and a short date window.  Rows whose primary slot
    is not a live QDB string are deliberately ignored; those values are
    internal references, not memo text.
    """
    source = directory / "qdb-type-0f7.bin"
    if not source.is_file():
        return {}
    register_rows = _read_fixed_records(source, ACCOUNT_INDEX_SIZE)
    transaction_internal_ids = {
        int(transaction.raw["qdb_internal_id"]) for transaction in transactions
    }
    candidates: dict[tuple[int | None, Decimal, str, dt.date], list[str]] = {}
    for record in register_rows:
        reference = struct.unpack_from("<I", record, 0)[0]
        if reference in transaction_internal_ids:
            continue
        memo = strings.get(reference)
        if not memo:
            continue
        display_id = struct.unpack_from("<I", record, 0x63)[0]
        display_payee = strings.get(display_id)
        if not display_payee:
            continue
        try:
            register_date = dt.date(record[8] + 1900, record[7], record[6])
        except ValueError:
            continue
        handle = struct.unpack_from("<H", record, 4)[0]
        amount = Decimal(struct.unpack_from("<q", record, 0x24)[0]) / Decimal(100)
        candidates.setdefault((handle, amount, display_payee, register_date), []).append(memo)

    result: dict[str, str] = {}
    for transaction in transactions:
        if transaction.date is None or not transaction.payee:
            continue
        internal_id = int(transaction.raw["qdb_internal_id"])
        handle = account_handles.get(internal_id)
        matches: set[str] = set()
        for (
            candidate_handle,
            amount,
            payee,
            register_date,
        ), memos in candidates.items():
            if (
                candidate_handle != handle
                or amount != transaction.amount
                or payee != transaction.payee
            ):
                continue
            if dt.timedelta(0) <= transaction.date - register_date <= dt.timedelta(days=7):
                matches.update(memos)
        if len(matches) == 1:
            result[transaction.number or f"qdb:{internal_id}"] = next(iter(matches))
    return result


def _qdb_category(
    record: bytes, strings: dict[int, str]
) -> tuple[str | None, int | None, str | None]:
    """Resolve the category/transfer marker from decoded record string slots.

    Quicken's category strings are marked with a leading ``=`` in the string
    pool.  Newer records use offset 0xa0; older/migrated rows can carry the
    same marker in the primary slot at offset zero.  A bracketed marker is a
    transfer account and is intentionally preserved in ``category`` so the
    SQLite writer can split it into ``transfer_account``.
    """
    for offset in (0xA0, 0):
        if offset + 4 > len(record):
            continue
        string_id = struct.unpack_from("<I", record, offset)[0]
        text = strings.get(string_id)
        if not text or not (text.startswith("=") or (text.startswith("[") and text.endswith("]"))):
            continue
        value = text.removeprefix("=")
        return value or None, string_id, text
    return None, None, None


def _pending_payee_overrides(
    directory: Path, strings: dict[int, str]
) -> dict[tuple[dt.date | None, Decimal, str | None, str | None], str]:
    """Return user-normalized payees for the current downloaded transactions.

    Type 0x08e contains the small current-download transaction set in the same
    961-byte layout as 0x13c.  Its ordinally paired 0x0b7 record stores the
    user-facing payee string ID at 0x63 and the downloaded payee string ID at
    0x67.  The full transaction signature joins this set back to 0x13c without
    relying on date/payee alone.
    """
    transaction_path = directory / "qdb-type-08e.bin"
    payee_path = directory / "qdb-type-0b7.bin"
    if not transaction_path.is_file() or not payee_path.is_file():
        return {}
    pending = _read_fixed_records(transaction_path, RECORD_SIZE)
    payees = _read_fixed_records(payee_path, ACCOUNT_INDEX_SIZE)
    if len(pending) != len(payees):
        return {}

    result = {}
    for transaction_record, payee_record in zip(pending, payees, strict=True):
        normalized_id = struct.unpack_from("<I", payee_record, 0x63)[0]
        downloaded_id = struct.unpack_from("<I", payee_record, 0x67)[0]
        normalized = strings.get(normalized_id)
        downloaded = strings.get(downloaded_id) or _cstring(transaction_record, 0x15, 0x5C)
        if not normalized or not downloaded:
            continue
        date_word = struct.unpack_from("<I", transaction_record, 0x74)[0]
        date = _date_word(date_word) or _date_word(
            struct.unpack_from("<I", transaction_record, 0x78)[0]
        )
        amount = Decimal(struct.unpack_from("<q", transaction_record, 0x61)[0]) / Decimal(100)
        fit_id = _cstring(transaction_record, 0x81, 0x184)
        result[(date, amount, fit_id, downloaded)] = normalized
    return result


def _pending_category_overrides(
    directory: Path,
    catalog_names: dict[int, str],
    account_catalog: dict[int, tuple[str, str | None]],
) -> dict[tuple[dt.date | None, Decimal, str | None, str | None], str]:
    transaction_path = directory / "qdb-type-08e.bin"
    payee_path = directory / "qdb-type-0b7.bin"
    if not transaction_path.is_file() or not payee_path.is_file():
        return {}
    pending = _read_fixed_records(transaction_path, RECORD_SIZE)
    payees = _read_fixed_records(payee_path, ACCOUNT_INDEX_SIZE)
    result: dict[tuple[dt.date | None, Decimal, str | None, str | None], str] = {}
    for transaction_record, payee_record in zip(pending, payees, strict=False):
        handle = struct.unpack_from("<I", payee_record, 0x4E)[0]
        category = catalog_names.get(handle)
        if not category or handle in account_catalog:
            continue
        date_word = struct.unpack_from("<I", transaction_record, 0x74)[0]
        date = _date_word(date_word) or _date_word(
            struct.unpack_from("<I", transaction_record, 0x78)[0]
        )
        amount = Decimal(struct.unpack_from("<q", transaction_record, 0x61)[0]) / Decimal(100)
        fit_id = _cstring(transaction_record, 0x81, 0x184)
        downloaded = _cstring(transaction_record, 0x15, 0x5C)
        result[(date, amount, fit_id, downloaded)] = category
    return result


def _display_payee_overrides(
    directory: Path,
    strings: dict[int, str],
    transactions: list[QifTransaction],
    account_handles: dict[int, int],
) -> dict[int, str]:
    """Map canonical internal IDs to Quicken's register/display payees.

    Type 0x96 supplies the object identity shared by the canonical 0x134 row
    and its 0x0f7 register row.  The latter's string slot at 0x63 is the payee
    Quicken displays after automatic or manual renaming.
    """
    link_path = directory / "qdb-type-096.bin"
    register_path = directory / "qdb-type-0f7.bin"
    if not link_path.is_file() or not register_path.is_file():
        return {}

    groups: dict[bytes, dict[int, int]] = {}
    for record in _read_fixed_records(link_path, 50):
        item_type = struct.unpack_from("<H", record, 8)[0]
        if item_type not in (ACCOUNT_INDEX_TYPE, REGISTER_TRANSACTION_TYPE):
            continue
        identity = record[0x22:0x32]
        if identity == bytes(16):
            continue
        groups.setdefault(identity, {})[item_type] = struct.unpack_from("<I", record, 4)[0]

    register_by_internal = {
        struct.unpack_from("<I", record, 0)[0]: record
        for record in _read_fixed_records(register_path, ACCOUNT_INDEX_SIZE)
    }
    result: dict[int, str] = {}
    for linked in groups.values():
        canonical_id = linked.get(ACCOUNT_INDEX_TYPE)
        register_id = linked.get(REGISTER_TRANSACTION_TYPE)
        register = register_by_internal.get(register_id) if register_id is not None else None
        if canonical_id is None or register is None:
            continue
        payee = strings.get(struct.unpack_from("<I", register, 0x63)[0])
        if payee:
            result[canonical_id] = payee

    # A small number of newly downloaded rows have not yet acquired a shared
    # 0x96 identity.  Accept only a unique register candidate with the same
    # account and amount whose register date precedes the posted date by no
    # more than seven days.
    register_candidates: dict[tuple[int, Decimal], list[tuple[dt.date, str]]] = {}
    for register in register_by_internal.values():
        payee = strings.get(struct.unpack_from("<I", register, 0x63)[0])
        try:
            date = dt.date(register[8] + 1900, register[7], register[6])
        except ValueError:
            continue
        if not payee:
            continue
        handle = struct.unpack_from("<H", register, 4)[0]
        amount = Decimal(struct.unpack_from("<q", register, 0x24)[0]) / Decimal(100)
        register_candidates.setdefault((handle, amount), []).append((date, payee))

    for transaction in transactions:
        internal_id = int(transaction.raw["qdb_internal_id"])
        if internal_id in result or transaction.date is None:
            continue
        handle = account_handles.get(internal_id)
        candidates = [
            payee
            for date, payee in register_candidates.get((handle, transaction.amount), [])
            if dt.timedelta(0) <= transaction.date - date <= dt.timedelta(days=7)
        ]
        if len(candidates) == 1:
            result[internal_id] = candidates[0]
    return result


def _register_category_overrides(
    directory: Path,
    catalog_names: dict[int, str],
    account_catalog: dict[int, tuple[str, str | None]],
    strings: dict[int, str],
    transactions: list[QifTransaction],
    display_payees: dict[int, str],
    account_handles: dict[int, int],
) -> dict[int, str]:
    """Resolve categories and transfers from native register rows.

    The canonical 0x13c rows carry the stable FITID, while the native register
    row carries the category or transfer handle at offset 0x4e.  Prefer the
    legacy 0x086 FITID bridge when available, then use the 0x096 identity link
    and the constrained date/amount/payee fallback for rows without a FITID
    bridge.
    """
    link_path = directory / "qdb-type-096.bin"
    register_path = directory / "qdb-type-0f7.bin"
    legacy_path = directory / "qdb-type-086.bin"
    if not register_path.is_file():
        return {}
    register_by_internal = {
        struct.unpack_from("<I", record, 0)[0]: record
        for record in _read_fixed_records(register_path, ACCOUNT_INDEX_SIZE)
    }

    def register_category(record: bytes) -> str | None:
        handle = struct.unpack_from("<I", record, 0x4E)[0]
        name = catalog_names.get(handle)
        if not name:
            return None
        if handle in account_catalog:
            return f"[{name}]"
        return name

    result: dict[int, str] = {}

    if link_path.is_file():
        groups: dict[bytes, dict[int, int]] = {}
        for record in _read_fixed_records(link_path, 50):
            item_type = struct.unpack_from("<H", record, 8)[0]
            if item_type in (ACCOUNT_INDEX_TYPE, REGISTER_TRANSACTION_TYPE):
                groups.setdefault(record[0x22:0x32], {})[item_type] = struct.unpack_from(
                    "<I", record, 4
                )[0]
        for linked in groups.values():
            canonical_id = linked.get(ACCOUNT_INDEX_TYPE)
            register_id = linked.get(REGISTER_TRANSACTION_TYPE)
            register = register_by_internal.get(register_id) if register_id is not None else None
            if canonical_id is None or register is None:
                continue
            category = register_category(register)
            if category:
                result[canonical_id] = category

    # The 0x086 rows carry the same FITID as 0x13c and their first dword is the
    # register reference used by the 0xf7 extraction.  This is the stable
    # bridge for the many canonical rows that do not have a 0x096 identity.
    if legacy_path.is_file():
        refs_by_fit: dict[str, list[int]] = {}
        for record in _read_fixed_records(legacy_path, RECORD_SIZE):
            fit_id = _cstring(record, 0x81, 0x184)
            if fit_id:
                refs_by_fit.setdefault(fit_id, []).append(struct.unpack_from("<I", record, 0)[0])
        for transaction in transactions:
            internal_id = int(transaction.raw["qdb_internal_id"])
            if internal_id in result:
                continue
            fit_id = transaction.fit_id or transaction.number
            values = {
                register_category(register_by_internal[register_ref])
                for register_ref in refs_by_fit.get(fit_id or "", [])
                if register_ref in register_by_internal
            }
            values.discard(None)
            if len(values) == 1:
                result[internal_id] = next(iter(values))

    candidates: dict[tuple[int, Decimal, str], list[tuple[dt.date, str]]] = {}
    for register in register_by_internal.values():
        payee = strings.get(struct.unpack_from("<I", register, 0x63)[0], "")
        if not payee:
            continue
        try:
            date = dt.date(register[8] + 1900, register[7], register[6])
        except ValueError:
            continue
        handle = struct.unpack_from("<H", register, 4)[0]
        amount = Decimal(struct.unpack_from("<q", register, 0x24)[0]) / Decimal(100)
        category = register_category(register)
        if category:
            candidates.setdefault((handle, amount, payee), []).append((date, category))
    for transaction in transactions:
        internal_id = int(transaction.raw["qdb_internal_id"])
        if internal_id in result or transaction.date is None:
            continue
        payee = display_payees.get(internal_id) or transaction.payee or ""
        matches = [
            category
            for date, category in candidates.get(
                (account_handles.get(internal_id), transaction.amount, payee), []
            )
            if dt.timedelta(0) <= transaction.date - date <= dt.timedelta(days=7)
        ]
        if matches and len(set(matches)) == 1:
            result[internal_id] = matches[0]
    return result


def parse_qdb_financial_extract(source: str | Path) -> list[QifTransaction]:
    """Parse 0x13c transactions and join their complete account index.

    ``source`` is the Windows extractor directory containing 0x13c, 0x134,
    and 0x80 files.  The index has one row per canonical transaction, so this
    join does not rely on payee or date heuristics.
    """
    directory = Path(source)
    transaction_source = directory / "qdb-type-13c.bin"
    transactions = parse_qdb_type13c(transaction_source)
    transaction_bytes = transaction_source.read_bytes()
    account_catalog = parse_qdb_accounts(directory / "qdb-type-080.bin")
    catalog_names = parse_qdb_account_names(directory / "qdb-type-080.bin")
    account_names = {handle: item[0] for handle, item in account_catalog.items()}
    by_internal = _read_account_index(directory / "qdb-account-map-134.bin")
    index_records = _read_fixed_records(directory / "qdb-type-134.bin", ACCOUNT_INDEX_SIZE)
    strings = parse_qdb_string_map(directory / STRING_MAP_NAME)
    payee_overrides = _pending_payee_overrides(directory, strings)
    pending_categories = _pending_category_overrides(directory, catalog_names, account_catalog)
    memo_overrides = _qdb_memo_overrides(directory, strings, transactions)
    native_memo_overrides = _qdb_native_memo_overrides(directory, transactions)
    native_split_overrides, register_split_transactions = _qdb_native_split_overrides(
        directory, transactions, catalog_names, account_catalog
    )
    register_memo_candidates = _qdb_register_memo_candidates(
        directory, strings, transactions, by_internal
    )
    display_payees = _display_payee_overrides(directory, strings, transactions, by_internal)
    register_categories = _register_category_overrides(
        directory,
        catalog_names,
        account_catalog,
        strings,
        transactions,
        display_payees,
        by_internal,
    )
    for transaction in transactions:
        record_offset = 8 + (int(transaction.raw["key"]) - 1) * RECORD_SIZE
        record = transaction_bytes[record_offset : record_offset + RECORD_SIZE]
        index_record = index_records[int(transaction.raw["key"]) - 1]
        category, category_id, category_text = _qdb_category(record, strings)
        category_handle = struct.unpack_from("<I", index_record, 0x4E)[0]
        category_name = catalog_names.get(category_handle)
        if category_name and category_handle not in account_catalog:
            category = category_name
            category_id = category_handle
            category_text = category
        register_category = register_categories.get(int(transaction.raw["qdb_internal_id"]))
        if register_category:
            category = register_category
            category_id = None
            category_text = category
        pending_category = pending_categories.get(
            (
                transaction.date,
                transaction.amount,
                transaction.number,
                transaction.downloaded_payee or transaction.payee,
            )
        )
        if pending_category:
            category = pending_category
            category_id = None
            category_text = category
        memo = native_memo_overrides.get(transaction.number or "")
        if not memo:
            memo = memo_overrides.get(transaction.number or "")
        if not memo:
            memo = register_memo_candidates.get(
                transaction.number or f"qdb:{int(transaction.raw['qdb_internal_id'])}"
            )
        if memo:
            transaction.memo = memo
        if category:
            transaction.category = category
            transaction.raw["qdb_category_string_id"] = category_id
            transaction.raw["qdb_category_string"] = category_text
        splits = native_split_overrides.get(transaction.number or "")
        if splits:
            transaction.splits = splits
            transaction.category = "--Split--"
            transaction.raw["qdb_split_count"] = len(splits)
        payee_signature = (
            transaction.date,
            transaction.amount,
            transaction.number,
            transaction.payee,
        )
        internal_id = int(transaction.raw["qdb_internal_id"])
        normalized_payee = display_payees.get(internal_id) or payee_overrides.get(payee_signature)
        if normalized_payee and normalized_payee != transaction.payee:
            transaction.downloaded_payee = transaction.payee
            transaction.payee_source = "QDB register displayed payee"
            transaction.payee = normalized_payee
        handle = by_internal.get(internal_id)
        if handle is None:
            transaction.account = "QDB account unresolved"
            transaction.raw["qdb_account_handle"] = None
            transaction.raw["qdb_account_name_source"] = "unresolved"
        else:
            transaction.account = account_names.get(handle, f"QDB account handle {handle}")
            catalog_item = account_catalog.get(handle)
            transaction.account_type = catalog_item[1] if catalog_item else None
            transaction.raw["qdb_account_handle"] = handle
            transaction.raw["qdb_account_name_source"] = "QDB type 0x80"
    transactions.extend(register_split_transactions)
    check_numbers_by_fit_id = _read_qdb_check_numbers_by_fit_id(directory)
    check_numbers_by_register = _read_qdb_register_check_numbers(directory)
    check_numbers_by_internal_id = _read_qdb_canonical_check_numbers(directory)
    for transaction in transactions:
        fit_id = transaction.fit_id or transaction.raw.get("fit_id")
        if transaction.raw.get("qdb_type") == "0x13c":
            internal_id = transaction.raw.get("qdb_internal_id")
            transaction.number = (
                check_numbers_by_internal_id.get(internal_id)
                if isinstance(internal_id, int)
                else None
            )
            if transaction.number is None and isinstance(fit_id, str) and fit_id:
                transaction.number = check_numbers_by_fit_id.get(fit_id)
        elif isinstance(fit_id, str) and fit_id and transaction.number is None:
            register_ref = transaction.raw.get("qdb_register_ref")
            if isinstance(register_ref, int):
                transaction.number = check_numbers_by_register.get(register_ref)
        if isinstance(fit_id, str) and fit_id:
            transaction.fit_id = fit_id
            transaction.raw["fit_id"] = fit_id

    return transactions


def write_qdb_security_prices_to_sqlite(
    source: str | Path, destination: str | Path
) -> tuple[int, int]:
    """Materialize qaccess security metadata and normalized daily quotes."""
    directory = Path(source)
    securities_path = directory / SECURITIES_NAME
    prices_path = directory / PRICE_HISTORY_NAME
    if not securities_path.is_file() and not prices_path.is_file():
        return 0, 0
    if not securities_path.is_file() or not prices_path.is_file():
        raise ValueError("incomplete QDB security-price sidecars")

    security_count = 0
    price_count = 0
    refs: dict[int, int] = {}
    with sqlite3.connect(destination) as connection:
        with securities_path.open(encoding="utf-8-sig", newline="") as source_file:
            reader = csv.DictReader(source_file, delimiter="\t")
            expected = ["qdb_security_ref", "name", "symbol"]
            if reader.fieldnames != expected:
                raise ValueError(f"unexpected {SECURITIES_NAME} header: {reader.fieldnames}")
            for row in reader:
                security_ref = int(row["qdb_security_ref"])
                name = row["name"].strip()
                if security_ref <= 0 or not name:
                    raise ValueError(f"invalid security row: {row}")
                cursor = connection.execute(
                    "INSERT INTO securities(qdb_security_ref, name, symbol) VALUES (?, ?, ?)",
                    (security_ref, name, row["symbol"].strip() or None),
                )
                refs[security_ref] = cursor.lastrowid
                security_count += 1

        with prices_path.open(encoding="utf-8-sig", newline="") as source_file:
            reader = csv.DictReader(source_file, delimiter="\t")
            legacy_expected = [
                "qdb_security_ref",
                "price_date",
                "price",
                "high",
                "low",
                "volume",
            ]
            if reader.fieldnames != legacy_expected:
                raise ValueError(f"unexpected {PRICE_HISTORY_NAME} header: {reader.fieldnames}")
            for row in reader:
                security_ref = int(row["qdb_security_ref"])
                security_id = refs.get(security_ref)
                if security_id is None:
                    raise ValueError(f"price references unknown QDB security {security_ref}")
                price_date = dt.date.fromisoformat(row["price_date"]).isoformat()
                price = row["price"].strip()
                if not price:
                    raise ValueError(f"price is missing for QDB security {security_ref}")
                volume = int(row["volume"]) if row["volume"].strip() else None
                connection.execute(
                    """INSERT INTO security_prices(
                        security_id, price_date, price, high, low, volume
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        security_id,
                        price_date,
                        price,
                        row["high"].strip() or None,
                        row["low"].strip() or None,
                        volume,
                    ),
                )
                price_count += 1
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [
                ("security_count", str(security_count)),
                ("security_price_count", str(price_count)),
            ],
        )
    return security_count, price_count


def write_qph_security_prices_to_sqlite(qdf_path: str | Path, destination: str | Path) -> int:
    """Overlay quote rows decoded from the QDF's embedded ``.QPH`` stream."""
    from .qph import parse_qph_bytes, read_qph_stream

    with sqlite3.connect(destination) as connection:
        rows = connection.execute(
            "SELECT id, symbol FROM securities WHERE symbol IS NOT NULL AND symbol <> ''"
        ).fetchall()
        symbol_to_ids: dict[str, list[int]] = defaultdict(list)
        for security_id, symbol in rows:
            symbol_to_ids[symbol].append(security_id)

        quotes = parse_qph_bytes(read_qph_stream(qdf_path), set(symbol_to_ids))
        values = []
        for quote in quotes:
            for security_id in symbol_to_ids[quote.symbol]:
                values.append(
                    (
                        security_id,
                        quote.price_date.isoformat(),
                        quote.price,
                        quote.high,
                        quote.low,
                        quote.volume,
                    )
                )
        connection.executemany(
            """INSERT INTO security_prices(
                   security_id, price_date, price, high, low, volume
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(security_id, price_date) DO UPDATE SET
                   price = excluded.price,
                   high = excluded.high,
                   low = excluded.low,
                   volume = excluded.volume""",
            values,
        )
        total = connection.execute("SELECT COUNT(*) FROM security_prices").fetchone()[0]
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [
                ("security_price_count", str(total)),
                ("security_price_qph_count", str(len(values))),
                ("security_price_source", "qaccess+embedded-qph"),
            ],
        )
    return len(values)


def _read_account_key_map(source: Path) -> dict[tuple[int, int], int]:
    data = (source / "qdb-account-map-134.bin").read_bytes()
    if len(data) < 16:
        raise ValueError("QDB account map is truncated")
    magic, version, record_size, count = struct.unpack_from("<4sIII", data)
    if magic != b"QATM" or version != 1 or record_size != ACCOUNT_INDEX_SIZE:
        raise ValueError("unexpected QDB account map layout")
    expected = 16 + count * (8 + record_size)
    if len(data) != expected:
        raise ValueError("QDB account map length does not match its header")
    result: dict[tuple[int, int], int] = {}
    for index in range(count):
        offset = 16 + index * (8 + record_size)
        account_handle, account_key = struct.unpack_from("<II", data, offset)
        internal_id = struct.unpack_from("<I", data, offset + 8)[0]
        result[(account_handle, account_key)] = internal_id
    return result


def _read_qdb_register_fit_ids(source: Path) -> dict[int, set[str]]:
    """Map native register references to legacy FITIDs when available.

    The account/key ordinal used by the native ``0xf7`` register collection is
    not the same ordinal used by the ``0x134`` canonical account index.  The
    legacy ``0x086`` rows carry the register reference and its FITID, which is
    the safe bridge between the two representations.
    """
    path = source / "qdb-type-086.bin"
    if not path.is_file():
        return {}
    result: dict[int, set[str]] = defaultdict(set)
    for record in _read_fixed_records(path, RECORD_SIZE):
        register_ref = struct.unpack_from("<I", record, 0)[0]
        fit_id = _cstring(record, 0x81, RECORD_SIZE)
        if fit_id:
            result[register_ref].add(fit_id)
    return result


def _read_qdb_register_check_numbers(source: Path) -> dict[int, str]:
    """Read native user-facing check/transaction numbers by register reference."""
    path = source / "qdb-register-memo.tsv"
    if not path.is_file():
        return {}
    result: dict[int, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as source_file:
        reader = csv.DictReader(source_file, delimiter="\t")
        if "check_number" not in (reader.fieldnames or []):
            return result
        for row in reader:
            value = (row.get("check_number") or "").strip()
            if not value:
                continue
            try:
                result[int(row["register_ref"])] = value
            except (KeyError, TypeError, ValueError):
                continue
    return result


def _read_qdb_register_clear_status(source: Path) -> dict[int, int]:
    """Read native clear-status enum values by register reference."""
    path = source / REGISTER_CLEAR_STATUS_NAME
    if not path.is_file():
        return {}
    result: dict[int, int] = {}
    with path.open(encoding="utf-8-sig", newline="") as source_file:
        reader = csv.DictReader(source_file, delimiter="\t")
        if "clear_status" not in (reader.fieldnames or []):
            return result
        for row in reader:
            try:
                result[int(row["register_ref"])] = int(row["clear_status"])
            except (KeyError, TypeError, ValueError):
                continue
    return result


def _decode_qdb_clear_status(value: int | None) -> str | None:
    """Translate Quicken's register clear-status enum to QIF values."""
    return {
        0: None,
        1: "c",
        2: "R",
    }.get(value)


def _read_qdb_canonical_check_numbers(source: Path) -> dict[int, str]:
    """Read check numbers queried directly from canonical transaction objects."""
    path = source / "qdb-canonical-check-numbers.tsv"
    if not path.is_file():
        return {}
    result: dict[int, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as source_file:
        reader = csv.DictReader(source_file, delimiter="\t")
        if "check_number" not in (reader.fieldnames or []):
            return result
        for row in reader:
            value = (row.get("check_number") or "").strip()
            if not value:
                continue
            try:
                result[int(row["qdb_internal_id"])] = value
            except (KeyError, TypeError, ValueError):
                continue
    return result


def _read_qdb_check_numbers_by_fit_id(source: Path) -> dict[str, str]:
    """Join native check numbers back to canonical transactions through FITIDs."""
    check_by_register = _read_qdb_register_check_numbers(source)
    if not check_by_register:
        return {}
    result: dict[str, str] = {}
    for fit_id, register_refs in _read_qdb_register_fit_ids(source).items():
        values = {check_by_register[ref] for ref in register_refs if ref in check_by_register}
        if len(values) == 1:
            result[fit_id] = next(iter(values))
    return result


def _read_qdb_register_canonical_links(source: Path) -> dict[int, int]:
    """Map a native register QID to a canonical internal transaction ID.

    Type ``0x096`` identity rows occasionally contain both the ``0xf7`` and
    ``0x134`` references.  Only those explicit pairs are accepted; account and
    key ordinals are deliberately not used because their namespaces differ.
    """
    path = source / "qdb-type-096.bin"
    if not path.is_file():
        return {}
    groups: dict[bytes, dict[int, int]] = defaultdict(dict)
    for record in _read_fixed_records(path, 50):
        item_type = struct.unpack_from("<H", record, 8)[0]
        if item_type in (ACCOUNT_INDEX_TYPE, REGISTER_TRANSACTION_TYPE):
            groups[record[0x22:0x32]][item_type] = struct.unpack_from("<I", record, 4)[0]
    result: dict[int, int] = {}
    for linked in groups.values():
        canonical_id = linked.get(ACCOUNT_INDEX_TYPE)
        register_id = linked.get(REGISTER_TRANSACTION_TYPE)
        if canonical_id is not None and register_id is not None:
            result[register_id] = canonical_id
    return result


def write_qdb_investment_transactions_to_sqlite(
    source: str | Path, destination: str | Path
) -> tuple[int, int]:
    """Materialize native investment fields and link only explicit matches.

    The per-account key ordinals in the native ``0xf7`` collection and the
    canonical ``0x134`` account index are independent namespaces, so matching
    them directly can attach a security purchase to an unrelated transaction.
    FITIDs and type-``0x096`` identity links are the accepted bridges.
    """
    directory = Path(source)
    sidecar = directory / INVESTMENT_TRANSACTIONS_NAME
    if not sidecar.is_file():
        return 0, 0
    account_catalog = (
        parse_qdb_accounts(directory / "qdb-type-080.bin")
        if (directory / "qdb-type-080.bin").is_file()
        else {}
    )
    with sqlite3.connect(destination) as connection:
        account_ids = {
            int(handle): int(account_id)
            for account_id, handle in connection.execute(
                "SELECT id, qdb_handle FROM accounts WHERE qdb_handle IS NOT NULL"
            )
        }
        security_ids = {
            int(reference): int(security_id)
            for security_id, reference in connection.execute(
                "SELECT id, qdb_security_ref FROM securities"
            )
        }
        transaction_ids: dict[int, int] = {}
        transaction_ids_by_fit: dict[str, set[int]] = defaultdict(set)
        transaction_ids_by_register_ref: dict[int, int] = {}
        for transaction_id, register_ref, number, fit_id, internal_id in connection.execute(
            "SELECT id, qdb_register_ref, number, fit_id, qdb_internal_id FROM transactions"
        ):
            if isinstance(register_ref, int):
                transaction_ids_by_register_ref[register_ref] = int(transaction_id)
            if isinstance(internal_id, int):
                transaction_ids[internal_id] = int(transaction_id)
            transaction_fit_id = fit_id or number
            if transaction_fit_id:
                transaction_ids_by_fit[str(transaction_fit_id)].add(int(transaction_id))
        register_fit_ids = _read_qdb_register_fit_ids(directory)
        register_canonical_links = _read_qdb_register_canonical_links(directory)

        with sidecar.open(encoding="utf-8-sig", newline="") as source_file:
            reader = csv.DictReader(source_file, delimiter="\t")
            if reader.fieldnames != NATIVE_INVESTMENT_SIDECAR_FIELDS:
                raise ValueError(
                    f"unexpected {INVESTMENT_TRANSACTIONS_NAME} header: {reader.fieldnames}"
                )
            rows: list[tuple[object, ...]] = []
            linked = 0
            for row in reader:
                account_handle = int(row["account"])
                account_key = int(row["key"])
                account_id = account_ids.get(account_handle)
                if account_id is None:
                    catalog_item = account_catalog.get(account_handle)
                    if catalog_item is None:
                        raise ValueError(
                            f"investment row references unknown account {account_handle}"
                        )
                    account_name, account_type = catalog_item
                    connection.execute(
                        "INSERT INTO accounts(name, account_type, qdb_handle) "
                        "VALUES (?, ?, ?) ON CONFLICT(name) DO NOTHING",
                        (account_name, account_type, account_handle),
                    )
                    connection.execute(
                        "UPDATE accounts SET qdb_handle = COALESCE(qdb_handle, ?) WHERE name = ?",
                        (account_handle, account_name),
                    )
                    account_id = connection.execute(
                        "SELECT id FROM accounts WHERE name = ?", (account_name,)
                    ).fetchone()[0]
                    account_ids[account_handle] = int(account_id)
                register_ref = int(row["register_ref"])
                internal_id = register_canonical_links.get(register_ref)
                transaction_id = transaction_ids_by_register_ref.get(register_ref)
                if transaction_id is None:
                    transaction_id = transaction_ids.get(internal_id)
                if transaction_id is None:
                    fit_ids = register_fit_ids.get(register_ref, set())
                    if len(fit_ids) == 1:
                        transaction_candidates = transaction_ids_by_fit.get(
                            next(iter(fit_ids)), set()
                        )
                        if len(transaction_candidates) == 1:
                            transaction_id = next(iter(transaction_candidates))
                if transaction_id is not None:
                    linked += 1
                security_ref = int(row["security_ref"])
                security_id = security_ids.get(security_ref) if security_ref else None
                if security_ref and security_id is None:
                    raise ValueError(f"investment row references unknown security {security_ref}")
                transaction_date = dt.date.fromisoformat(row["transaction_date"]).isoformat()
                shares = str(Decimal(row["shares"]))
                price = str(Decimal(row["price"]))
                amount = str(Decimal(int(row["investment_amount"])) / Decimal(100))
                transaction_amount = str(Decimal(int(row["transaction_amount"])) / Decimal(100))
                backfill_pair = (
                    int(row["backfill_pair"]) or None if row["backfill_pair"].strip() else None
                )
                is_backfill_cash = (
                    int(row["is_backfill_cash"]) if row["is_backfill_cash"].strip() else 0
                )
                transfer_qid = (
                    int(row["transfer_qid"]) or None if row["transfer_qid"].strip() else None
                )
                transfer_account = (
                    int(row["xfer_account"]) or None if row["xfer_account"].strip() else None
                )
                if not row["native_cash_balance"].strip():
                    raise ValueError(
                        f"investment row {account_handle}/{account_key} has no native_cash_balance"
                    )
                native_cash_balance = str(Decimal(int(row["native_cash_balance"])) / Decimal(100))
                rows.append(
                    (
                        account_id,
                        transaction_id,
                        account_key,
                        transaction_date,
                        security_id,
                        shares,
                        price,
                        amount,
                        transaction_amount,
                        backfill_pair,
                        is_backfill_cash,
                        transfer_qid,
                        transfer_account,
                        native_cash_balance,
                        row["inv_txn_type_name"].strip(),
                    )
                )
            connection.executemany(
                """INSERT INTO investment_transactions(
                    account_id, transaction_id, register_key,
                    transaction_date, security_id, shares, price, investment_amount,
                    transaction_amount, backfill_pair_ref, is_backfill_cash,
                    transfer_qdb_register_ref, transfer_account, native_cash_balance,
                    transaction_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            connection.execute(
                """UPDATE transactions
                   SET security = (
                           SELECT securities.name
                           FROM investment_transactions
                           JOIN securities ON securities.id = investment_transactions.security_id
                           WHERE investment_transactions.transaction_id = transactions.id
                             AND investment_transactions.security_id IS NOT NULL
                       ),
                       price = (
                           SELECT investment_transactions.price
                           FROM investment_transactions
                           WHERE investment_transactions.transaction_id = transactions.id
                             AND investment_transactions.security_id IS NOT NULL
                       ),
                       quantity = (
                           SELECT investment_transactions.shares
                           FROM investment_transactions
                           WHERE investment_transactions.transaction_id = transactions.id
                             AND investment_transactions.security_id IS NOT NULL
                       )
                 WHERE id IN (
                     SELECT transaction_id FROM investment_transactions
                     WHERE transaction_id IS NOT NULL
                       AND security_id IS NOT NULL
                 )"""
            )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [
                ("investment_transaction_count", str(len(rows))),
                ("investment_transaction_linked_count", str(linked)),
            ],
        )
    return len(rows), linked


def _read_qdb_investment_register_balances(
    source: str | Path, account_ids_by_handle: dict[int, int]
) -> dict[int, dict[str, Decimal]]:
    """Read effective cash balances from native 0xf7 investment-register rows."""
    register_path = Path(source) / "qdb-type-0f7.bin"
    if not register_path.is_file():
        return {}
    daily_changes: dict[int, dict[str, Decimal]] = defaultdict(dict)
    for record in _read_fixed_records(register_path, ACCOUNT_INDEX_SIZE):
        account_handle = struct.unpack_from("<H", record, 4)[0]
        account_id = account_ids_by_handle.get(account_handle)
        if account_id is None:
            continue
        try:
            register_date = dt.date(record[8] + 1900, record[7], record[6]).isoformat()
        except ValueError:
            continue
        amount = Decimal(struct.unpack_from("<q", record, 0x24)[0]) / Decimal(100)
        changes = daily_changes[account_id]
        changes[register_date] = changes.get(register_date, Decimal(0)) + amount
    balances: dict[int, dict[str, Decimal]] = {}
    for account_id, changes in daily_changes.items():
        running = Decimal(0)
        balances[account_id] = {}
        for balance_date in sorted(changes):
            running += changes[balance_date]
            balances[account_id][balance_date] = running
    return balances


# Investment-register transactions represent a security leg and, for some
# transaction types, a separate cash leg.  The amount returned by the native
# transaction object is therefore only a cash change for the direct cash
# types below.  ``BoughtX``/``SoldX`` and ``Added``/``Removed`` are security
# or inter-account transfer legs; treating their amount as cash is what made
# the old balance materialization produce large, spurious negative balances.
_INVESTMENT_DIRECT_CASH_TYPES = frozenset(
    {
        "Bought",
        "Sold",
        "Div",
        "IntInc",
        "CGLong",
        "CGShort",
        "RtrnCap",
        "ShtSell",
    }
)
_INVESTMENT_CASH_TRANSFER_TYPES = frozenset(
    {
        "ContribX",
        "WithdrwX",
        "IntIncX",
        "MiscExpX",
        "MiscIncX",
        "XIn",
        "XOut",
    }
)


def _investment_cash_delta(
    transaction_type: str, amount: Decimal, native_is_cash: bool | None = None
) -> Decimal:
    """Return the native transaction's effective cash delta.

    The native ``GetAmount`` value is authoritative, but its applicability is
    determined by the investment transaction type. Cash-transfer types are
    included by their native type; security-only ``BoughtX``/``SoldX`` rows
    remain excluded.
    """
    if native_is_cash is True:
        return amount
    normalized = transaction_type.strip()
    if normalized in _INVESTMENT_DIRECT_CASH_TYPES:
        return amount
    if normalized in _INVESTMENT_CASH_TRANSFER_TYPES:
        return amount
    return Decimal(0)


def _read_qdb_investment_cash_flags(source: str | Path) -> dict[tuple[int, int], bool]:
    """Read the native cash-leg flag without persisting it in SQLite.

    ``is_cash`` is an extraction-level diagnostic supplied by qaccess.  The
    analytics schema intentionally omits that column, but the flag still
    carries information for transaction types whose cash behavior is not
    represented by the readable type name alone.
    """
    sidecar = Path(source) / INVESTMENT_TRANSACTIONS_NAME
    if not sidecar.is_file():
        return {}
    result: dict[tuple[int, int], bool] = {}
    with sidecar.open(encoding="utf-8-sig", newline="") as source_file:
        reader = csv.DictReader(source_file, delimiter="\t")
        if not reader.fieldnames or "is_cash" not in reader.fieldnames:
            return result
        for row in reader:
            try:
                result[(int(row["account"]), int(row["key"]))] = row["is_cash"].strip() == "1"
            except (KeyError, TypeError, ValueError):
                continue
    return result


def _read_qdb_investment_native_balances(
    source: str | Path,
) -> dict[tuple[int, int], Decimal]:
    """Read qaccess's exact cash balance after each investment-register row."""
    sidecar = Path(source) / INVESTMENT_TRANSACTIONS_NAME
    if not sidecar.is_file():
        return {}
    result: dict[tuple[int, int], Decimal] = {}
    with sidecar.open(encoding="utf-8-sig", newline="") as source_file:
        reader = csv.DictReader(source_file, delimiter="\t")
        if reader.fieldnames != NATIVE_INVESTMENT_SIDECAR_FIELDS:
            raise ValueError(
                f"unexpected {INVESTMENT_TRANSACTIONS_NAME} header: {reader.fieldnames}"
            )
        for row in reader:
            try:
                result[(int(row["account"]), int(row["key"]))] = Decimal(
                    row["native_cash_balance"]
                ) / Decimal(100)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
    return result


def write_qdb_investment_balance_periods_to_sqlite(
    source: str | Path | None, destination: str | Path
) -> int:
    """Materialize effective-dated investment cash, share, and value balances.

    A period begins on each investment transaction date and on each quoted price
    date for securities held by that account. Shares and prices are taken from
    the native investment transaction object. Cash is derived from the native
    transaction type; the generic 0xf7 account-register amount
    is deliberately not used because it mixes security and cash legs for
    investment accounts.
    """
    with sqlite3.connect(destination) as connection:
        connection.execute("DELETE FROM investment_position_balance_periods")
        connection.execute("DELETE FROM investment_account_balance_periods")
        price_rows = connection.execute(
            "SELECT security_id, price_date, price FROM security_prices "
            "ORDER BY security_id, price_date"
        ).fetchall()
        prices: dict[int, list[tuple[str, Decimal]]] = defaultdict(list)
        for security_id, price_date, price in price_rows:
            prices[int(security_id)].append((price_date, Decimal(price)))
        transactions: dict[
            int,
            list[
                tuple[
                    str,
                    int,
                    int | None,
                    Decimal,
                    Decimal,
                    Decimal,
                    Decimal,
                    str,
                    Decimal | None,
                ]
            ],
        ] = defaultdict(list)
        native_cash_flags = _read_qdb_investment_cash_flags(source) if source else {}
        native_cash_balances = _read_qdb_investment_native_balances(source) if source else {}
        account_handles = {
            int(account_id): int(handle)
            for account_id, handle in connection.execute(
                "SELECT id, qdb_handle FROM accounts WHERE qdb_handle IS NOT NULL"
            )
        }
        backfill_accounts = {
            int(account_id)
            for (account_id,) in connection.execute(
                "SELECT DISTINCT account_id FROM investment_transactions "
                "WHERE backfill_pair_ref IS NOT NULL OR is_backfill_cash <> 0"
            )
        }
        for (
            account_id,
            register_key,
            transaction_date,
            security_id,
            shares,
            price,
            amount,
            transaction_amount,
            transaction_type,
            stored_native_cash_balance,
        ) in connection.execute(
            "SELECT account_id, register_key, transaction_date, security_id, shares, price, "
            "investment_amount, COALESCE(transaction_amount, investment_amount), "
            "transaction_type, native_cash_balance "
            "FROM investment_transactions ORDER BY account_id, transaction_date, id"
        ):
            transactions[int(account_id)].append(
                (
                    transaction_date,
                    int(register_key),
                    int(security_id) if security_id is not None else None,
                    Decimal(shares),
                    Decimal(price),
                    Decimal(amount),
                    Decimal(transaction_amount),
                    str(transaction_type),
                    native_cash_balances.get(
                        (account_handles.get(int(account_id), 0), int(register_key)),
                        (
                            Decimal(stored_native_cash_balance)
                            if stored_native_cash_balance is not None
                            else None
                        ),
                    ),
                )
            )

        account_rows: list[tuple[object, ...]] = []
        position_rows: list[tuple[object, ...]] = []
        for account_id, account_transactions in transactions.items():
            first_transaction_type = account_transactions[0][7].strip()
            has_native_cash_series = any(
                transaction[8] is not None for transaction in account_transactions
            )
            has_opening_cash_anchor = first_transaction_type in _INVESTMENT_CASH_TRANSFER_TYPES
            cash_balance_status = (
                "anchored"
                if has_opening_cash_anchor or has_native_cash_series
                else "unanchored_opening_balance"
            )
            securities = {
                security_id
                for _, _, security_id, _, _, _, _, _, _ in account_transactions
                if security_id is not None
            }
            dates = {transaction[0] for transaction in account_transactions}
            for security_id in securities:
                dates.update(price_date for price_date, _ in prices.get(security_id, ()))
            ordered_dates = sorted(dates)
            transactions_by_date: dict[
                str,
                list[
                    tuple[
                        str,
                        int,
                        int | None,
                        Decimal,
                        Decimal,
                        Decimal,
                        Decimal,
                        str,
                        Decimal | None,
                    ]
                ],
            ] = defaultdict(list)
            for transaction in account_transactions:
                transactions_by_date[transaction[0]].append(transaction)
            shares_by_security: dict[int, Decimal] = defaultdict(Decimal)
            latest_transaction_price: dict[int, tuple[str, Decimal]] = {}
            cash = Decimal(0)
            account_values: list[tuple[str, Decimal, Decimal, Decimal]] = []
            position_values: list[tuple[str, int, Decimal, Decimal | None, Decimal]] = []
            for balance_date in ordered_dates:
                for (
                    _,
                    register_key,
                    security_id,
                    share_delta,
                    transaction_price,
                    _amount,
                    transaction_amount,
                    transaction_type,
                    native_cash_balance,
                ) in transactions_by_date.get(balance_date, ()):
                    native_cash_flag = native_cash_flags.get(
                        (account_handles.get(account_id, 0), register_key)
                    )
                    if native_cash_balance is not None:
                        cash = native_cash_balance
                    else:
                        cash += _investment_cash_delta(
                            transaction_type, transaction_amount, native_cash_flag
                        )
                    if security_id is not None:
                        if transaction_type.strip() == "StkSplit":
                            # qaccess exposes the split ratio in GetShares,
                            # not a signed share delta (e.g. 3 means 3-for-1).
                            shares_by_security[security_id] *= share_delta
                        else:
                            shares_by_security[security_id] += share_delta
                        if transaction_price > 0:
                            latest_transaction_price[security_id] = (
                                balance_date,
                                transaction_price,
                            )
                investment_value = Decimal(0)
                for security_id in securities:
                    share_balance = shares_by_security[security_id]
                    price_history = prices.get(security_id, ())
                    price_dates = [item[0] for item in price_history]
                    price_index = bisect_right(price_dates, balance_date) - 1
                    if price_index >= 0:
                        price = price_history[price_index][1]
                    else:
                        price = None
                    transaction_price = latest_transaction_price.get(security_id)
                    if transaction_price and (
                        price is None or transaction_price[0] >= price_dates[price_index]
                    ):
                        price = transaction_price[1]
                    market_value = share_balance * price if price is not None else Decimal(0)
                    investment_value += market_value
                    if share_balance or price is not None:
                        position_values.append(
                            (
                                balance_date,
                                security_id,
                                share_balance,
                                price,
                                market_value,
                            )
                        )
                account_values.append(
                    (balance_date, cash, investment_value, cash + investment_value)
                )
            for index, (balance_date, cash, investment_value, total_value) in enumerate(
                account_values
            ):
                next_date = (
                    account_values[index + 1][0] if index + 1 < len(account_values) else None
                )
                account_rows.append(
                    (
                        account_id,
                        balance_date,
                        next_date,
                        str(cash),
                        cash_balance_status,
                        (
                            "native_qaccess_balance_series"
                            if has_native_cash_series
                            else (
                                "native_transaction_types+backfill_pair_metadata"
                                if account_id in backfill_accounts
                                else "native_transaction_types"
                            )
                        ),
                        str(investment_value),
                        str(total_value),
                    )
                )
            next_dates: dict[tuple[str, int], str | None] = {}
            for security_id in securities:
                security_dates = [
                    balance_date
                    for balance_date, item_security_id, *_ in position_values
                    if item_security_id == security_id
                ]
                for index, balance_date in enumerate(security_dates):
                    next_dates[(balance_date, security_id)] = (
                        security_dates[index + 1] if index + 1 < len(security_dates) else None
                    )
            for (
                balance_date,
                security_id,
                share_balance,
                price,
                market_value,
            ) in position_values:
                next_date = next_dates[(balance_date, security_id)]
                position_rows.append(
                    (
                        account_id,
                        security_id,
                        balance_date,
                        next_date,
                        str(share_balance),
                        str(price) if price is not None else None,
                        str(market_value),
                    )
                )
        connection.executemany(
            "INSERT INTO investment_account_balance_periods("
            "account_id, balance_date, next_balance_date, cash_balance, "
            "cash_balance_status, cash_balance_source, investment_value, total_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            account_rows,
        )
        connection.executemany(
            "INSERT INTO investment_position_balance_periods("
            "account_id, security_id, balance_date, next_balance_date, shares, price, market_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            position_rows,
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [
                ("investment_account_balance_period_count", str(len(account_rows))),
                ("investment_position_balance_period_count", str(len(position_rows))),
            ],
        )
    return len(account_rows)


def write_qdb_register_balance_periods_to_sqlite(
    source: str | Path, destination: str | Path
) -> int:
    """Use account-register (0xf7) rows for QDB balance periods.

    The 0x13c and 0xf7 families are parallel representations of ledger
    history.  The 0xf7 register amount is the representation that corresponds
    to the balance shown for an account in Quicken.  Only accounts with valid
    register rows are replaced; accounts absent from the register extract
    retain the transaction-derived fallback written by ``write_transactions``.
    """
    register_path = Path(source) / "qdb-type-0f7.bin"
    if not register_path.is_file():
        return 0

    from .sqlite_export import _date

    register_dates: dict[int, dict[dt.date, int]] = {}
    with sqlite3.connect(destination) as connection:
        account_rows = connection.execute(
            "SELECT id, qdb_handle FROM accounts "
            "WHERE account_type IN ('Bank', 'Banking', 'Cash') "
            "AND qdb_handle IS NOT NULL"
        ).fetchall()
        account_ids_by_handle = {
            int(handle): int(account_id) for account_id, handle in account_rows
        }

        for record in _read_fixed_records(register_path, ACCOUNT_INDEX_SIZE):
            account_handle = struct.unpack_from("<H", record, 4)[0]
            account_id = account_ids_by_handle.get(account_handle)
            if account_id is None:
                continue
            try:
                register_date = dt.date(record[8] + 1900, record[7], record[6])
            except ValueError:
                continue
            amount_cents = struct.unpack_from("<q", record, 0x24)[0]
            daily_changes = register_dates.setdefault(account_id, {})
            daily_changes[register_date] = daily_changes.get(register_date, 0) + amount_cents

        if not register_dates:
            return 0
        connection.executemany(
            "DELETE FROM banking_account_balance_periods WHERE account_id = ?",
            [(account_id,) for account_id in register_dates],
        )
        period_rows: list[tuple[int, str | None, str | None, int]] = []
        for account_id, daily_changes in register_dates.items():
            dates = sorted(daily_changes)
            period_rows.append(
                (
                    account_id,
                    None,
                    _date(dates[0]),
                    0,
                )
            )
            balance_cents = 0
            for index, balance_date in enumerate(dates):
                balance_cents += daily_changes[balance_date]
                next_date = dates[index + 1] if index + 1 < len(dates) else None
                period_rows.append(
                    (
                        account_id,
                        _date(balance_date),
                        _date(next_date),
                        balance_cents,
                    )
                )
        connection.executemany(
            "INSERT INTO banking_account_balance_periods("
            "account_id, balance_date, next_balance_date, balance_cents) "
            "VALUES (?, ?, ?, ?)",
            period_rows,
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                "balance_source",
                "qdb-register-0xf7-with-transaction-fallback",
            ),
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("balance_period_count", str(len(period_rows))),
        )
    return len(period_rows)


def export_qdb_financial_to_sqlite(
    source: str | Path,
    destination: str | Path,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    qdf_path: str | Path | None = None,
) -> int:
    """Materialize a complete extracted QDB directory as a financial SQLite export."""
    from .sqlite_export import write_transactions

    directory = Path(source)
    register_transactions = parse_qdb_register_transactions(directory)
    fi_transactions = parse_qdb_fi_transactions(directory)
    if start_date or end_date:
        register_transactions = [
            transaction
            for transaction in register_transactions
            if transaction.date is not None
            and (start_date is None or transaction.date >= start_date)
            and (end_date is None or transaction.date <= end_date)
        ]
        fi_transactions = [
            transaction
            for transaction in fi_transactions
            if transaction.date is not None
            and (start_date is None or transaction.date >= start_date)
            and (end_date is None or transaction.date <= end_date)
        ]
    transactions = register_transactions
    count = write_transactions(
        destination,
        transactions,
        source_format="qdb-api-register-0xf7-with-fi-transactions",
        source_name=directory.name,
    )
    write_qdb_fi_transactions_to_sqlite(directory, destination, fi_transactions)
    write_qdb_category_catalog_to_sqlite(directory, destination)
    write_qdb_tag_catalog_to_sqlite(directory, destination)
    from .qdb_budgets import write_qdb_budgets_to_sqlite

    write_qdb_budgets_to_sqlite(directory, destination)
    write_qdb_account_status_to_sqlite(
        directory,
        destination,
        exclude_names=QDB_INTERNAL_ACCOUNT_NAMES,
    )
    write_qdb_register_balance_periods_to_sqlite(directory, destination)
    write_qdb_security_prices_to_sqlite(directory, destination)
    if qdf_path is not None:
        write_qph_security_prices_to_sqlite(qdf_path, destination)
    write_qdb_investment_transactions_to_sqlite(directory, destination)
    write_qdb_investment_balance_periods_to_sqlite(directory, destination)
    report_source = directory / "qdb-reports.bin"
    if report_source.is_file():
        from .qdb_reports import write_qdb_reports_to_sqlite

        write_qdb_reports_to_sqlite(
            report_source,
            destination,
            directory / "qdb-type-080.bin",
        )
    with sqlite3.connect(destination) as connection:
        connection.executemany(
            "DELETE FROM accounts WHERE name = ?",
            [(name,) for name in QDB_INTERNAL_ACCOUNT_NAMES],
        )
    return count


def parse_qdb_fi_transactions(source: str | Path) -> list[QifTransaction]:
    """Parse canonical FI/download transactions without register overlays."""
    directory = Path(source)
    transactions = parse_qdb_type13c(directory / "qdb-type-13c.bin")
    account_catalog = parse_qdb_accounts(directory / "qdb-type-080.bin")
    account_names = {handle: item[0] for handle, item in account_catalog.items()}
    by_internal = _read_account_index(directory / "qdb-account-map-134.bin")
    for transaction in transactions:
        internal_id = int(transaction.raw["qdb_internal_id"])
        handle = by_internal.get(internal_id)
        if handle is None:
            transaction.account = "QDB account unresolved"
            transaction.account_type = None
            transaction.raw["qdb_account_handle"] = None
            continue
        transaction.account = account_names.get(handle, f"QDB account handle {handle}")
        transaction.account_type = (account_catalog.get(handle) or (None, None))[1]
        transaction.raw["qdb_account_handle"] = handle
    return transactions


def write_qdb_fi_transactions_to_sqlite(
    source: str | Path,
    destination: str | Path,
    transactions: list[QifTransaction],
) -> tuple[int, int]:
    """Materialize canonical FI rows and link them to register rows when reliable."""
    from .sqlite_export import _account_id_with_metadata, _date, _decimal

    directory = Path(source)
    canonical_to_register = {
        canonical_id: register_ref
        for register_ref, canonical_id in _read_qdb_register_canonical_links(directory).items()
    }
    register_fit_ids = _read_qdb_register_fit_ids(directory)
    fit_to_refs: dict[str, set[int]] = defaultdict(set)
    for register_ref, fit_ids in register_fit_ids.items():
        for fit_id in fit_ids:
            fit_to_refs[fit_id].add(register_ref)

    with sqlite3.connect(destination) as connection:
        account_ids = {
            int(handle): int(account_id)
            for account_id, handle in connection.execute(
                "SELECT id, qdb_handle FROM accounts WHERE qdb_handle IS NOT NULL"
            )
        }
        register_rows = {
            int(register_ref): (int(transaction_id), account_id, Decimal(amount))
            for transaction_id, register_ref, account_id, amount in connection.execute(
                "SELECT id, qdb_register_ref, account_id, amount "
                "FROM transactions WHERE qdb_register_ref IS NOT NULL"
            )
            if amount is not None
        }
        rows: list[tuple[object, ...]] = []
        linked = 0
        for transaction in transactions:
            internal_id = int(transaction.raw["qdb_internal_id"])
            handle = transaction.raw.get("qdb_account_handle")
            account_id = account_ids.get(handle) if isinstance(handle, int) else None
            if account_id is None and transaction.account:
                account_id = _account_id_with_metadata(
                    connection,
                    transaction.account,
                    transaction.account_type or transaction.section,
                    handle if isinstance(handle, int) else None,
                )
                if isinstance(handle, int):
                    account_ids[handle] = account_id

            register_ref = canonical_to_register.get(internal_id)
            link_method = "0x096" if register_ref in register_rows else None
            if register_ref not in register_rows:
                fit_id = transaction.fit_id or transaction.number
                candidates = [
                    ref
                    for ref in fit_to_refs.get(fit_id or "", set())
                    if ref in register_rows
                    and register_rows[ref][1] == account_id
                    and register_rows[ref][2] == transaction.amount
                ]
                if len(candidates) == 1:
                    register_ref = candidates[0]
                    link_method = "FITID/0x086"
                else:
                    register_ref = None
            register_transaction_id = (
                register_rows[register_ref][0]
                if register_ref is not None and register_ref in register_rows
                else None
            )
            if register_transaction_id is not None:
                linked += 1
            rows.append(
                (
                    register_transaction_id,
                    account_id,
                    _date(transaction.date),
                    _decimal(transaction.amount),
                    transaction.payee,
                    transaction.fit_id or transaction.number,
                    transaction.cleared,
                    "QDB:0x13c",
                    internal_id,
                    register_ref,
                    link_method,
                )
            )
        connection.execute("DELETE FROM fi_transactions")
        connection.executemany(
            """INSERT INTO fi_transactions(
                register_transaction_id, account_id, transaction_date, amount, payee,
                fit_id, cleared, qdb_record_type, qdb_internal_id, qdb_register_ref,
                link_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [
                ("fi_transaction_count", str(len(rows))),
                ("fi_linked_count", str(linked)),
            ],
        )
    return len(rows), linked
