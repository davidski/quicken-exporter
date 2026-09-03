#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["olefile==0.47"]
# ///
"""Reader for the newer embedded Quicken price-history stream."""

from __future__ import annotations

import datetime as dt
import struct
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import olefile

QPH_HEADER = b"\x01\x00\x00\x00"
QPH_PREFIX_SIZE = 36
QPH_HEADER_SIZE = 51
QPH_RECORD_SIZE = 54


@dataclass(frozen=True)
class QphQuote:
    """One quote row decoded from the embedded QPH stream."""

    symbol: str
    price_date: dt.date
    price: str
    high: str | None
    low: str | None
    volume: int | None
    record_type: int


def _qph_date(word: int) -> dt.date | None:
    year = 1900 + ((word >> 16) & 0xFF)
    month = (word >> 8) & 0xFF
    day = word & 0xFF
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _price_value(value: int) -> Decimal | None:
    low = value & 0xFFFFFFFF
    high = (value >> 32) & 0xFFFFFFFF
    if high < 0x80000000:
        return None
    return Decimal(low) + Decimal(high - 0x80000000) / Decimal(100000000)


def _price_text(value: Decimal | None, *, optional: bool = False) -> str | None:
    if value is None or (optional and value == 0):
        return None
    text = format(value, "f").rstrip("0").rstrip(".")
    return text or "0"


def _symbol_headers(data: bytes, symbols: set[str]) -> list[tuple[int, str]]:
    headers: list[tuple[int, str]] = []
    start = 0
    while True:
        position = data.find(QPH_HEADER, start)
        if position < 0 or position + QPH_HEADER_SIZE > len(data):
            break
        raw_symbol = data[position + 4 : position + 36]
        if b"\x00" in raw_symbol:
            encoded, padding = raw_symbol.split(b"\x00", 1)
            if not padding.strip(b"\x00"):
                symbol = encoded.decode("cp1252", errors="replace").strip()
                if symbol in symbols:
                    headers.append((position, symbol))
        start = position + 4
    return headers


def _parse_block(data: bytes, start: int, end: int, symbol: str) -> list[QphQuote]:
    quotes: list[QphQuote] = []
    length = end - start
    for offset in range(start, start + length - QPH_RECORD_SIZE + 1, QPH_RECORD_SIZE):
        record = data[offset : offset + QPH_RECORD_SIZE]
        price_date = _qph_date(struct.unpack_from("<I", record, 0)[0])
        if price_date is None:
            continue
        price = _price_value(struct.unpack_from("<Q", record, 14)[0])
        if price is None or price == 0:
            continue
        high = _price_value(struct.unpack_from("<Q", record, 22)[0])
        low = _price_value(struct.unpack_from("<Q", record, 30)[0])
        volume = struct.unpack_from("<i", record, 38)[0]
        quotes.append(
            QphQuote(
                symbol=symbol,
                price_date=price_date,
                price=_price_text(price) or "0",
                high=_price_text(high, optional=True),
                low=_price_text(low, optional=True),
                volume=volume or None,
                record_type=struct.unpack_from("<I", record, 4)[0],
            )
        )
    return quotes


def parse_qph_bytes(data: bytes, symbols: set[str]) -> list[QphQuote]:
    """Decode quote rows for the requested security symbols.

    The symbol index record names the data block immediately before it.  The
    first block starts after the 36-byte stream prefix; later blocks start 51
    bytes after the preceding symbol index record.
    """
    if len(data) < QPH_PREFIX_SIZE:
        raise ValueError("QPH stream is truncated")
    headers = _symbol_headers(data, symbols)
    if not headers:
        return []

    quotes: list[QphQuote] = []
    block_start = QPH_PREFIX_SIZE
    for index, (header_position, symbol) in enumerate(headers):
        if header_position >= block_start:
            quotes.extend(_parse_block(data, block_start, header_position, symbol))
        block_start = header_position + QPH_HEADER_SIZE
    return quotes


def read_qph_stream(path: str | Path) -> bytes:
    """Read the embedded ``.QPH`` stream from a structured QDF file."""
    with olefile.OleFileIO(str(path)) as qdf:
        try:
            return qdf.openstream([".QPH"]).read()
        except OSError as error:
            raise ValueError(f"QDF does not contain an embedded .QPH stream: {path}") from error
