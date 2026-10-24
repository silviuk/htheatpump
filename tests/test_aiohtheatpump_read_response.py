#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#  htheatpump - Serial communication module for Heliotherm heat pumps
#  Copyright (C) 2023  Daniel Strigl

#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.

""" Tests for `AioHtHeatpump.read_response_async` using a mocked TCP stream (no heat pump needed). """

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from htheatpump.aiohtheatpump import AioHtHeatpump
from htheatpump.protocol import RESPONSE_HEADER, calc_checksum

HEADER_00 = b"\x02\xfd\xe0\xd0\x00\x00"
HEADER_01 = b"\x02\xfd\xe0\xd0\x01\x00"
HEADER_02 = b"\x02\xfd\xe0\xd0\x02\x00"
HEADER_04 = b"\x02\xfd\xe0\xd0\x04\x00"
HEADER_08 = b"\x02\xfd\xe0\xd0\x08\x00"

# answer of the heat pump on "MR,...,41" for MP 41 ("BSZ Verdichter Schaltungen") as observed
#   on 2026-10-24: header b"\x02\xfd\xe0\xd0\x01\x00" with payload length byte 0x00;
#   payload and checksum byte were not logged, so the payload is reconstructed from the
#   individual "MP,NR=41" answer (VAL=16567) in the format of the other "MA" answers
MP41_PAYLOAD = b"~MA,41,16567,2;\r\n"


def make_hp(data: bytes) -> AioHtHeatpump:
    """Create an `AioHtHeatpump` instance with a mocked (already "open") TCP stream."""
    hp = AioHtHeatpump(url="tcp://127.0.0.1:9999")
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    writer = MagicMock()
    writer.is_closing.return_value = False
    writer.wait_closed = AsyncMock()
    writer.drain = AsyncMock()
    hp._reader = reader
    hp._writer = writer
    return hp


def make_response(header: bytes, payload: bytes, len_byte: Optional[int] = None, checksum: Optional[int] = None) -> bytes:
    """Build a raw response; by default the length byte is len(payload) and the checksum the "correct" one."""
    if len_byte is None:
        len_byte = len(payload)
    if checksum is None:
        counted_len = len(payload) if len_byte == 0 else len_byte
        checksum = RESPONSE_HEADER[header]["checksum"](
            header, RESPONSE_HEADER[header]["payload_len"](counted_len), payload
        )
    return header + bytes([len_byte]) + payload + bytes([checksum])


class TestReadResponseAsync:
    @pytest.mark.asyncio
    async def test_zero_length_header_01_valid_checksum(self) -> None:
        # checksum computed with the counted (actual) payload length - 1
        exp_checksum = calc_checksum(HEADER_01 + bytes([len(MP41_PAYLOAD) - 1]) + MP41_PAYLOAD)
        hp = make_hp(make_response(HEADER_01, MP41_PAYLOAD, len_byte=0, checksum=exp_checksum))
        assert await hp.read_response_async() == "MA,41,16567,2"

    @pytest.mark.asyncio
    async def test_zero_length_header_01_invalid_checksum_raises_IOError(self) -> None:
        good = calc_checksum(HEADER_01 + bytes([len(MP41_PAYLOAD) - 1]) + MP41_PAYLOAD)
        hp = make_hp(make_response(HEADER_01, MP41_PAYLOAD, len_byte=0, checksum=(good + 1) & 0xFF))
        with pytest.raises(IOError, match="invalid checksum"):
            await hp.read_response_async()

    @pytest.mark.asyncio
    async def test_zero_length_header_00(self) -> None:
        payload = b"~ERR,INVALID IDX;\r\n"
        exp_checksum = calc_checksum(HEADER_00 + bytes([len(payload)]) + payload)
        hp = make_hp(make_response(HEADER_00, payload, len_byte=0, checksum=exp_checksum))
        assert await hp.read_response_async() == "ERR,INVALID IDX"

    @pytest.mark.asyncio
    async def test_zero_length_header_02(self) -> None:
        payload = b"~ERR,INVALID IDX;\r\n"
        exp_checksum = calc_checksum(HEADER_02 + bytes([len(payload) - 2]) + payload)
        hp = make_hp(make_response(HEADER_02, payload, len_byte=0, checksum=exp_checksum))
        assert await hp.read_response_async() == "ERR,INVALID IDX"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "header, payload, exp_checksum",
        [
            (HEADER_00, b"~MA,0,10.6,2;\r\n", 0xCB),  # captured from a real heat pump (2026-10-24)
            (HEADER_00, b"~OK;\r\n", 0x91),  # captured from a real heat pump (2026-10-24)
            (HEADER_01, b"~MA,0,10.6,2;\r\n", None),
            (HEADER_02, b"~MA,0,10.6,2;\r\n", None),
            (HEADER_04, b"~SP,NR=0,VAL=1;\r\n", 0x00),
            (HEADER_08, b"~SP,NR=0,VAL=1;\r\n", 0x00),
        ],
    )
    async def test_normal_length_all_headers(self, header: bytes, payload: bytes, exp_checksum: Optional[int]) -> None:
        if exp_checksum is None:
            corr = {HEADER_01: 1, HEADER_02: 2}[header]
            exp_checksum = calc_checksum(header + bytes([len(payload) - corr]) + payload)
        hp = make_hp(make_response(header, payload, checksum=exp_checksum))
        assert await hp.read_response_async() == payload.decode("ascii")[1:-3]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", [HEADER_00, HEADER_01, HEADER_02])
    async def test_normal_length_invalid_checksum_raises_IOError(self, header: bytes) -> None:
        payload = b"~MA,0,10.6,2;\r\n"
        good = make_response(header, payload)
        hp = make_hp(good[:-1] + bytes([(good[-1] + 1) & 0xFF]))
        with pytest.raises(IOError, match="invalid checksum"):
            await hp.read_response_async()

    @pytest.mark.asyncio
    async def test_negative_corrected_length_raises_IOError(self) -> None:
        # length byte 1 with header 0x02 -> corrected length -1; must be an IOError, not a ValueError
        hp = make_hp(HEADER_02 + b"\x01" + b"~" + b"\x00")
        with pytest.raises(IOError, match="invalid payload length"):
            await hp.read_response_async()
