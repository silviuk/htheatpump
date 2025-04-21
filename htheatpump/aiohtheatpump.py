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

""" This module provides an asynchronous communication with the Heliotherm heat pump. """

from __future__ import annotations

import asyncio
import copy
import datetime
import logging
import re
from typing import Dict, Final, List, Optional, Set, Tuple, Union, cast
import socket
import aioserial
import time
import serial

from .htheatpump import HtHeatpump, VerifyAction
from .htparams import HtParams, HtParamValueType
from .httimeprog import TimeProgEntry, TimeProgram
from .protocol import (
    ALC_CMD,
    ALC_RESP,
    ALS_CMD,
    ALS_RESP,
    AR_CMD,
    AR_RESP,
    CLK_CMD,
    CLK_RESP,
    LOGIN_CMD,
    LOGIN_RESP,
    LOGOUT_CMD,
    LOGOUT_RESP,
    MAX_CMD_LENGTH,
    MR_CMD,
    MR_RESP,
    PRD_CMD,
    PRD_RESP,
    PRE_CMD,
    PRE_RESP,
    PRI_CMD,
    PRI_RESP,
    PRL_CMD,
    PRL_RESP,
    RESPONSE_HEADER,
    RESPONSE_HEADER_LEN,
    RID_CMD,
    RID_RESP,
    VERSION_CMD,
    VERSION_RESP,
    create_request,
)

# ------------------------------------------------------------------------------------------------------------------- #
# Logging
# ------------------------------------------------------------------------------------------------------------------- #


_LOGGER: Final = logging.getLogger(__name__)


# ------------------------------------------------------------------------------------------------------------------- #
# AioHtHeatpump class
# ------------------------------------------------------------------------------------------------------------------- #


class AioHtHeatpump(HtHeatpump):
    """Object which encapsulates the asynchronous communication with the Heliotherm heat pump.

    :param url: The TCP URL (e.g., tcp://192.168.1.100:9999). Provide either device or url.
    :type url: Optional[str]
    :param device: The serial device to attach to (e.g. :data:`/dev/ttyUSB0`).
    :type device: str
    :param baudrate: The baud rate to use for the serial device.
    :type baudrate: int
    :param bytesize: The bytesize of the serial messages.
    :type bytesize: int
    :param parity: Which kind of parity to use.
    :type parity: str
    :param stopbits: The number of stop bits to use.
    :type stopbits: float or int
    :param timeout: The read timeout value.
        Default is :attr:`~htheatpump.htheatpump.HtHeatpump.DEFAULT_TIMEOUT`.
    :type timeout: None, float or int
    :param xonxoff: Software flow control enabled.
    :type xonxoff: bool
    :param rtscts: Hardware flow control (RTS/CTS) enabled.
    :type rtscts: bool
    :param write_timeout: The write timeout value.
    :type write_timeout: None, float or int
    :param dsrdtr: Hardware flow control (DSR/DTR) enabled.
    :type dsrdtr: bool
    :param inter_byte_timeout: Inter-character timeout, ``None`` to disable (default).
    :type inter_byte_timeout: None, float or int
    :param exclusive: Exclusive access mode enabled (POSIX only).
    :type exclusive: bool
    :param verify_param_action: Parameter verification actions.
    :type verify_param_action: None or set
    :param verify_param_error: Interpretation of parameter verification failure as error enabled.
    :type verify_param_error: bool
    :param loop: The event loop, ``None`` for the currently running event loop (default).
    :type loop: None or asyncio.AbstractEventLoop
    :param cancel_read_timeout: TODO
    :type cancel_read_timeout: int
    :param cancel_write_timeout: TODO
    :type cancel_write_timeout: int

    Example::

        # Serial
        hp = AioHtHeatpump(device="/dev/ttyUSB0", baudrate=9600)
        # TCP
        hp = AioHtHeatpump(url="tcp://192.168.1.100:9999")

        async def run():
            try:
                # open_connection is synchronous (prepares settings)
                hp.open_connection()
                # login_async establishes connection and logs in
                await hp.login_async()
                temp = await hp.get_param_async("Temp. Aussen")
                print(temp)
                # ...
            finally:
                # logout_async also closes the connection
                await hp.logout_async()

        asyncio.run(run())
    """

    def __init__(
        self,
        device: Optional[str] = None,  # Changed: Make optional
        url: Optional[str] = None,    # Added: url parameter
        baudrate: int = 115200,
        bytesize: int = serial.EIGHTBITS,
        parity: str = serial.PARITY_NONE,
        stopbits: Union[float, int] = serial.STOPBITS_ONE,
        timeout: Optional[Union[float, int]] = HtHeatpump.DEFAULT_TIMEOUT,
        xonxoff: bool = True,
        rtscts: bool = False,
        write_timeout: Optional[Union[float, int]] = None,
        dsrdtr: bool = False,
        inter_byte_timeout: Optional[Union[float, int]] = None,
        exclusive: Optional[bool] = None,
        verify_param_action: Optional[Set[VerifyAction]] = None,
        verify_param_error: bool = False,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        cancel_read_timeout: int = 1,
        cancel_write_timeout: int = 1,
    ) -> None:
        """Initialize the AioHtHeatpump class."""

        super().__init__(
            url,
            device,
            baudrate,
            bytesize,
            parity,
            stopbits,
            timeout,
            xonxoff,
            rtscts,
            write_timeout,
            dsrdtr,
            inter_byte_timeout,
            exclusive,
            verify_param_action,
            verify_param_error,
        )

        # Async specific attributes
        self._loop = loop  # Store loop, might be None initially
        self._lock = asyncio.Lock()
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        # _ser (aioserial instance) is handled below

        # update the settings for later connection establishment
        if self._ser_settings:
            # Ensure loop is available for aioserial
            if self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    # If no loop running, create one (less ideal, depends on context)
                    # Or raise an error if loop is strictly required
                    # self._loop = asyncio.new_event_loop()
                    # asyncio.set_event_loop(self._loop)
                    raise RuntimeError("asyncio event loop is required for AioHtHeatpump with serial.")

            self._ser_settings.update(
                {
                    "loop": self._loop,
                    "cancel_read_timeout": cancel_read_timeout,
                    "cancel_write_timeout": cancel_write_timeout,
                }
            )
            # Initialize aioserial instance (but don't open yet)
            # Parent __init__ sets self._ser to None, override here
            self._ser = aioserial.AioSerial(**self._ser_settings)
        else:
            # If URL is used, parent __init__ sets self._ser to None, which is correct
            self._ser = None

        # _sock_settings is populated by parent __init__ if url was provided

    # --- Connection Management ---

    def open_connection(self) -> None:
        """Open the serial connection with the defined settings.

        :raises IOError: If the connection objects indicate it's already open.
        :raises RuntimeError: If neither serial nor TCP is configured.
        """
        if self.is_open:
            raise IOError("connection objects indicate an open connection.")

        # Reset async state variables
        self._reader = None
        self._writer = None
        # Re-initialize aioserial instance if needed (settings might have changed)
        if self._ser_settings:
            if self._ser and self._ser.is_open:
                self._ser.close()  # Close existing if open
            # Ensure loop is available
            if self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    raise RuntimeError("asyncio event loop is required for AioHtHeatpump with serial.")
            # Update loop in settings if it was None before
            self._ser_settings["loop"] = self._loop
            self._ser = aioserial.AioSerial(**self._ser_settings)
            _LOGGER.info("prepared serial connection: %s", self._ser)
        elif self._sock_settings:
            _LOGGER.info("prepared TCP connection settings: %s", self._sock_settings)
        else:
            raise RuntimeError("neither serial nor socket settings are configured.")

    async def connect_async(self) -> None:
        """Establishes the asynchronous connection (Serial or TCP)."""
        if self.is_open:
            _LOGGER.debug("connection already open, skipping connect_async.")
            return

        if self._ser_settings:
            if not self._ser:
                raise RuntimeError("serial instance not prepared. Call open_connection first.")
            if not self._ser.is_open:
                try:
                    # aioserial opens implicitly, but let's ensure it
                    # A simple check or triggering an operation might be needed
                    # Forcing open if possible, or relying on first IO
                    # Let's try to trigger opening via a zero-byte read
                    await self._ser.read_async(0)
                    _LOGGER.info("serial connection confirmed open: %s", self._ser)
                except (aioserial.SerialException, OSError) as e:
                    _LOGGER.error("failed to open serial connection %s: %s", self._ser.port, e)
                    raise IOError(f"failed to open serial connection {self._ser.port}: {e}") from e

        elif self._sock_settings:
            host, port = self._sock_settings["address"]
            # Use timeout from settings, fallback to default
            timeout = self._sock_settings.get("timeout")
            if timeout is None:
                timeout = self.DEFAULT_TIMEOUT

            try:
                _LOGGER.debug("attempting to open TCP connection to %s:%s with timeout %s", host, port, timeout)
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=timeout
                )
                _LOGGER.info("TCP connection established successfully to %s:%s", host, port)
            except asyncio.TimeoutError:
                _LOGGER.error("TCP connection timed out to %s:%s", host, port)
                raise IOError(f"TCP connection timed out to {host}:{port}") from None
            except (socket.gaierror, ConnectionRefusedError, OSError) as e:
                _LOGGER.error("TCP connection failed to %s:%s: %s", host, port, e)
                # Close potentially partially opened streams on failure
                if self._writer:
                    self._writer.close()
                    # await self._writer.wait_closed() # Might hang if connection failed badly
                self._reader = None
                self._writer = None
                raise IOError(f"TCP connection failed to {host}:{port}: {e}") from e
        else:
            raise RuntimeError("neither serial nor socket settings are configured.")

    async def close_connection_async(self) -> None:
        """Close the established asynchronous connection."""
        if self._ser_settings:
            if self._ser and self._ser.is_open:
                try:
                    self._ser.close()
                    _LOGGER.info("serial connection closed.")
                except Exception as e:
                    _LOGGER.warning("error closing serial connection: %s", e)
                finally:
                    self._ser = None  # Ensure instance is cleared
                    await asyncio.sleep(0.1)  # Keep delay
        elif self._sock_settings:
            if self._writer:
                try:
                    if not self._writer.is_closing():
                        self._writer.close()
                        await self._writer.wait_closed()
                        _LOGGER.info("TCP connection closed.")
                    else:
                        _LOGGER.debug("TCP writer already closing.")
                except Exception as e:
                    _LOGGER.warning("error closing TCP writer: %s", e)
                finally:
                    self._writer = None  # Ensure cleared
                    self._reader = None  # Ensure cleared
                    await asyncio.sleep(0.1)  # Keep delay

        # Ensure state is reset even if already closed or error occurred
        self._ser = None
        self._reader = None
        self._writer = None

    def close_connection(self) -> None:
        """Close the connection (Synchronous wrapper - Use with caution)."""
        if self.is_open:
            _LOGGER.warning("close_connection called synchronously on AioHtHeatpump. Use close_connection_async.")
            # Try sync close for serial if possible
            if self._ser and self._ser.is_open:
                try:
                    self._ser.close()
                    time.sleep(0.1)
                except Exception as e:
                    _LOGGER.error("synchronous close of aioserial failed: %s", e)
            elif self._writer:
                _LOGGER.error("cannot synchronously close active async TCP connection. Use close_connection_async.")
            # Reset state regardless
            self._ser = None
            self._reader = None
            self._writer = None
        else:
            # Ensure state is reset if called when not open
            self._ser = None
            self._reader = None
            self._writer = None

    async def reconnect_async(self) -> None:
        """Reconnect asynchronously (close, prepare, connect)."""
        _LOGGER.info("attempting to reconnect...")
        await self.close_connection_async()
        # open_connection is sync and just prepares settings/instances
        self.open_connection()
        # connect_async actually establishes the connection
        await self.connect_async()
        _LOGGER.info("reconnection attempt finished.")

    def reconnect(self) -> None:
        """Synchronous reconnect wrapper (Not Recommended)."""
        _LOGGER.warning("reconnect called synchronously on AioHtHeatpump. Use reconnect_async.")
        raise NotImplementedError("synchronous reconnect is not supported. Use reconnect_async.")

    @property
    def is_open(self) -> bool:
        """Return the state of the asynchronous connection."""
        if self._ser_settings:
            # Check aioserial instance and its state
            return self._ser is not None and self._ser.is_open
        elif self._sock_settings:
            # Check if writer exists and is not closing/closed
            return self._writer is not None and not self._writer.is_closing()
        return False

    # --- Communication Methods ---

    async def send_request_async(self, cmd: str) -> None:
        """Send a request asynchronously to the heat pump."""
        if not self.is_open:
            # Try to connect if not open? Or raise error? Raising is safer.
            raise IOError("connection not open")

        req = create_request(cmd)
        _LOGGER.debug("send async request: [%s]", req)

        try:
            if self._ser_settings:
                if not self._ser:
                    raise IOError("serial connection not initialized")
                await self._ser.write_async(req)
                # await self._ser.drain_async() # Usually not needed for serial
            elif self._sock_settings:
                if not self._writer:
                    raise IOError("TCP connection not established")
                self._writer.write(req)
                await self._writer.drain()  # Ensure data is sent over TCP
            else:
                # This case should be prevented by __init__
                raise RuntimeError("neither serial, nor socket settings are configured")
        except (aioserial.SerialException, socket.error, OSError, BrokenPipeError) as e:
            _LOGGER.error("Failed to send async request: %s", e)
            # Connection likely broken, close it
            await self.close_connection_async()
            raise IOError(f"failed to send async request: {e}") from e

    async def _socket_recvall_async(self, size: int) -> bytes:
        """Receive exactly size bytes asynchronously from TCP socket."""
        if not self._reader:
            raise IOError("TCP connection not established or reader is missing")

        # Use timeout from settings for the read operation
        timeout = self._sock_settings.get("timeout") if self._sock_settings else None
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT

        try:
            data = await asyncio.wait_for(self._reader.readexactly(size), timeout=timeout)
            return data
        except asyncio.TimeoutError:
            _LOGGER.error("socket read timed out waiting for %d bytes", size)
            await self.close_connection_async()
            raise IOError(f"socket read timed out waiting for {size} bytes") from None
        except asyncio.IncompleteReadError as e:
            _LOGGER.error("socket connection closed or broke during readexactly "
                          "(expected %d, got %d)", size, len(e.partial))
            await self.close_connection_async()
            raise IOError("socket connection closed or broke during read") from e
        except (socket.error, OSError) as e:
            _LOGGER.error("socket error during readexactly: %s", e)
            await self.close_connection_async()
            raise IOError(f"socket error during read: {e}") from e

    async def read_response_async(self) -> str:
        """Read the response message from the heat pump.

        :returns: The returned response message of the heat pump as :obj:`str`.
        :rtype: ``str``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            (or unknown) response (e.g. broken data stream, unknown header, invalid checksum, ...).

        .. note::

            **There is a little bit strange behavior how the heat pump sometimes replies on some requests:**

            A response from the heat pump normally consists of the following header (the first 6 bytes)
            ``b"\\x02\\xfd\\xe0\\xd0\\x00\\x00"`` together with the payload and a computed checksum.
            But sometimes the heat pump replies with a different header (``b"\\x02\\xfd\\xe0\\xd0\\x04\\x00"``
            or ``b"\\x02\\xfd\\xe0\\xd0\\x08\\x00"``) together with the payload and a *fixed* value of
            ``0x0`` for the checksum (regardless of the content).

            We have no idea about the reason for this behavior. But after analysing the communication between
            the `Heliotherm home control <http://homecontrol.heliotherm.com/>`_ Windows application and the
            heat pump, which simply accepts this kind of responses, we also decided to handle it as a valid
            answer to a request.

            Furthermore, we have noticed another behavior, which is not fully explainable:
            For some response messages from the heat pump (e.g. for the error message ``"ERR,INVALID IDX"``)
            the transmitted payload length in the protocol is zero (0 bytes), although some payload follows.
            In this case we read until we will found the trailing ``b"\\r\\n"`` at the end of the payload
            to determine the payload of the message.

            Additionally to the upper described facts, for some of the answers of the heat pump the payload length
            must be corrected (for the checksum computation) so that the received checksum fits with the computed
            one (e.g. for ``b"\\x02\\xfd\\xe0\\xd0\\x01\\x00"`` and ``b"\\x02\\xfd\\xe0\\xd0\\x02\\x00"``).
        """
        if not self.is_open:
            raise IOError("connection not open")

        # Determine read timeout
        read_timeout: Optional[Union[float, int]] = None
        if self._ser_settings:
            read_timeout = self._ser.timeout if self._ser else self.DEFAULT_TIMEOUT
        elif self._sock_settings:
            read_timeout = self._sock_settings.get("timeout", self.DEFAULT_TIMEOUT)
        if read_timeout is None:
            read_timeout = self.DEFAULT_TIMEOUT  # Ensure a value

        # Read header
        try:
            if self._ser_settings:
                if not self._ser:
                    raise IOError("serial connection not initialized")
                header = await asyncio.wait_for(self._ser.read_async(RESPONSE_HEADER_LEN), timeout=read_timeout)
            elif self._sock_settings:
                header = await self._socket_recvall_async(RESPONSE_HEADER_LEN)  # Uses internal timeout
            else:
                raise RuntimeError("neither serial, nor socket settings are configured.")
        except (IOError, asyncio.TimeoutError, aioserial.SerialException, socket.error, OSError) as e:
            _LOGGER.error("failed reading response header: %s", e)
            await self.close_connection_async()
            raise IOError(f"failed reading response header: {e}") from e

        if not header or len(header) < RESPONSE_HEADER_LEN:
            await self.close_connection_async()
            raise IOError(f"data stream broken reading header (incomplete: {header!r})")
        elif header not in RESPONSE_HEADER:
            _LOGGER.error("invalid or unknown response header received: %r", header)
            await self.close_connection_async()
            raise IOError(f"invalid or unknown response header [{header!r}]")

        # Read payload length byte
        try:
            if self._ser_settings:
                if not self._ser:
                    raise IOError("serial connection not initialized")
                payload_len_r_bytes = await asyncio.wait_for(self._ser.read_async(1), timeout=read_timeout)
            else:  # Socket
                payload_len_r_bytes = await self._socket_recvall_async(1)  # Uses internal timeout
        except (IOError, asyncio.TimeoutError, aioserial.SerialException, socket.error, OSError) as e:
            _LOGGER.error("failed reading payload length: %s", e)
            await self.close_connection_async()
            raise IOError(f"failed reading payload length: {e}") from e

        if not payload_len_r_bytes:
            await self.close_connection_async()
            raise IOError("data stream broken reading payload length (empty byte)")

        payload_len_r = payload_len_r_bytes[0]
        # payload_len will be corrected later for checksum

        # Read payload
        payload: bytes
        actual_payload_len: int

        try:
            if payload_len_r == 0:
                _LOGGER.info("received response with a payload length zero; reading until '\\r\\n' [header=%r]", header)
                payload = b""
                # Use a loop with timeout for each byte read
                loop_start_time = asyncio.get_running_loop().time()
                while payload[-2:] != b"\r\n":
                    # Check overall timeout for this loop
                    if (asyncio.get_running_loop().time() - loop_start_time) > read_timeout:
                        raise asyncio.TimeoutError("overall timeout while reading payload ending with '\\r\\n'")

                    try:
                        # Read one byte with a smaller, per-byte timeout or rely on overall
                        byte_timeout = min(0.5, read_timeout)  # Example: 0.5s per byte
                        if self._ser_settings:
                            if not self._ser:
                                raise IOError("serial connection not initialized")
                            tmp = await asyncio.wait_for(self._ser.read_async(1), timeout=byte_timeout)
                        else:  # Socket
                            tmp = await asyncio.wait_for(self._socket_recvall_async(1), timeout=byte_timeout)

                        if not tmp:
                            raise IOError("data stream broken (received empty byte)")
                        payload += tmp
                    except asyncio.TimeoutError:
                        # This might happen if there's a pause, continue loop if overall timeout not exceeded
                        _LOGGER.debug("per-byte read timeout, continuing if overall time allows.")
                        continue
                    except (IOError, aioserial.SerialException, socket.error, OSError) as e:
                        raise IOError(f"failed reading payload chunk (len=0): {e}") from e

                actual_payload_len = len(payload)
            else:
                # Read payload_len_r bytes
                if self._ser_settings:
                    if not self._ser:
                        raise IOError("serial connection not initialized")
                    payload = await asyncio.wait_for(self._ser.read_async(payload_len_r), timeout=read_timeout)
                else:  # Socket
                    payload = await self._socket_recvall_async(payload_len_r)  # Uses internal timeout

                if not payload or len(payload) < payload_len_r:
                    raise IOError("data stream broken reading payload")
                actual_payload_len = len(payload)

        except (IOError, asyncio.TimeoutError, aioserial.SerialException, socket.error, OSError) as e:
            _LOGGER.error("failed reading payload: %s", e)
            await self.close_connection_async()
            raise IOError(f"failed reading payload: {e}") from e

        # Correct payload length for checksum computation based on header
        # Call the function to get the corrected length value
        corrected_payload_len_val = RESPONSE_HEADER[header]["payload_len"](payload_len_r)

        # Read checksum byte
        try:
            if self._ser_settings:
                if not self._ser:
                    raise IOError("serial connection not initialized")
                checksum_bytes = await asyncio.wait_for(self._ser.read_async(1), timeout=read_timeout)
            else:  # Socket
                checksum_bytes = await self._socket_recvall_async(1)  # Uses internal timeout
        except (IOError, asyncio.TimeoutError, aioserial.SerialException, socket.error, OSError) as e:
            _LOGGER.error("failed reading checksum: %s", e)
            await self.close_connection_async()
            raise IOError(f"failed reading checksum: {e}") from e

        if not checksum_bytes:
            await self.close_connection_async()
            raise IOError("data stream broken reading checksum (empty byte)")
        checksum = checksum_bytes[0]

        # Compute and verify checksum
        # Call the function to get the computed checksum value
        comp_checksum_val = RESPONSE_HEADER[header]["checksum"](
            header, corrected_payload_len_val, payload
        )

        # Compare the integer checksum values
        if checksum != comp_checksum_val:
            # Use the calculated values in the error message
            error_msg = (
                f"invalid checksum [{checksum:#04x}] of response "
                f"[header={header!r}, payload_len={corrected_payload_len_val}(orig={payload_len_r}), "
                f"payload={payload!r}, expected_checksum={comp_checksum_val:#04x}]"
            )
            _LOGGER.error(error_msg)
            await self.close_connection_async()
            raise IOError(error_msg)

        # Debug log
        _LOGGER.debug("received async response raw: %s", header + payload_len_r_bytes + payload + checksum_bytes)
        _LOGGER.debug("  header = %r", header)
        # Use the calculated value in the debug log
        _LOGGER.debug("  payload length = %d (orig=%d, actual=%d)",
                      corrected_payload_len_val, payload_len_r, actual_payload_len)
        _LOGGER.debug("  payload = %r", payload)
        _LOGGER.debug("  checksum = %#04x", checksum)

        # Extract data
        try:
            decoded_payload = payload.decode("ascii")
            m = re.match(r"^~([^;]*);\r\n$", decoded_payload)
            if not m:
                raise IOError(f"failed to extract response data from payload [{payload!r}]")
            return m.group(1)
        except UnicodeDecodeError as e:
            _LOGGER.error("failed to decode payload as ASCII: %s [%r]", e, payload)
            await self.close_connection_async()
            raise IOError(f"failed to decode payload as ASCII: {e} [{payload!r}]") from e
        except IOError as e:
            _LOGGER.error("error extracting data: %s", e)
            await self.close_connection_async()
            raise e  # Re-raise the extraction error

    # --- Async API Methods ---

    async def login_async(
        self,
        update_param_limits: bool = False,
        max_retries: int = HtHeatpump.DEFAULT_LOGIN_RETRIES,
    ) -> None:
        """Log in the heat pump. If :attr:`update_param_limits` is :const:`True` an update of the
        parameter limits in :class:`~htheatpump.htparams.HtParams` will be performed. This will
        be done by requesting the current value together with their limits (MIN and MAX) for all
        “known” parameters directly after a successful login.

        :param update_param_limits: Determines whether an update of the parameter limits in
            :class:`~htheatpump.htparams.HtParams` should be done or not. Default is :const:`False`.
        :type update_param_limits: bool
        :param max_retries: Maximal number of retries for a successful login. One regular try
            plus :const:`max_retries` retries.
            Default is :attr:`~htheatpump.htheatpump.HtHeatpump.DEFAULT_LOGIN_RETRIES`.
        :type max_retries: int
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            # Ensure connection is established before login attempt
            await self.connect_async()  # Ensures connection is open

            success = False
            retry = 0
            while not success and retry <= max_retries:
                try:
                    await self.send_request_async(LOGIN_CMD)
                    resp = await self.read_response_async()
                    m = re.match(LOGIN_RESP, resp)
                    if not m:
                        raise IOError(f"invalid response for LOGIN command [{resp!r}]")
                    success = True
                except Exception as ex:
                    retry += 1
                    _LOGGER.warning("login try #%d failed: %s", retry, ex)
                    if retry <= max_retries:
                        _LOGGER.info("attempting reconnect after failed login...")
                        try:
                            await self.reconnect_async()
                        except Exception as recon_ex:
                            _LOGGER.error("reconnect failed: %s", recon_ex)
                            break  # Stop retrying if reconnect fails
                        await asyncio.sleep(0.2 * retry)  # Slight backoff

            if not success:
                _LOGGER.error("login failed after %d try/tries", retry)
                await self.close_connection_async()  # Ensure closed on final failure
                raise IOError(f"login failed after {retry} try/tries")

            _LOGGER.info("login successful")
            if update_param_limits:
                await self.update_param_limits_async()

    async def logout_async(self) -> None:
        """Log out from the heat pump session asynchronously and close connection."""
        if not self.is_open:
            _LOGGER.info("connection already closed, skipping logout.")
            return

        # Use lock to prevent concurrent operations during logout/close
        async with self._lock:
            # Check again inside lock
            if not self.is_open:
                _LOGGER.info("connection closed before logout could proceed.")
                return
            try:
                await self.send_request_async(LOGOUT_CMD)
                resp = await self.read_response_async()
                m = re.match(LOGOUT_RESP, resp)
                if not m:
                    _LOGGER.warning(f"invalid response for LOGOUT command [{resp!r}]")
                else:
                    _LOGGER.info("logout successful")
            except Exception as ex:
                _LOGGER.warning("logout command failed: %s", ex)
            finally:
                # Always close the connection after attempting logout
                await self.close_connection_async()

    async def get_serial_number_async(self) -> int:
        """Query for the manufacturer's serial number of the heat pump.

        :returns: The manufacturer's serial number of the heat pump as :obj:`int` (e.g. :data:`123456`).
        :rtype: ``int``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            # send RID request to the heat pump
            await self.send_request_async(RID_CMD)
            # ... and wait for the response
            try:
                resp = await self.read_response_async()  # e.g. "RID,123456"
                m = re.match(RID_RESP, resp)
                if not m:
                    raise IOError(
                        "invalid response for RID command [{!r}]".format(resp)
                    )
                rid = int(m.group(1))
                _LOGGER.debug("manufacturer's serial number = %d", rid)
                return rid  # return the received manufacturer's serial number as an int
            except Exception as ex:
                _LOGGER.error("query for manufacturer's serial number failed: %s", ex)
                raise

    async def get_version_async(self) -> Tuple[str, int]:
        """Query for the software version of the heat pump.

        :returns: The software version of the heat pump as a tuple with 2 elements.
            The first element inside the returned tuple represents the software
            version as a readable string in a common version number format
            (e.g. :data:`"3.0.20"`). The second element (probably) contains a numerical
            representation as :obj:`int` of the software version returned by the heat pump.
            For example:
            ::

                ( "3.0.20", 2321 )

        :rtype: ``tuple`` ( str, int )
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            # send request command to the heat pump
            await self.send_request_async(VERSION_CMD)
            # ... and wait for the response
            try:
                resp = await self.read_response_async()
                # search for pattern "NAME=..." and "VAL=..." inside the response string;
                #   the textual representation of the version is encoded in the 'NAME',
                #   e.g. "SP,NR=9,ID=9,NAME=3.0.20,LEN=4,TP=0,BIT=0,VAL=2321,MAX=0,MIN=0,WR=0,US=1"
                #   => software version = 3.0.20
                m = re.match(VERSION_RESP, resp)
                if not m:
                    raise IOError(
                        "invalid response for query of the software version [{!r}]".format(
                            resp
                        )
                    )
                ver = (m.group(1).strip(), int(m.group(2)))
                _LOGGER.debug("software version = %s (%d)", *ver)
                return ver
            except Exception as ex:
                _LOGGER.error("query for software version failed: %s", ex)
                raise

    async def get_date_time_async(self) -> Tuple[datetime.datetime, int]:
        """Read the current date and time of the heat pump.

        :returns: The current date and time of the heat pump as a tuple with 2 elements, where
            the first element is of type :class:`datetime.datetime` which represents the current
            date and time while the second element is the corresponding weekday in form of an
            :obj:`int` between 1 and 7, inclusive (Monday through Sunday). For example:
            ::

                ( datetime.datetime(...), 2 )  # 2 = Tuesday

        :rtype: ``tuple`` ( datetime.datetime, int )
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            # send CLK request to the heat pump
            await self.send_request_async(CLK_CMD[0])
            # ... and wait for the response
            try:
                resp = (
                    await self.read_response_async()
                )  # e.g. "CLK,DA=26.11.15,TI=21:28:57,WD=4"
                m = re.match(CLK_RESP, resp)
                if not m:
                    raise IOError(
                        "invalid response for CLK command [{!r}]".format(resp)
                    )
                year = 2000 + int(m.group(3))
                month, day, hour, minute, second = [
                    int(g) for g in m.group(2, 1, 4, 5, 6)
                ]
                weekday = int(m.group(7))  # weekday 1-7 (Monday through Sunday)
                # create datetime object from extracted data
                dt = datetime.datetime(year, month, day, hour, minute, second)
                _LOGGER.debug("datetime = %s, weekday = %d", dt.isoformat(), weekday)
                return (
                    dt,
                    weekday,
                )  # return the heat pump's date and time as a datetime object
            except Exception as ex:
                _LOGGER.error("query for date and time failed: %s", ex)
                raise

    async def set_date_time_async(
        self, dt: Optional[datetime.datetime] = None
    ) -> Tuple[datetime.datetime, int]:
        """Set the current date and time of the heat pump.

        :param dt: The date and time to set. If :const:`None` current date and time
            of the host will be used.
        :type dt: datetime.datetime
        :returns: A 2-elements tuple composed of a :class:`datetime.datetime` which represents
            the sent date and time and an :obj:`int` between 1 and 7, inclusive, for the corresponding
            weekday (Monday through Sunday).
        :rtype: ``tuple`` ( datetime.datetime, int )
        :raises TypeError:
            Raised for an invalid type of argument :attr:`dt`. Must be :const:`None` or
            of type :class:`datetime.datetime`.
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            if dt is None:
                dt = datetime.datetime.now()
            elif not isinstance(dt, datetime.datetime):
                raise TypeError(
                    "argument 'dt' must be None or of type datetime.datetime"
                )
            # create CLK set command
            cmd = CLK_CMD[1].format(
                dt.day,
                dt.month,
                dt.year - 2000,
                dt.hour,
                dt.minute,
                dt.second,
                dt.isoweekday(),
            )
            # send command to the heat pump
            await self.send_request_async(cmd)
            # ... and wait for the response
            try:
                resp = (
                    await self.read_response_async()
                )  # e.g. "CLK,DA=26.11.15,TI=21:28:57,WD=4"
                m = re.match(CLK_RESP, resp)
                if not m:
                    raise IOError(
                        "invalid response for CLK command [{!r}]".format(resp)
                    )
                year = 2000 + int(m.group(3))
                month, day, hour, minute, second = [
                    int(g) for g in m.group(2, 1, 4, 5, 6)
                ]
                weekday = int(m.group(7))  # weekday 1-7 (Monday through Sunday)
                # create datetime object from extracted data
                dt = datetime.datetime(year, month, day, hour, minute, second)
                _LOGGER.debug("datetime = %s, weekday = %d", dt.isoformat(), weekday)
                return (
                    dt,
                    weekday,
                )  # return the heat pump's date and time as a datetime object
            except Exception as ex:
                _LOGGER.error("set of date and time failed: %s", ex)
                raise

    async def get_last_fault_async(self) -> Tuple[int, int, datetime.datetime, str]:
        """Query for the last fault message of the heat pump.

        :returns:
            The last fault message of the heat pump as a tuple with 4 elements.
            The first element of the returned tuple represents the index as :obj:`int` of
            the message inside the fault list. The second element is (probably) the
            the error code as :obj:`int` defined by Heliotherm. The last two elements of the
            tuple are the date and time when the error occurred (as :class:`datetime.datetime`)
            and the error message string itself. For example:
            ::

                ( 29, 20, datetime.datetime(...), "EQ_Spreizung" )

        :rtype: ``tuple`` ( int, int, datetime.datetime, str )
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            # send ALC request to the heat pump
            await self.send_request_async(ALC_CMD)
            # ... and wait for the response
            try:
                resp = (
                    await self.read_response_async()
                )  # e.g. "AA,29,20,14.09.14-11:52:08,EQ_Spreizung"
                m = re.match(ALC_RESP, resp)
                if not m:
                    raise IOError(
                        "invalid response for ALC command [{!r}]".format(resp)
                    )
                idx, err = [
                    int(g) for g in m.group(1, 2)
                ]  # fault list index, error code (?)
                year = 2000 + int(m.group(5))
                month, day, hour, minute, second = [
                    int(g) for g in m.group(4, 3, 6, 7, 8)
                ]
                # create datetime object from extracted data
                dt = datetime.datetime(year, month, day, hour, minute, second)
                msg = m.group(9).strip()
                _LOGGER.debug(
                    "(idx: %d, err: %d)[%s]: %s", idx, err, dt.isoformat(), msg
                )
                return idx, err, dt, msg
            except Exception as ex:
                _LOGGER.error("query for last fault message failed: %s", ex)
                raise

    async def get_fault_list_size_async(self) -> int:
        """Query for the fault list size of the heat pump.

        :returns: The size of the fault list as :obj:`int`.
        :rtype: ``int``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            # send ALS request to the heat pump
            await self.send_request_async(ALS_CMD)
            # ... and wait for the response
            try:
                resp = await self.read_response_async()  # e.g. "SUM=2757"
                m = re.match(ALS_RESP, resp)
                if not m:
                    raise IOError(
                        "invalid response for ALS command [{!r}]".format(resp)
                    )
                size = int(m.group(1))
                _LOGGER.debug("fault list size = %d", size)
                return size
            except Exception as ex:
                _LOGGER.error("query for fault list size failed: %s", ex)
                raise

    async def get_fault_list_async(self, *args: int) -> List[Dict[str, object]]:
        """Query for the fault list of the heat pump.

        :param args: The index number(s) to request from the fault list (optional).
            If not specified all fault list entries are requested.
        :type args: int
        :returns: The requested entries of the fault list as :obj:`list`, e.g.:
            ::

                [ { "index"   : 29,                     # fault list index
                    "error"   : 20,                     # error code
                    "datetime": datetime.datetime(...), # date and time of the entry
                    "message" : "EQ_Spreizung",         # error message
                    },
                  # ...
                  ]

        :rtype: ``list``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        if not args:
            args = tuple(range(await self.get_fault_list_size_async()))
        # TODO args = set(args) ???
        async with self._lock:
            fault_list = []
            # request fault list entries in several pieces (if required)
            n = 0
            while n < len(args):
                cnt = 0
                cmd = AR_CMD
                while n < len(args):
                    item = ",{}".format(args[n])
                    if len(cmd + item) <= MAX_CMD_LENGTH:
                        cmd += item
                        cnt += 1
                        n += 1
                    else:
                        break
                assert cnt > 0
                # send AR request to the heat pump
                await self.send_request_async(cmd)
                # ... and wait for the response
                try:
                    resp = []
                    # read all requested fault list entries
                    for _ in range(cnt):
                        resp.append(
                            await self.read_response_async()
                        )  # e.g. "AA,29,20,14.09.14-11:52:08,EQ_Spreizung"
                    # extract data (fault list index, error code, date, time and message)
                    for i, r in enumerate(resp):
                        m = re.match(AR_RESP, r)
                        if not m:
                            raise IOError(
                                "invalid response for AR command [{!r}]".format(r)
                            )
                        idx, err = [
                            int(g) for g in m.group(1, 2)
                        ]  # fault list index, error code
                        year = 2000 + int(m.group(5))
                        month, day, hour, minute, second = [
                            int(g) for g in m.group(4, 3, 6, 7, 8)
                        ]
                        # create datetime object from extracted data
                        dt = datetime.datetime(year, month, day, hour, minute, second)
                        msg = m.group(9).strip()
                        _LOGGER.debug(
                            "(idx: %03d, err: %05d)[%s]: %s",
                            idx,
                            err,
                            dt.isoformat(),
                            msg,
                        )
                        if idx != args[n - cnt + i]:
                            raise IOError(
                                "fault list index doesn't match [{:d}, should be {:d}]".format(
                                    idx, args[n - cnt + i]
                                )
                            )
                        # add the received fault list entry to the result list
                        fault_list.append(
                            {
                                "index": idx,  # fault list index
                                "error": err,  # error code
                                "datetime": dt,  # date and time of the entry
                                "message": msg,  # error message
                            }
                        )
                except Exception as ex:
                    _LOGGER.error("query for fault list failed: %s", ex)
                    raise
            return fault_list

    async def _get_param_async(
        self, name: str
    ) -> Tuple[str, HtParamValueType, HtParamValueType, HtParamValueType]:
        """Read the data (NAME, MIN, MAX, VAL) of a specific parameter of the heat pump.

        :param name: The parameter name, e.g. :data:`"Betriebsart"`.
        :type name: str
        :returns: The extracted parameter data as a tuple with 4 elements. The first element inside
            the returned tuple represents the parameter name as :obj:`str`, the second and third element
            the minimal and maximal value (as :obj:`bool`, :obj:`int` or :obj:`float`) and the last element
            the current value (as :obj:`bool`, :obj:`int` or :obj:`float`) of the parameter. For example:
            ::

                ( "Temp. EQ_Austritt", -20.0, 30.0, 15.1 )  # name, min, max, val

        :rtype: ``tuple`` ( str, bool/int/float, bool/int/float, bool/int/float )
        :raises IOError:
            Will be raised for an incomplete/invalid response from the heat pump.
        """
        async with self._lock:
            # get the corresponding definition for the requested parameter
            assert (
                name in HtParams
            ), "parameter definition for parameter {!r} not found".format(name)
            param = HtParams[name]  # type: ignore
            # send command to the heat pump
            await self.send_request_async(param.cmd())
            # ... and wait for the response
            try:
                resp = await self.read_response_async()
                return self._extract_param_data(name, resp)
            except Exception as ex:
                _LOGGER.error("query of parameter '%s' failed: %s", name, ex)
                raise

    async def update_param_limits_async(self) -> List[str]:
        """Perform an update of the parameter limits in :class:`~htheatpump.htparams.HtParams` by requesting
        the limit values of all "known" parameters directly from the heat pump.

        :returns: The list of updated (changed) parameters.
        :rtype: ``list``
        :raises VerificationException:
            Will be raised if the parameter verification fails and the property :attr:`~HtHeatpump.verify_param_error`
            is set to :const:`True`. If property :attr:`~HtHeatpump.verify_param_error` is set to :const:`False` only
            a warning message will be emitted. The performed verification steps are defined by the property
            :attr:`~HtHeatpump.verify_param_action`.
        """
        updated_params = []  # stores the name of updated parameters
        for name in HtParams.keys():
            resp_name, resp_min, resp_max, _ = await self._get_param_async(name)
            # only verify the returned NAME here, ignore MIN and MAX (and also the returned VAL)
            self._verify_param_resp(name, resp_name)
            # update the limit values in the HtParams database and count the number of updated entries
            if HtParams[name].set_limits(resp_min, resp_max):
                updated_params.append(name)
                _LOGGER.debug(
                    "updated param '%s': MIN=%s, MAX=%s", name, resp_min, resp_max
                )
        _LOGGER.info(
            "updated %d (of %d) parameter limits", len(updated_params), len(HtParams)
        )
        return updated_params

    async def get_param_async(self, name: str) -> HtParamValueType:
        """Query for a specific parameter of the heat pump.

        :param name: The parameter name, e.g. :data:`"Betriebsart"`.
        :type name: str
        :returns: Returned value of the requested parameter.
            The type of the returned value is defined by the csv-table
            of supported heat pump parameters in :file:`htparams.csv`.
        :rtype: ``bool``, ``int`` or ``float``
        :raises KeyError:
            Will be raised when the parameter definition for the passed parameter is not found.
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        :raises VerificationException:
            Will be raised if the parameter verification fails and the property :attr:`~HtHeatpump.verify_param_error`
            is set to :const:`True`. If property :attr:`~HtHeatpump.verify_param_error` is set to :const:`False` only
            a warning message will be emitted. The performed verification steps are defined by the property
            :attr:`~HtHeatpump.verify_param_action`.

        For example, the following call
        ::

            temp = await hp.get_param_async("Temp. Aussen")

        will return the current measured outdoor temperature in °C.
        """
        # find the corresponding definition for the parameter
        if name not in HtParams:
            raise KeyError(
                "parameter definition for parameter {!r} not found".format(name)
            )
        try:
            resp = await self._get_param_async(name)
            val = self._verify_param_resp(name, *resp)
            _LOGGER.debug("'%s' = %s", name, val)
            assert val is not None
            return val
        except Exception as ex:
            _LOGGER.error("get parameter '%s' failed: %s", name, ex)
            raise

    async def set_param_async(
        self, name: str, val: HtParamValueType, ignore_limits: bool = False
    ) -> HtParamValueType:
        """Set the value of a specific parameter of the heat pump. If :attr:`ignore_limits` is :const:`False`
        and the passed value is beyond the parameter limits a :exc:`ValueError` will be raised.

        :param name: The parameter name, e.g. :data:`"Betriebsart"`.
        :type name: str
        :param val: The value to set.
        :type val: bool, int or float
        :param ignore_limits: Indicates if the parameter limits should be ignored or not.
        :type ignore_limits: bool
        :returns: Returned value of the parameter set request.
            In case of success this value should be the same as the one
            passed to the function.
            The type of the returned value is defined by the csv-table
            of supported heat pump parameters in :file:`htparams.csv`.
        :rtype: ``bool``, ``int`` or ``float``
        :raises KeyError:
            Will be raised when the parameter definition for the passed parameter is not found.
        :raises ValueError:
            Will be raised if the passed value is beyond the parameter limits and argument :attr:`ignore_limits`
            is set to :const:`False`.
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        :raises VerificationException:
            Will be raised if the parameter verification fails and the property :attr:`~HtHeatpump.verify_param_error`
            is set to :const:`True`. If property :attr:`~HtHeatpump.verify_param_error` is set to :const:`False` only
            a warning message will be emitted. The performed verification steps are defined by the property
            :attr:`~HtHeatpump.verify_param_action`.

        For example, the following call
        ::

            await hp.set_param_async("HKR Soll_Raum", 21.5)

        will set the desired room temperature of the heating circuit to 21.5 °C.
        """
        async with self._lock:
            assert val is not None, "'val' must not be None"
            # find the corresponding definition for the parameter
            if name not in HtParams:
                raise KeyError(
                    "parameter definition for parameter {!r} not found".format(name)
                )
            param = HtParams[name]  # type: ignore
            # check the passed value against the defined limits (if desired)
            if not ignore_limits and not param.in_limits(val):
                raise ValueError(
                    "value {!r} is beyond the limits [{}, {}]".format(
                        val, param.min_val, param.max_val
                    )
                )
            # send command to the heat pump
            val = param.to_str(val)
            await self.send_request_async("{},VAL={}".format(param.cmd(), val))
            # ... and wait for the response
            try:
                resp = await self.read_response_async()
                data = self._extract_param_data(name, resp)
                ret = self._verify_param_resp(name, *data)
                _LOGGER.debug("'%s' = %s", name, ret)
                assert ret is not None
                return ret
            except Exception as ex:
                _LOGGER.error("set parameter '%s' failed: %s", name, ex)
                raise

    @property
    async def in_error_async(self) -> bool:
        """Query whether the heat pump is malfunctioning.

        :returns: :const:`True` if the heat pump is malfunctioning, :const:`False` otherwise.
        :rtype: ``bool``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        return cast(bool, await self.get_param_async("Stoerung"))

    async def query_async(self, *args: str) -> Dict[str, HtParamValueType]:
        """Query for the current values of parameters from the heat pump.

        :param args: The parameter name(s) to request from the heat pump.
            If not specified all "known" parameters are requested.
        :type args: str
        :returns: A dict of the requested parameters with their values, e.g.:
            ::

                { "HKR Soll_Raum": 21.0,
                  "Stoerung": False,
                  "Temp. Aussen": 8.8,
                  # ...
                  }

        :rtype: ``dict``
        :raises KeyError:
            Will be raised when the parameter definition for a passed parameter is not found.
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        :raises VerificationException:
            Will be raised if the parameter verification fails and the property :attr:`~HtHeatpump.verify_param_error`
            is set to :const:`True`. If property :attr:`~HtHeatpump.verify_param_error` is set to :const:`False` only
            a warning message will be emitted. The performed verification steps are defined by the property
            :attr:`~HtHeatpump.verify_param_action`.
        """
        if not args:
            args = tuple(HtParams.keys())
        values = {}
        try:
            # query for each parameter in the given list
            for name in args:
                values.update({name: await self.get_param_async(name)})
        except Exception as ex:
            _LOGGER.error("query of parameter(s) failed: %s", ex)
            raise
        return values

    async def fast_query_async(self, *args: str) -> Dict[str, HtParamValueType]:
        """Query for the current values of parameters from the heat pump the fast way.

        .. note::

            Only available for parameters representing a "MP" data point and no parameter verification possible!

        :param args: The parameter name(s) to request from the heat pump.
            If not specified all "known" parameters representing a "MP" data point are requested.
        :type args: str
        :returns: A dict of the requested parameters with their values, e.g.:
            ::

                { "EQ Pumpe (Ventilator)": False,
                  "FWS Stroemungsschalter": False,
                  "Frischwasserpumpe": 0,
                  "HKR_Sollwert": 26.8,
                  # ...
                  }

        :rtype: ``dict``
        :raises KeyError:
            Will be raised when the parameter definition for a passed parameter is not found.
        :raises ValueError:
            Will be raised when a passed parameter doesn't represent a "MP" data point.
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            if not args:
                args = tuple(
                    name for name, param in HtParams.items() if param.dp_type == "MP"
                )
            # TODO args = set(args) ???
            dp_list = []
            dp_dict = {}
            for name in args:
                if name not in HtParams:
                    raise KeyError(
                        "parameter definition for parameter {!r} not found".format(name)
                    )
                param = HtParams[name]  # type: ignore
                if param.dp_type != "MP":
                    raise ValueError(
                        "invalid parameter {!r}; only parameters representing a 'MP' data point are allowed".format(
                            name
                        )
                    )
                dp_list.append(param.dp_number)
                dp_dict.update({param.dp_number: (name, param)})
            values = {}
            # query for the current values of parameters in several pieces (if required)
            n = 0
            while n < len(dp_list):
                cnt = 0
                cmd = MR_CMD
                while n < len(dp_list):
                    number = ",{}".format(dp_list[n])
                    if len(cmd + number) <= MAX_CMD_LENGTH:
                        cmd += number
                        cnt += 1
                        n += 1
                    else:
                        break
                assert cnt > 0
                # send MR request to the heat pump
                await self.send_request_async(cmd)
                # ... and wait for the response
                try:
                    resp = []
                    # read all requested data point (parameter) values
                    for _ in range(cnt):
                        resp.append(
                            await self.read_response_async()
                        )  # e.g. "MA,11,46.0,16"
                    # extract data (MP data point number, data point value and "unknown" value)
                    for r in resp:
                        m = re.match(MR_RESP, r)
                        if not m:
                            raise IOError(
                                "invalid response for MR command [{!r}]".format(r)
                            )
                        # MP data point number, value and ?
                        dp_number, dp_value, unknown_val = m.group(1, 2, 3)
                        dp_number = int(dp_number)
                        if dp_number not in dp_dict:
                            raise IOError(
                                "non requested data point value received [MP,{:d}]".format(
                                    dp_number
                                )
                            )
                        name, param = dp_dict[dp_number]
                        val = param.from_str(dp_value)
                        _LOGGER.debug("'%s' = %s (%s)", name, val, unknown_val)
                        # check the received value against the limits and write a WARNING if necessary
                        if not param.in_limits(val):
                            _LOGGER.warning(
                                "value '%s' of parameter '%s' is beyond the limits [%s, %s]",
                                val,
                                name,
                                param.min_val,
                                param.max_val,
                            )
                        values.update({name: val})
                except Exception as ex:
                    _LOGGER.error("fast query of parameter(s) failed: %s", ex)
                    raise
            return values

    async def get_time_progs_async(self) -> List[TimeProgram]:
        """Return a list of all available time programs of the heat pump.

        :returns: A list of :class:`~htheatpump.httimeprog.TimeProgram` instances.
        :rtype: ``list``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            time_progs = []
            # send PRL request to the heat pump
            await self.send_request_async(PRL_CMD)
            # ... and wait for the response
            try:
                resp = await self.read_response_async()  # e.g. "SUM=5"
                m = re.match(PRL_RESP[0], resp)
                if not m:
                    raise IOError(
                        "invalid response for PRL command [{!r}]".format(resp)
                    )
                sum = int(m.group(1))
                _LOGGER.debug("number of time programs = %d", sum)
                for idx in range(sum):
                    resp = (
                        await self.read_response_async()
                    )  # e.g. "PRI0,NAME=Warmwasser,EAD=7,NOS=2,STE=15,NOD=7,ACS=0,US=1"
                    m = re.match(PRL_RESP[1].format(idx), resp)
                    if not m:
                        raise IOError(
                            "invalid response for PRL command [{!r}]".format(resp)
                        )
                    # extract data (NAME, EAD, NOS, STE and NOD)
                    name = m.group(1)
                    ead, nos, ste, nod = [int(g) for g in m.group(2, 3, 4, 5)]
                    _LOGGER.debug(
                        "[idx=%d]: name='%s', ead=%d, nos=%d, ste=%d, nod=%d",
                        idx,
                        name,
                        ead,
                        nos,
                        ste,
                        nod,
                    )
                    time_progs.append(TimeProgram(idx, name, ead, nos, ste, nod))
            except Exception as ex:
                _LOGGER.error("query for time programs failed: %s", ex)
                raise
            return time_progs

    async def _get_time_prog_async(self, idx: int) -> TimeProgram:
        """Return a specific time program (specified by their index) without their time program entries
        from the heat pump.

        :param idx: The time program index.
        :type idx: int

        :returns: The requested time program as :class:`~htheatpump.httimeprog.TimeProgram` without
            their time program entries.
        :rtype: ``TimeProgram``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            assert isinstance(idx, int)
            # send PRI request to the heat pump
            await self.send_request_async(PRI_CMD.format(idx))
            # ... and wait for the response
            try:
                resp = (
                    await self.read_response_async()
                )  # e.g. "PRI0,NAME=Warmwasser,EAD=7,NOS=2,STE=15,NOD=7,ACS=0,US=1"
                m = re.match(PRI_RESP.format(idx), resp)
                if not m:
                    raise IOError(
                        "invalid response for PRI command [{!r}]".format(resp)
                    )
                # extract data (NAME, EAD, NOS, STE and NOD)
                name = m.group(1)
                ead, nos, ste, nod = [int(g) for g in m.group(2, 3, 4, 5)]
                _LOGGER.debug(
                    "[idx=%d]: name='%s', ead=%d, nos=%d, ste=%d, nod=%d",
                    idx,
                    name,
                    ead,
                    nos,
                    ste,
                    nod,
                )
                time_prog = TimeProgram(idx, name, ead, nos, ste, nod)
                return time_prog
            except Exception as ex:
                _LOGGER.error("query for time program failed: %s", ex)
                raise

    async def _get_time_prog_with_entries_async(self, idx: int) -> TimeProgram:
        """Return a specific time program (specified by their index) together with their time program entries
        from the heat pump.

        :param idx: The time program index.
        :type idx: int

        :returns: The requested time program as :class:`~htheatpump.httimeprog.TimeProgram` together
            with their time program entries.
        :rtype: ``TimeProgram``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            assert isinstance(idx, int)
            # send PRD request to the heat pump
            await self.send_request_async(PRD_CMD.format(idx))
            # ... and wait for the response
            try:
                resp = (
                    await self.read_response_async()
                )  # e.g. "PRI0,NAME=Warmwasser,EAD=7,NOS=2,STE=15,NOD=7,ACS=0,US=1"
                m = re.match(PRD_RESP[0].format(idx), resp)
                if not m:
                    raise IOError(
                        "invalid response for PRD command [{!r}]".format(resp)
                    )
                # extract data (NAME, EAD, NOS, STE and NOD)
                name = m.group(1)
                ead, nos, ste, nod = [int(g) for g in m.group(2, 3, 4, 5)]
                _LOGGER.debug(
                    "[idx=%d]: name='%s', ead=%d, nos=%d, ste=%d, nod=%d",
                    idx,
                    name,
                    ead,
                    nos,
                    ste,
                    nod,
                )
                time_prog = TimeProgram(idx, name, ead, nos, ste, nod)
                # read the single time program entries for each day
                for day, num in [
                    (day, num) for day in range(nod) for num in range(ead)
                ]:
                    resp = (
                        await self.read_response_async()
                    )  # e.g. "PRE,PR=0,DAY=2,EV=1,ST=1,BEG=03:30,END=22:00"
                    m = re.match(PRD_RESP[1].format(idx, day, num), resp)
                    if not m:
                        raise IOError(
                            "invalid response for PRD command [{!r}]".format(resp)
                        )
                    # extract data (ST, BEG, END)
                    st, beg, end = m.group(1, 2, 3)
                    _LOGGER.debug(
                        "[idx=%d, day=%d, entry=%d]: st=%s, beg=%s, end=%s",
                        idx,
                        day,
                        num,
                        st,
                        beg,
                        end,
                    )
                    time_prog.set_entry(day, num, TimeProgEntry.from_str(st, beg, end))
                return time_prog
            except Exception as ex:
                _LOGGER.error("query for time program with entries failed: %s", ex)
                raise

    async def get_time_prog_async(
        self, idx: int, with_entries: bool = True
    ) -> TimeProgram:
        """Return a specific time program (specified by their index) together with their time program entries
        (if desired) from the heat pump.

        :param idx: The time program index.
        :type idx: int
        :param with_entries: Determines whether also the single time program entries should be requested or not.
            Default is :const:`True`.
        :type with_entries: bool

        :returns: The requested time program as :class:`~htheatpump.httimeprog.TimeProgram`.
        :rtype: ``TimeProgram``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        assert isinstance(idx, int)
        assert isinstance(with_entries, bool)
        return (
            await self._get_time_prog_with_entries_async(idx)
            if with_entries
            else await self._get_time_prog_async(idx)
        )

    async def get_time_prog_entry_async(
        self, idx: int, day: int, num: int
    ) -> TimeProgEntry:
        """Return a specific time program entry (specified by time program index, day and entry-of-day)
        of the heat pump.

        :param idx: The time program index.
        :type idx: int
        :param day: The day of the time program entry (inside the specified time program).
        :type day: int
        :param num: The number of the time program entry (of the specified day).
        :type num: int

        :returns: The requested time program entry as :class:`~htheatpump.httimeprog.TimeProgEntry`.
        :rtype: ``TimeProgEntry``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            assert isinstance(idx, int)
            assert isinstance(day, int)
            assert isinstance(num, int)
            # send PRE request to the heat pump
            await self.send_request_async(PRE_CMD[0].format(idx, day, num))
            # ... and wait for the response
            try:
                resp = (
                    await self.read_response_async()
                )  # e.g. "PRE,PR=0,DAY=2,EV=1,ST=1,BEG=03:30,END=22:00"
                m = re.match(PRE_RESP.format(idx, day, num), resp)
                if not m:
                    raise IOError(
                        "invalid response for PRE command [{!r}]".format(resp)
                    )
                # extract data (ST, BEG, END)
                st, beg, end = m.group(1, 2, 3)
                _LOGGER.debug(
                    "[idx=%d, day=%d, entry=%d]: st=%s, beg=%s, end=%s",
                    idx,
                    day,
                    num,
                    st,
                    beg,
                    end,
                )
                return TimeProgEntry.from_str(st, beg, end)
            except Exception as ex:
                _LOGGER.error("query for time program entry failed: %s", ex)
                raise

    async def set_time_prog_entry_async(
        self, idx: int, day: int, num: int, entry: TimeProgEntry
    ) -> TimeProgEntry:
        """Set a specific time program entry (specified by time program index, day and entry-of-day)
        of the heat pump.

        :param idx: The time program index.
        :type idx: int
        :param day: The day of the time program entry (inside the specified time program).
        :type day: int
        :param num: The number of the time program entry (of the specified day).
        :type num: int
        :param entry: The new time program entry as :class:`~htheatpump.httimeprog.TimeProgEntry`.
        :type entry: TimeProgEntry

        :returns: The changed time program entry :class:`~htheatpump.httimeprog.TimeProgEntry`.
        :rtype: ``TimeProgEntry``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        async with self._lock:
            assert isinstance(idx, int)
            assert isinstance(day, int)
            assert isinstance(num, int)
            assert isinstance(entry, TimeProgEntry)
            # send PRE command to the heat pump
            await self.send_request_async(
                PRE_CMD[1].format(
                    idx,
                    day,
                    num,
                    entry.state,
                    entry.period.start_str,
                    entry.period.end_str,
                )
            )
            # ... and wait for the response
            try:
                resp = (
                    await self.read_response_async()
                )  # e.g. "PRE,PR=0,DAY=2,EV=1,ST=1,BEG=03:30,END=22:00"
                m = re.match(PRE_RESP.format(idx, day, num), resp)
                if not m:
                    raise IOError(
                        "invalid response for PRE command [{!r}]".format(resp)
                    )
                # extract data (ST, BEG, END)
                st, beg, end = m.group(1, 2, 3)
                _LOGGER.debug(
                    "[idx=%d, day=%d, entry=%d]: st=%s, beg=%s, end=%s",
                    idx,
                    day,
                    num,
                    st,
                    beg,
                    end,
                )
                return TimeProgEntry.from_str(st, beg, end)
            except Exception as ex:
                _LOGGER.error("set time program entry failed: %s", ex)
                raise

    async def set_time_prog_async(self, time_prog: TimeProgram) -> TimeProgram:
        """Set all time program entries of a specific time program. Any non-specified entry
        (which is :const:`None`) in the time program will be requested from the heat pump.
        The returned :class:`~htheatpump.httimeprog.TimeProgram` instance includes therefore
        all entries of this time program.

        :param time_prog: The given time program as :class:`~htheatpump.httimeprog.TimeProgram`.
        :type time_prog: TimeProgram

        :returns: The time program as :class:`~htheatpump.httimeprog.TimeProgram` including all time program entries.
        :rtype: ``TimeProgram``
        :raises IOError:
            Will be raised when the serial connection is not open or received an incomplete/invalid
            response (e.g. broken data stream, invalid checksum).
        """
        assert isinstance(time_prog, TimeProgram)
        ret = copy.deepcopy(time_prog)
        for day, num in [
            (day, num)
            for day in range(time_prog.number_of_days)
            for num in range(time_prog.entries_a_day)
        ]:
            entry = time_prog.entry(day, num)
            _LOGGER.debug(
                "[idx=%d, day=%d, entry=%d]: %s", time_prog.index, day, num, entry
            )
            if entry is not None:
                entry = await self.set_time_prog_entry_async(
                    time_prog.index, day, num, entry
                )
            else:
                entry = await self.get_time_prog_entry_async(time_prog.index, day, num)
            ret.set_entry(day, num, entry)
        return ret


# ------------------------------------------------------------------------------------------------------------------- #
# Exported symbols
# ------------------------------------------------------------------------------------------------------------------- #

__all__ = ["AioHtHeatpump"]
