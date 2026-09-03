"""Reproduce saved-report content from decoded settings and SQLite data."""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .qdb_reports import QdbReportComponent, load_qdb_report_component, report_type_name

GROCERY_REPORT_NAME = "Grocery expenses"
UNCLEARED_REPORT_NAME = "Uncleared transactions"


@dataclass(frozen=True)
class ReportTypeRule:
    """Describe the report-type handler and its currently supported scope."""

    report_type: int
    handler: str
    supported_reports: tuple[str, ...]


# Keep report-type behavior declarative so additional report families can add
# handlers without embedding their rules in the command-line wrapper.
REPORT_TYPE_RULES = {
    1: ReportTypeRule(1, "balance", ()),
    2: ReportTypeRule(2, "balance", ()),
    4: ReportTypeRule(
        report_type=4,
        handler="transaction-detail",
        supported_reports=(),
    ),
    7: ReportTypeRule(7, "itemized-category", ()),
    13: ReportTypeRule(13, "cash-flow", ()),
    14: ReportTypeRule(14, "expense-summary", ()),
    20: ReportTypeRule(20, "budget-detail", ()),
    29: ReportTypeRule(29, "cash-flow", ()),
    32: ReportTypeRule(32, "spending", ()),
    47: ReportTypeRule(47, "itemized-payee", ()),
    55: ReportTypeRule(55, "itemized-tag", ()),
}


class ReportRenderer(Protocol):
    """Interface implemented by one decoded QDB report-type renderer."""

    report_type: int
    output_format: str

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render one component and return the number of detail rows written."""


@dataclass(frozen=True)
class ReproducedTransactionRow:
    """One register or split line rendered by a transaction-detail report."""

    transaction_id: int
    split_line: int | None
    transaction_date: str
    account: str
    payee: str
    category: str
    memo: str
    amount: Decimal


def _find_component(connection: sqlite3.Connection, report_name: str) -> QdbReportComponent:
    component = load_qdb_report_component(connection, report_name)
    if component is None:
        raise ValueError(f"saved report not found in SQLite export: {report_name}")
    return component


def _category_matches(category: str, selected: frozenset[str], show_subcategories: bool) -> bool:
    if not category:
        return False
    if category in selected:
        return True
    return show_subcategories and any(category.startswith(name + ":") for name in selected)


def _decimal(value: str | None) -> Decimal:
    return Decimal(value or "0")


def _effective_date_bounds(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> tuple[str, str]:
    """Resolve a saved preset relative to the extract date."""
    if component.custom_start_date is not None and component.custom_end_date is not None:
        return component.custom_start_date.isoformat(), component.custom_end_date.isoformat()
    if component.date_range != "Last 12 months":
        raise ValueError(f"unsupported transaction date range: {component.date_range!r}")
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key = 'extract_date'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row:
        row = connection.execute("SELECT MAX(transaction_date) FROM transactions").fetchone()
    if not row or not row[0]:
        raise ValueError("cannot resolve Last 12 Months without an extract date")
    end = date.fromisoformat(row[0])
    try:
        start = end.replace(year=end.year - 1)
    except ValueError:
        start = date(end.year - 1, 2, 28)
    return start.isoformat(), end.isoformat()


def _validate_grocery_definition(component: QdbReportComponent) -> None:
    if component.report_type != 4:
        raise ValueError(f"{GROCERY_REPORT_NAME} is not a type-4 transaction report")
    expected = {
        "date_range": "Custom range",
        "subtotal": "Don't subtotal",
        "sort_by": "Date/Account",
        "account_filter_mode": "Include all accounts",
        "category_filter_mode": "Include only selected categories",
        "transfer_mode": "Include all",
        "subcategory_mode": "Show all",
        "rounding": "Cents (no rounding)",
    }
    mismatches = {
        field: (getattr(component, field), value)
        for field, value in expected.items()
        if getattr(component, field) != value
    }
    if mismatches:
        details = ", ".join(
            f"{field}={actual!r}, expected {expected!r}"
            for field, (actual, expected) in mismatches.items()
        )
        raise ValueError(f"unsupported Grocery Expenses definition: {details}")
    if component.custom_start_date is None or component.custom_end_date is None:
        raise ValueError("Grocery Expenses has no usable custom date range")
    if any(
        getattr(component, field) is not None
        for field in (
            "status_not_cleared",
            "status_newly_cleared",
            "status_newly_reconciled",
            "status_reconciled",
        )
    ):
        raise ValueError("Grocery Expenses status filtering is not supported by this renderer")


def reproduce_grocery_expenses(connection: sqlite3.Connection) -> list[ReproducedTransactionRow]:
    """Compatibility wrapper for the generic type-4 renderer."""
    return reproduce_type4_transactions(connection, GROCERY_REPORT_NAME)


def _validate_uncleared_definition(component: QdbReportComponent) -> None:
    if component.report_type != 4:
        raise ValueError(f"{UNCLEARED_REPORT_NAME} is not a type-4 transaction report")
    expected = {
        "date_range": "Last 12 months",
        "rounding": "Cents (no rounding)",
        "account_filter_mode": "Only selected accounts",
        "category_filter_mode": "Include values with any categories",
        "transfer_mode": "Exclude self-transfers",
        "subcategory_mode": "Show all",
        "status_not_cleared": True,
        "status_newly_cleared": False,
        "status_newly_reconciled": None,
        "status_reconciled": False,
    }
    mismatches = {
        field: (getattr(component, field), value)
        for field, value in expected.items()
        if getattr(component, field) != value
    }
    if mismatches:
        details = ", ".join(
            f"{field}={actual!r}, expected {expected!r}"
            for field, (actual, expected) in mismatches.items()
        )
        raise ValueError(f"unsupported Uncleared Transactions definition: {details}")


def reproduce_uncleared_transactions(
    connection: sqlite3.Connection,
) -> list[ReproducedTransactionRow]:
    """Compatibility wrapper for the generic type-4 renderer."""
    return reproduce_type4_transactions(connection, UNCLEARED_REPORT_NAME)


def _write_transaction_tsv(rows: list[ReproducedTransactionRow], destination: str | Path) -> int:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(
            [
                "Date",
                "Account",
                "Payee",
                "Category",
                "Memo",
                "Amount",
                "Transaction ID",
                "Split Line",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.transaction_date,
                    row.account,
                    row.payee,
                    row.category,
                    row.memo,
                    f"{row.amount:.2f}",
                    row.transaction_id,
                    row.split_line if row.split_line is not None else "",
                ]
            )
    return len(rows)


def write_grocery_expenses_tsv(connection: sqlite3.Connection, destination: str | Path) -> int:
    """Write a Quicken-like Grocery Expenses detail extract and return its row count."""
    return _write_transaction_tsv(reproduce_grocery_expenses(connection), destination)


def write_uncleared_transactions_tsv(
    connection: sqlite3.Connection, destination: str | Path
) -> int:
    """Write an Uncleared Transactions detail extract and return its row count."""
    return _write_transaction_tsv(reproduce_uncleared_transactions(connection), destination)


def reproduce_saved_report(database: str | Path, report_name: str, destination: str | Path) -> int:
    """Dispatch a saved report from the SQLite export by its report type."""
    with sqlite3.connect(database) as connection:
        component = _find_component(connection, report_name)
        renderer = get_report_renderer(component.report_type, component.name)
        return renderer.write(connection, component, destination)


class UnsupportedReportError(ValueError):
    """Raised when the renderer cannot prove a report semantic."""


@dataclass(frozen=True)
class EntityFilter:
    """A name/handle-backed include, exclude, or all-entities predicate."""

    mode: str
    names: frozenset[str]
    handles: frozenset[int]

    def matches(self, name: str, handle: int | None) -> bool:
        """Return whether one materialized entity passes this filter."""
        if self.mode == "all":
            return True
        selected = name in self.names or (handle is not None and handle in self.handles)
        return selected if self.mode == "include" else not selected


@dataclass(frozen=True)
class TransactionReportSpec:
    """Normalized, data-oriented semantics for one type-4 report."""

    report_name: str
    start_date: str
    end_date: str
    account_filter: EntityFilter
    category_filter: EntityFilter
    allowed_cleared: frozenset[str | None] | None
    transfer_mode: str
    subcategory_mode: str | None
    sort_by: str
    subtotal: str | None
    rounding_checked: bool


def _resolve_type4_entities(
    connection: sqlite3.Connection,
    component: QdbReportComponent,
    group_index: int,
    entity: str,
    strict: bool = True,
) -> tuple[frozenset[str], frozenset[int]]:
    """Resolve one SQLite report filter group to display names and handles."""
    table = "accounts" if entity == "account" else "categories"
    names: set[str] = set()
    handles: set[int] = set()
    group = component.report_filter.groups[group_index]
    stored_names = dict(group.entity_names)
    catalog_types = dict(group.catalog_types)
    for _, reference in group.values:
        row = connection.execute(
            f"SELECT name FROM {table} WHERE qdb_handle = ?", (reference,)
        ).fetchone()
        if row is not None:
            names.add(row[0])
            handles.add(reference)
            continue
        catalog_type = catalog_types.get(reference)
        belongs_to_entity = (entity == "category" and catalog_type in (0, 1)) or (
            entity == "account" and catalog_type not in (0, 1, None)
        )
        if belongs_to_entity and reference in stored_names:
            names.add(stored_names[reference])
            handles.add(reference)
            continue
        if catalog_type is not None:
            # The extracted catalog type proves this is a different entity
            # family, commonly caused by a shared QDF handle collision.
            continue
        if strict:
            raise UnsupportedReportError(
                f"{component.name!r}: unresolved {entity} filter handle {reference}"
            )
    return frozenset(names), frozenset(handles)


def _type4_category_names(connection: sqlite3.Connection) -> frozenset[str]:
    """Return all category names usable by a type-4 category complement."""
    return frozenset(
        row[0]
        for row in connection.execute("SELECT name FROM categories")
        if row[0] and not row[0].endswith(":ZZZZZ")
    )


def _type4_extract_date(connection: sqlite3.Connection, component: QdbReportComponent) -> date:
    """Resolve the report-relative end date from export metadata or data."""
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key = 'extract_date'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row or not row[0]:
        row = connection.execute("SELECT MAX(transaction_date) FROM transactions").fetchone()
    if not row or not row[0]:
        raise UnsupportedReportError(
            f"{component.name!r}: cannot resolve {component.date_range!r} without an extract date"
        )
    try:
        return date.fromisoformat(row[0])
    except ValueError as error:
        raise UnsupportedReportError(
            f"{component.name!r}: invalid extract date {row[0]!r}"
        ) from error


def _type4_date_bounds(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> tuple[str, str]:
    """Compile all currently decoded transaction date presets."""
    if component.date_range == "Custom range":
        if component.custom_start_date is None or component.custom_end_date is None:
            raise UnsupportedReportError(
                f"{component.name!r}: invalid custom date words {component.custom_date_words!r}"
            )
        return component.custom_start_date.isoformat(), component.custom_end_date.isoformat()

    end = _type4_extract_date(connection, component)
    if component.date_range == "Last 12 months":
        try:
            start = end.replace(year=end.year - 1)
        except ValueError:
            start = date(end.year - 1, 2, 28)
        return start.isoformat(), end.isoformat()
    if component.date_range == "Year to date":
        return date(end.year, 1, 1).isoformat(), end.isoformat()
    if component.date_range == "Last month":
        first_this_month = date(end.year, end.month, 1)
        last_previous_month = date.fromordinal(first_this_month.toordinal() - 1)
        return (
            date(last_previous_month.year, last_previous_month.month, 1).isoformat(),
            last_previous_month.isoformat(),
        )
    raise UnsupportedReportError(
        f"{component.name!r}: unsupported transaction date range {component.date_range!r}"
    )


def _type4_allowed_cleared(
    component: QdbReportComponent,
) -> frozenset[str | None] | None:
    """Compile decoded status checkboxes into SQLite cleared values."""
    if component.status_filter_words in ((0xFFFF, 0xFFFF), (0, 0)):
        return None
    values = (
        (None, component.status_not_cleared),
        ("c", component.status_newly_cleared),
        ("R", component.status_reconciled),
    )
    if all(selected is None for _, selected in values):
        raise UnsupportedReportError(
            f"{component.name!r}: unknown status filter words {component.status_filter_words!r}"
        )
    return frozenset(state for state, selected in values if selected is True)


def compile_type4_report(
    connection: sqlite3.Connection,
    component: QdbReportComponent,
) -> TransactionReportSpec:
    """Compile a decoded type-4 component into executable predicates."""
    if component.report_type != 4:
        raise UnsupportedReportError(
            f"{component.name!r} is report type {component.report_type}, not type 4"
        )
    start, end = _type4_date_bounds(connection, component)
    if component.transfer_mode not in ("Include all", "Exclude self-transfers"):
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported transfer setting "
            f"{component.transfer_mode!r} (raw {component.transfer_mode_code})"
        )
    valid_transfer_codes = {
        "Include all": {1},
        # Both raw values occur in validated type-4 signatures: the
        # Last-12-Months family uses 1, while the synthetic fixture uses 2.
        "Exclude self-transfers": {1, 2},
    }[component.transfer_mode]
    if component.transfer_mode_code not in valid_transfer_codes:
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported transfer setting: label {component.transfer_mode!r} "
            f"does not match raw code {component.transfer_mode_code}"
        )
    if component.subcategory_mode not in (None, "Show all"):
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported subcategory setting "
            f"{component.subcategory_mode!r} (raw {component.subcategory_mode_code})"
        )
    if component.subtotal not in (None, "Don't subtotal"):
        raise UnsupportedReportError(
            f"{component.name!r}: subtotal layout {component.subtotal!r} "
            "is not supported by the detail TSV renderer"
        )
    if component.sort_by not in (None, "Date/Account", "Account/Date"):
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported sort setting {component.sort_by!r}"
        )
    if component.rounding_checked is None:
        raise UnsupportedReportError(
            f"{component.name!r}: unknown rounding code {component.rounding_raw_code}"
        )
    if component.category_filter_mode not in (
        "Include values with any categories",
        "Include only selected categories",
    ):
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported category filter mode "
            f"{component.category_filter_mode!r}"
        )

    category_values = component.report_filter.groups[7].values
    account_names, account_handles = _resolve_type4_entities(connection, component, 6, "account")
    if category_values:
        category_names, category_handles = _resolve_type4_entities(
            connection, component, 7, "category"
        )
        category_filter = EntityFilter(
            "include",
            _type4_category_names(connection) - category_names,
            category_handles,
        )
    else:
        category_filter = EntityFilter("all", frozenset(), frozenset())

    if category_values:
        # For the observed type-4 family, populated group 6 is the account
        # complement paired with the populated category complement.
        account_filter = EntityFilter("exclude", account_names, account_handles)
    elif component.account_filter_mode == "Only selected accounts":
        account_filter = EntityFilter("include", account_names, account_handles)
    elif component.account_filter_mode in ("All accounts", "Include all accounts"):
        account_filter = EntityFilter("all", frozenset(), frozenset())
    else:
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported account filter mode {component.account_filter_mode!r}"
        )
    return TransactionReportSpec(
        report_name=component.name,
        start_date=start,
        end_date=end,
        account_filter=account_filter,
        category_filter=category_filter,
        allowed_cleared=_type4_allowed_cleared(component),
        transfer_mode=component.transfer_mode,
        subcategory_mode=component.subcategory_mode,
        sort_by=component.sort_by or "Date/Account",
        subtotal=component.subtotal,
        rounding_checked=component.rounding_checked,
    )


def _type4_category_matches(category: str, selection: EntityFilter) -> bool:
    """Apply a normalized category filter using exact category names."""
    if selection.mode == "all":
        return True
    selected = bool(category) and category in selection.names
    return selected if selection.mode == "include" else not selected


def _render_type4_transactions(
    connection: sqlite3.Connection, spec: TransactionReportSpec
) -> list[ReproducedTransactionRow]:
    """Apply a compiled type-4 specification to materialized transactions."""
    rows: list[ReproducedTransactionRow] = []
    transactions = connection.execute(
        """
        SELECT t.id, t.transaction_date, COALESCE(a.name, ''),
               COALESCE(t.payee, ''), COALESCE(t.category, ''),
               COALESCE(t.memo, ''), t.amount, t.cleared, a.qdb_handle,
               COALESCE(t.transfer_account, '')
        FROM transactions AS t
        LEFT JOIN accounts AS a ON a.id = t.account_id
        WHERE t.transaction_date BETWEEN ? AND ?
        """,
        (spec.start_date, spec.end_date),
    )
    for (
        transaction_id,
        date_value,
        account,
        payee,
        category,
        memo,
        amount,
        cleared,
        account_handle,
        transfer,
    ) in transactions:
        if not spec.account_filter.matches(account, account_handle):
            continue
        if spec.allowed_cleared is not None and cleared not in spec.allowed_cleared:
            continue
        splits = connection.execute(
            """
            SELECT line_number, COALESCE(category, ''), COALESCE(memo, ''), amount,
                   COALESCE(transfer_account, '')
            FROM transaction_splits
            WHERE transaction_id = ?
            ORDER BY line_number
            """,
            (transaction_id,),
        ).fetchall()
        if splits:
            for line_number, split_category, split_memo, split_amount, split_transfer in splits:
                if spec.transfer_mode == "Exclude self-transfers" and split_transfer:
                    continue
                if not _type4_category_matches(split_category, spec.category_filter):
                    continue
                rows.append(
                    ReproducedTransactionRow(
                        transaction_id=transaction_id,
                        split_line=line_number,
                        transaction_date=date_value,
                        account=account,
                        payee=payee,
                        category=split_category,
                        memo=split_memo or memo,
                        amount=_decimal(split_amount),
                    )
                )
            continue
        if spec.transfer_mode == "Exclude self-transfers" and transfer:
            continue
        if not _type4_category_matches(category, spec.category_filter):
            continue
        rows.append(
            ReproducedTransactionRow(
                transaction_id=transaction_id,
                split_line=None,
                transaction_date=date_value,
                account=account,
                payee=payee,
                category=category,
                memo=memo,
                amount=_decimal(amount),
            )
        )
    split_sentinel = -1
    if spec.sort_by == "Account/Date":
        rows.sort(
            key=lambda row: (
                row.account,
                row.transaction_date,
                row.transaction_id,
                row.split_line if row.split_line is not None else split_sentinel,
            )
        )
    else:
        rows.sort(
            key=lambda row: (
                row.transaction_date,
                row.account,
                row.transaction_id,
                row.split_line if row.split_line is not None else split_sentinel,
            )
        )
    return rows


def reproduce_type4_transactions(
    connection: sqlite3.Connection, report_name: str
) -> list[ReproducedTransactionRow]:
    """Render any supported type-4 component by its SQLite-decoded parameters."""
    component = _find_component(connection, report_name)
    return _render_type4_transactions(
        connection,
        compile_type4_report(connection, component),
    )


@dataclass(frozen=True)
class ReportTable:
    """Tabular renderer result written as a portable TSV."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


def _write_report_table(table: ReportTable, destination: str | Path) -> int:
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(table.columns)
        for row in table.rows:
            writer.writerow(
                "" if value is None else f"{value:.2f}" if isinstance(value, Decimal) else value
                for value in row
            )
    return len(table.rows)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _report_account_filter(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> EntityFilter:
    if component.account_filter_mode in ("All accounts", "Include all accounts"):
        return EntityFilter("all", frozenset(), frozenset())
    if component.account_filter_mode != "Only selected accounts":
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported account filter mode {component.account_filter_mode!r}"
        )
    names, handles = _resolve_type4_entities(connection, component, 6, "account", strict=False)
    return EntityFilter("include", names, handles)


def _report_category_filter(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> EntityFilter:
    values = component.report_filter.groups[7].values
    mode = component.category_filter_mode
    if not values or mode in ("Include all categories", "Include values with any categories"):
        return EntityFilter("all", frozenset(), frozenset())
    names, handles = _resolve_type4_entities(connection, component, 7, "category", strict=False)
    if mode in ("All categories except selected",):
        return EntityFilter("exclude", names, handles)
    if mode in (
        "Include only selected categories",
        "Include only transactions with selected categories",
        "Selected categories only",
    ):
        return EntityFilter("include", names, handles)
    raise UnsupportedReportError(f"{component.name!r}: unsupported category filter mode {mode!r}")


def _report_period(component: QdbReportComponent, date_value: str) -> str:
    if component.interval == "Month":
        return date_value[:7]
    if component.interval == "Year" or component.header_setting == "Year":
        return date_value[:4]
    return "Total"


def _report_category_name(category: str, component: QdbReportComponent) -> str:
    if component.subcategory_mode == "Hide all" and ":" in category:
        return category.split(":", 1)[0]
    return category or "(Uncategorized)"


def _report_entries(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> list[tuple[str, str, str, str, Decimal]]:
    """Materialize filtered transaction or split entries for summary reports."""
    start, end = _type4_date_bounds(connection, component)
    account_filter = _report_account_filter(connection, component)
    category_filter = _report_category_filter(connection, component)
    allowed_cleared = (
        _type4_allowed_cleared(component) if component.report_type in (7, 14, 29, 32) else None
    )
    if component.transfer_mode not in (
        None,
        "Include all",
        "Exclude self-transfers",
        "Exclude internal",
        "Exclude all",
    ):
        raise UnsupportedReportError(
            f"{component.name!r}: unsupported transfer setting {component.transfer_mode!r}"
        )
    result: list[tuple[str, str, str, str, Decimal]] = []
    transactions = connection.execute(
        """
        SELECT t.id, t.transaction_date, COALESCE(a.name, ''),
               COALESCE(t.payee, ''), COALESCE(t.category, ''),
               COALESCE(t.memo, ''), t.amount, t.cleared, a.qdb_handle,
               COALESCE(t.transfer_account, '')
        FROM transactions AS t
        LEFT JOIN accounts AS a ON a.id = t.account_id
        WHERE t.transaction_date BETWEEN ? AND ?
        """,
        (start, end),
    )
    for (
        transaction_id,
        date_value,
        account,
        payee,
        category,
        memo,
        amount,
        cleared,
        account_handle,
        transfer,
    ) in transactions:
        if not account_filter.matches(account, account_handle):
            continue
        if allowed_cleared is not None and cleared not in allowed_cleared:
            continue
        splits = connection.execute(
            """
            SELECT COALESCE(category, ''), COALESCE(memo, ''), amount,
                   COALESCE(transfer_account, '')
            FROM transaction_splits
            WHERE transaction_id = ?
            ORDER BY line_number
            """,
            (transaction_id,),
        ).fetchall()
        entries = splits or [(category, memo, amount, transfer)]
        for entry_category, entry_memo, entry_amount, entry_transfer in entries:
            if component.transfer_mode != "Include all" and entry_transfer:
                if component.transfer_mode in (
                    "Exclude self-transfers",
                    "Exclude internal",
                    "Exclude all",
                ):
                    continue
            if not category_filter.matches(entry_category, None):
                continue
            result.append(
                (
                    date_value,
                    account,
                    payee,
                    _report_category_name(entry_category, component),
                    _decimal(entry_amount),
                )
            )
    return result


def _render_itemized(
    connection: sqlite3.Connection,
    component: QdbReportComponent,
    field: str,
) -> ReportTable:
    totals: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for date_value, _, payee, category, amount in _report_entries(connection, component):
        label = payee or "(No payee)" if field == "payee" else category
        key = (_report_period(component, date_value), label)
        previous_amount, previous_count = totals.get(key, (Decimal(0), 0))
        totals[key] = (previous_amount + amount, previous_count + 1)
    rows = tuple(
        (period, label, amount, count)
        for (period, label), (amount, count) in sorted(totals.items())
    )
    return ReportTable(("Period", field.title(), "Amount", "Transaction Count"), rows)


def _render_cash_flow(connection: sqlite3.Connection, component: QdbReportComponent) -> ReportTable:
    totals: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for date_value, _, _, category, amount in _report_entries(connection, component):
        period = _report_period(component, date_value)
        inflow = amount if amount > 0 else Decimal(0)
        outflow = -amount if amount < 0 else Decimal(0)
        old_inflow, old_outflow = totals.get((period, category), (Decimal(0), Decimal(0)))
        totals[(period, category)] = (old_inflow + inflow, old_outflow + outflow)
    rows = tuple(
        (period, category, inflow, outflow, inflow - outflow)
        for (period, category), (inflow, outflow) in sorted(totals.items())
    )
    return ReportTable(("Period", "Category", "Inflows", "Outflows", "Net"), rows)


def _render_expense_summary(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> ReportTable:
    totals: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for date_value, _, _, category, amount in _report_entries(connection, component):
        if amount >= 0:
            continue
        key = (_report_period(component, date_value), category)
        old_amount, old_count = totals.get(key, (Decimal(0), 0))
        totals[key] = (old_amount - amount, old_count + 1)
    rows = tuple(
        (period, category, amount, count)
        for (period, category), (amount, count) in sorted(totals.items())
    )
    return ReportTable(("Period", "Category", "Amount", "Transaction Count"), rows)


def _render_balance_report(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> ReportTable:
    start, end = _type4_date_bounds(connection, component)
    account_filter = _report_account_filter(connection, component)
    latest: dict[tuple[str, str], tuple[str, str, Decimal]] = {}
    if _table_exists(connection, "banking_account_balance_periods"):
        rows = connection.execute(
            """
            SELECT a.name, COALESCE(a.account_type, ''), p.balance_date,
                   p.balance_cents, a.qdb_handle
            FROM banking_account_balance_periods AS p
            JOIN accounts AS a ON a.id = p.account_id
            WHERE p.balance_date BETWEEN ? AND ?
            """,
            (start, end),
        )
        for account, account_type, balance_date, cents, handle in rows:
            if account_filter.matches(account, handle):
                key = (_report_period(component, balance_date), account)
                if key not in latest or balance_date > latest[key][0]:
                    latest[key] = (balance_date, account_type, Decimal(cents) / Decimal(100))
    if _table_exists(connection, "investment_account_balance_periods"):
        rows = connection.execute(
            """
            SELECT a.name, COALESCE(a.account_type, ''), p.balance_date,
                   p.total_value, a.qdb_handle
            FROM investment_account_balance_periods AS p
            JOIN accounts AS a ON a.id = p.account_id
            WHERE p.balance_date BETWEEN ? AND ?
            """,
            (start, end),
        )
        for account, account_type, balance_date, total_value, handle in rows:
            if account_filter.matches(account, handle):
                key = (_report_period(component, balance_date), account)
                if key not in latest or balance_date > latest[key][0]:
                    latest[key] = (balance_date, account_type, _decimal(total_value))
    if component.report_type == 1:
        totals: dict[str, Decimal] = {}
        for (period, _), (_, _, balance) in latest.items():
            totals[period] = totals.get(period, Decimal(0)) + balance
        return ReportTable(
            ("Period", "Net Worth"),
            tuple((period, balance) for period, balance in sorted(totals.items())),
        )
    return ReportTable(
        ("Period", "Account", "Account Type", "Balance"),
        tuple(
            (period, account, account_type, balance)
            for (period, account), (_, account_type, balance) in sorted(latest.items())
        ),
    )


def _budget_entity(connection: sqlite3.Connection, entity_qid: int) -> tuple[str, str]:
    """Resolve a budget item to its persisted category/account label."""
    category = connection.execute(
        "SELECT name FROM categories WHERE qdb_handle = ?", (entity_qid,)
    ).fetchone()
    if category is not None:
        return "category", category[0]
    account = connection.execute(
        "SELECT name FROM accounts WHERE qdb_handle = ?", (entity_qid,)
    ).fetchone()
    if account is not None:
        return "account", account[0]
    return "unknown", f"#{entity_qid}"


def _render_budget(connection: sqlite3.Connection, component: QdbReportComponent) -> ReportTable:
    """Render budget rows without inventing unavailable budget calculations."""
    start, end = _type4_date_bounds(connection, component)
    query = """
        SELECT b.budget_qid, b.year, b.month, b.budget_name, b.item_index,
               b.category_qid, b.flags, b.budget_amount, b.secondary_amount
        FROM budgets AS b
        WHERE date(b.year || '-' || printf('%02d', b.month) || '-01')
              BETWEEN date(?) AND date(?)
    """
    parameters: tuple[object, ...] = (start, end)
    if component.budget is not None:
        query += " AND b.budget_name = ?"
        parameters += (component.budget,)
    query += " ORDER BY b.year, b.month, b.budget_name, b.item_index"
    rows = []
    for (
        budget_qid,
        year,
        month,
        budget_name,
        item_index,
        entity_qid,
        flags,
        budget_amount,
        secondary_amount,
    ) in connection.execute(query, parameters):
        entity_kind, entity_name = _budget_entity(connection, entity_qid)
        rows.append(
            (
                f"{year:04d}-{month:02d}",
                budget_qid,
                budget_name,
                entity_qid,
                entity_kind,
                entity_name,
                item_index,
                flags,
                _decimal(budget_amount),
                _decimal(secondary_amount),
                bool(flags & 0x0800),
            )
        )
    return ReportTable(
        (
            "Period",
            "Budget QID",
            "Budget",
            "Entity QID",
            "Entity Kind",
            "Entity Name",
            "Item Index",
            "Flags",
            "Budget Amount",
            "Secondary Amount",
            "Rollover Enabled",
        ),
        tuple(rows),
    )


def _render_tag_report(
    connection: sqlite3.Connection, component: QdbReportComponent
) -> ReportTable:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
    if "tag" not in columns:
        raise UnsupportedReportError(
            f"{component.name!r}: type 55 requires an exported transactions.tag column "
            "or transaction_tags table"
        )
    start, end = _type4_date_bounds(connection, component)
    rows = connection.execute(
        """
        SELECT t.transaction_date, COALESCE(t.tag, '(No tag)'), t.amount
        FROM transactions AS t
        WHERE t.transaction_date BETWEEN ? AND ?
        ORDER BY t.transaction_date, t.tag
        """,
        (start, end),
    ).fetchall()
    totals: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for date_value, tag, amount in rows:
        key = (_report_period(component, date_value), tag)
        old_amount, old_count = totals.get(key, (Decimal(0), 0))
        totals[key] = (old_amount + _decimal(amount), old_count + 1)
    return ReportTable(
        ("Period", "Tag", "Amount", "Transaction Count"),
        tuple(
            (period, tag, amount, count)
            for (period, tag), (amount, count) in sorted(totals.items())
        ),
    )


class BalanceRenderer:
    """Render report types 1 and 2 from normalized balance periods."""

    output_format = "tsv"

    def __init__(self, report_type: int):
        """Create a renderer for one decoded report type."""
        self.report_type = report_type

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the balance table and return its row count."""
        return _write_report_table(_render_balance_report(connection, component), destination)


class ItemizedCategoryRenderer:
    """Render report type 7 as category totals."""

    report_type = 7
    output_format = "tsv"

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the category table and return its row count."""
        return _write_report_table(_render_itemized(connection, component, "category"), destination)


class CashFlowRenderer:
    """Render report types 13 and 29 as inflow/outflow tables."""

    output_format = "tsv"

    def __init__(self, report_type: int):
        """Create a renderer for one decoded report type."""
        self.report_type = report_type

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the cash-flow table and return its row count."""
        return _write_report_table(_render_cash_flow(connection, component), destination)


class ExpenseSummaryRenderer:
    """Render report type 14 as positive expense totals."""

    report_type = 14
    output_format = "tsv"

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the expense table and return its row count."""
        return _write_report_table(_render_expense_summary(connection, component), destination)


class BudgetRenderer:
    """Render report type 20 as lossless budget-detail tables."""

    report_type = 20
    output_format = "tsv"

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the report table and return its row count."""
        if not _table_exists(connection, "budgets"):
            raise UnsupportedReportError(f"{component.name!r}: SQLite export has no budgets table")
        return _write_report_table(_render_budget(connection, component), destination)


class SpendingRenderer:
    """Render report type 32 as positive spending totals."""

    report_type = 32
    output_format = "tsv"

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the expense table and return its row count."""
        return _write_report_table(_render_expense_summary(connection, component), destination)


class ItemizedPayeeRenderer:
    """Render report type 47 as payee totals."""

    report_type = 47
    output_format = "tsv"

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the payee table and return its row count."""
        return _write_report_table(_render_itemized(connection, component, "payee"), destination)


class ItemizedTagRenderer:
    """Render report type 55 when tag data was exported."""

    report_type = 55
    output_format = "tsv"

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Render the report table and return its row count."""
        """Render the tag table and return its row count."""
        return _write_report_table(_render_tag_report(connection, component), destination)


class TransactionDetailRenderer:
    """Render report type 4 as a detail TSV."""

    report_type = 4
    output_format = "tsv"

    def write(
        self,
        connection: sqlite3.Connection,
        component: QdbReportComponent,
        destination: str | Path,
    ) -> int:
        """Compile and write one transaction-detail report."""
        if component.report_type != self.report_type:
            raise UnsupportedReportError(
                f"{component.name!r} is report type {component.report_type}, "
                f"not type {self.report_type}"
            )
        spec = compile_type4_report(connection, component)
        return _write_transaction_tsv(_render_type4_transactions(connection, spec), destination)


REPORT_RENDERERS: dict[int, ReportRenderer] = {
    1: BalanceRenderer(1),
    2: BalanceRenderer(2),
    4: TransactionDetailRenderer(),
    7: ItemizedCategoryRenderer(),
    13: CashFlowRenderer(13),
    14: ExpenseSummaryRenderer(),
    20: BudgetRenderer(),
    29: CashFlowRenderer(29),
    32: SpendingRenderer(),
    47: ItemizedPayeeRenderer(),
    55: ItemizedTagRenderer(),
}


def get_report_renderer(report_type: int, report_name: str | None = None) -> ReportRenderer:
    """Return the registered renderer or fail with a report-type explanation."""
    renderer = REPORT_RENDERERS.get(report_type)
    if renderer is not None:
        return renderer
    name = f" ({report_type_name(report_type)})" if report_type else ""
    report_label = f" for {report_name!r}" if report_name else ""
    registered = ", ".join(str(value) for value in sorted(REPORT_RENDERERS))
    raise UnsupportedReportError(
        f"no renderer registered for report type {report_type}{name}{report_label}; "
        f"registered report types: {registered or 'none'}"
    )


def supported_report_types() -> tuple[int, ...]:
    """Return report types with an implemented renderer, in numeric order."""
    return tuple(sorted(REPORT_RENDERERS))


__all__ = [
    "GROCERY_REPORT_NAME",
    "REPORT_RENDERERS",
    "REPORT_TYPE_RULES",
    "UNCLEARED_REPORT_NAME",
    "BalanceRenderer",
    "BudgetRenderer",
    "CashFlowRenderer",
    "EntityFilter",
    "ExpenseSummaryRenderer",
    "ItemizedCategoryRenderer",
    "ItemizedPayeeRenderer",
    "ItemizedTagRenderer",
    "ReportRenderer",
    "ReportTable",
    "ReproducedTransactionRow",
    "SpendingRenderer",
    "TransactionDetailRenderer",
    "TransactionReportSpec",
    "UnsupportedReportError",
    "compile_type4_report",
    "get_report_renderer",
    "reproduce_grocery_expenses",
    "reproduce_saved_report",
    "reproduce_type4_transactions",
    "reproduce_uncleared_transactions",
    "supported_report_types",
    "write_grocery_expenses_tsv",
    "write_uncleared_transactions_tsv",
]
