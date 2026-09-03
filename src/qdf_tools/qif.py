"""Small, loss-aware QIF reader used as the transaction export bridge."""

from __future__ import annotations

import datetime as dt
import decimal
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class QifSplit:
    """Represent one category or transfer split within a QIF transaction."""

    category: str | None = None
    memo: str | None = None
    amount: decimal.Decimal | None = None
    raw: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class QifTransaction:
    """Represent a normalized QIF transaction and its preserved raw fields."""

    account: str | None = None
    account_type: str | None = None
    section: str = "Bank"
    date: dt.date | None = None
    amount: decimal.Decimal | None = None
    payee: str | None = None
    downloaded_payee: str | None = None
    payee_source: str | None = None
    memo: str | None = None
    number: str | None = None
    fit_id: str | None = None
    security: str | None = None
    price: decimal.Decimal | None = None
    quantity: decimal.Decimal | None = None
    commission: decimal.Decimal | None = None
    category: str | None = None
    tag: str | None = None
    cleared: str | None = None
    address: list[str] = field(default_factory=list)
    splits: list[QifSplit] = field(default_factory=list)
    raw: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class QifAccount:
    """Represent an account catalog entry parsed from a QIF file."""

    name: str
    account_type: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class QifCategory:
    """Represent a category catalog entry parsed from a QIF file."""

    name: str
    description: str | None = None


@dataclass
class QifData:
    """Collect QIF transactions, accounts, and categories from one file."""

    transactions: list[QifTransaction] = field(default_factory=list)
    accounts: list[QifAccount] = field(default_factory=list)
    categories: list[QifCategory] = field(default_factory=list)


def parse_amount(value: str) -> decimal.Decimal:
    """Parse common QIF amounts while preserving cents exactly."""
    text = value.strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    text = re.sub(r"[^0-9+\-.]", "", text)
    if not text or text in {"+", "-", "."}:
        raise ValueError(f"invalid QIF amount: {value!r}")
    return decimal.Decimal(text)


def parse_date(value: str) -> dt.date:
    """Parse Quicken date variants into a standard ``datetime.date``."""
    text = value.strip().split(" ", 1)[0]
    # Quicken's native QIF writer pads single-digit month/day/year fields,
    # e.g. ``1/ 1'26``.  The spaces are not significant.
    compact = re.fullmatch(r"\s*(\d{1,2})/\s*(\d{1,2})'\s*(\d{1,2})\s*", value)
    if compact:
        month, day, year = (int(part) for part in compact.groups())
        year += 2000 if year < 70 else 1900
        return dt.date(year, month, day)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m/%d'%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            result = dt.datetime.strptime(text, fmt).date()
            if fmt in {"%m/%d/%y", "%m/%d'%y"} and result.year < 1970:
                result = result.replace(year=result.year + 100)
            return result
        except ValueError:
            continue
    raise ValueError(f"invalid QIF date: {value!r}")


def _append(raw: dict[str, list[str]], code: str, value: str) -> None:
    raw.setdefault(code, []).append(value)


def _finish(
    record: dict[str, list[str]],
    account: str | None,
    account_type: str | None,
    section: str,
) -> QifTransaction | None:
    if not record:
        return None

    def first(code: str) -> str | None:
        values = record.get(code, [])
        return values[0] if values else None

    category = first("L")
    tag = None
    if category and "/" in category:
        category, tag = category.split("/", 1)
        category = category or None
        tag = tag or None
    transaction = QifTransaction(
        account=account,
        account_type=account_type,
        section=section,
        date=parse_date(first("D")) if first("D") else None,
        amount=parse_amount(first("T")) if first("T") else None,
        payee=first("P"),
        memo=first("M"),
        number=first("N"),
        security=first("Y"),
        price=parse_amount(first("I")) if first("I") else None,
        quantity=parse_amount(first("Q")) if first("Q") else None,
        commission=parse_amount(first("O")) if first("O") else None,
        category=category,
        tag=tag,
        cleared=first("C"),
        address=record.get("A", []),
        raw=record,
    )
    split_categories = record.get("S", [])
    split_memos = record.get("E", [])
    split_amounts = record.get("$", [])
    for index, category in enumerate(split_categories):
        transaction.splits.append(
            QifSplit(
                category=category or None,
                memo=split_memos[index] if index < len(split_memos) else None,
                amount=parse_amount(split_amounts[index]) if index < len(split_amounts) else None,
            )
        )
    return transaction


def parse_qif(source: str | Path) -> list[QifTransaction]:
    """Parse QIF transactions, retaining unknown/repeated fields in ``raw``."""
    return parse_qif_data(source).transactions


QIF_TRANSACTION_SECTIONS = {"bank", "cash", "ccard", "invst", "oth a", "oth l"}
QIF_ACCOUNT_TYPES = {
    "bank": "Banking",
    "cash": "Cash",
    "ccard": "Credit Card",
    "invst": "Investing",
    "oth a": "Asset",
    "oth l": "Loan",
}


def parse_qif_data(source: str | Path) -> QifData:
    """Parse QIF transactions plus account and category catalogs.

    Quicken exports account and category records alongside transactions, and
    may also include tags, securities, and price-history sections.  Only the
    bank-like sections contain transactions; the other sections are metadata
    and must not be interpreted as transaction fields.
    """
    lines = Path(source).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    account: str | None = None
    account_type: str | None = None
    section = ""
    record: dict[str, list[str]] = {}
    data = QifData()
    accounts_by_name: dict[str, QifAccount] = {}
    categories_by_name: dict[str, QifCategory] = {}

    def finish_record() -> None:
        nonlocal account, account_type, record
        if section.lower() == "account":
            name = (record.get("N") or record.get("A") or [None])[0]
            if name:
                qif_type = (record.get("T") or [None])[0]
                normalized_type = QIF_ACCOUNT_TYPES.get((qif_type or "").lower(), qif_type)
                accounts_by_name[name] = QifAccount(
                    name=name,
                    account_type=normalized_type,
                    description=(record.get("D") or [None])[0],
                )
            record = {}
            return
        if section.lower() == "cat":
            name = (record.get("N") or [None])[0]
            if name:
                categories_by_name[name] = QifCategory(
                    name=name,
                    description=(record.get("D") or [None])[0],
                )
        elif section.lower() in QIF_TRANSACTION_SECTIONS:
            transaction = _finish(record, account, account_type, section)
            if transaction is not None:
                data.transactions.append(transaction)
        record = {}

    for line in lines:
        line = line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("!"):
            finish_record()
            header = line[1:].strip()
            if header.lower() == "account":
                section = "Account"
            elif header.lower().startswith("type:"):
                section = header.split(":", 1)[1].strip()
                if section.lower() in QIF_TRANSACTION_SECTIONS:
                    account_type = QIF_ACCOUNT_TYPES.get(section.lower())
                else:
                    account_type = None
            else:
                section = ""
            continue
        if line == "^":
            if section.lower() == "account":
                name = (record.get("N") or record.get("A") or [account])[0]
                account = name or account
                qif_type = (record.get("T") or [None])[0]
                account_type = QIF_ACCOUNT_TYPES.get((qif_type or "").lower(), qif_type)
            finish_record()
            continue
        if len(line) >= 1:
            # QIF uses one-character field codes followed directly by the value;
            # a colon is not part of the field syntax.
            _append(record, line[0], line[1:])
            if line[0] == "A" and section.lower() == "account" and account is None:
                account = line[1:]
    finish_record()
    data.accounts = list(accounts_by_name.values())
    data.categories = list(categories_by_name.values())
    return data
