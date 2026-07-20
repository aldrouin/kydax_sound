"""UDP client for the MusiSelect music source device.

Protocol knowledge is limited (see PROTOCOL.md): ASCII commands such as
``PACINI diffSpecial 2`` sent as UDP datagrams; the device is not known to
respond. Commands are therefore fire-and-forget, but any datagram the device
does send back is logged to help map the protocol.
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 2325


class MusiSelectError(Exception):
    """Could not send to the MusiSelect device."""


class _MusiSelectDatagramProtocol(asyncio.DatagramProtocol):
    """Log whatever the device sends back; nothing is expected."""

    def datagram_received(self, data: bytes, addr) -> None:
        _LOGGER.info(
            "MusiSelect sent data (protocol mapping): %r",
            data.decode("ascii", errors="replace"),
        )

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("MusiSelect UDP error: %s", exc)


class MusiSelectClient:
    """Fire-and-forget command sender for one MusiSelect device."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        self._host = host
        self._port = port
        self._transport: asyncio.DatagramTransport | None = None

    @property
    def connected(self) -> bool:
        return self._transport is not None and not self._transport.is_closing()

    async def async_connect(self) -> None:
        if self.connected:
            return
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            _MusiSelectDatagramProtocol,
            remote_addr=(self._host, self._port),
        )

    def disconnect(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def async_send(self, command: str) -> None:
        """Send one ASCII command (no terminator, as the old code did)."""
        try:
            if not self.connected:
                await self.async_connect()
            self._transport.sendto(command.encode("ascii"))
        except OSError as err:
            raise MusiSelectError(
                f"cannot send to MusiSelect {self._host}:{self._port}: {err}"
            ) from err
        _LOGGER.debug(
            "MusiSelect %s:%s: sent %s", self._host, self._port, command
        )
