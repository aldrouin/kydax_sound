"""Asyncio UDP client for the Symetrix Jupiter control protocol.

See PROTOCOL.md at the repo root for the full protocol reference. In short:
ASCII commands terminated by <CR> sent as single UDP datagrams to port 48630,
one response datagram per command, plus optional unsolicited "push" datagrams
whose lines look like ``#00007=12321``.

The asyncio DatagramProtocol callbacks are wrapped here so the rest of the
integration only ever sees awaitable calls: ``await client.async_get(7122)``.
Requests are serialized (one in flight), with a timeout and retries because
UDP gives no delivery guarantee.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 48630
REQUEST_TIMEOUT = 1.0  # seconds to wait for a response datagram
REQUEST_RETRIES = 3  # total attempts per command

type PushCallback = Callable[[dict[int, int]], None]


class SymetrixError(Exception):
    """Could not communicate with the appliance (timeout, network error)."""


class SymetrixNakError(SymetrixError):
    """The appliance understood the command but rejected it (NAK)."""


class _SymetrixDatagramProtocol(asyncio.DatagramProtocol):
    """Thin adapter: forwards datagrams and errors to the client."""

    def __init__(self, client: SymetrixClient) -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr) -> None:
        self._client._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        # ICMP errors (e.g. port unreachable) surface here on some platforms.
        self._client._on_error(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._client._on_connection_lost(exc)


class SymetrixClient:
    """Request/response + push client for one Jupiter appliance."""

    def __init__(
        self, host: str, port: int = DEFAULT_PORT, push_callback: PushCallback | None = None
    ) -> None:
        self._host = host
        self._port = port
        self._push_callback = push_callback
        self._transport: asyncio.DatagramTransport | None = None
        self._lock = asyncio.Lock()
        self._response: asyncio.Future[str] | None = None
        self._last_error: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._transport is not None and not self._transport.is_closing()

    async def async_connect(self) -> None:
        """Open the UDP endpoint (does not exchange any packet by itself)."""
        if self.connected:
            return
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _SymetrixDatagramProtocol(self),
            remote_addr=(self._host, self._port),
        )

    def disconnect(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # --- low level ----------------------------------------------------------

    async def _async_request(self, command: str) -> str:
        """Send one command and return the raw response text (CRs stripped at ends).

        Serialized so responses can be matched to their command; retried on
        timeout since UDP may drop either the command or the response.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            last_exc: Exception | None = None
            for attempt in range(1, REQUEST_RETRIES + 1):
                try:
                    if not self.connected:
                        await self.async_connect()
                except OSError as err:
                    raise SymetrixError(
                        f"cannot open UDP endpoint to {self._host}:{self._port}: {err}"
                    ) from err
                self._last_error = None
                self._response = loop.create_future()
                self._transport.sendto(f"{command}\r".encode("ascii"))
                try:
                    response = await asyncio.wait_for(
                        self._response, REQUEST_TIMEOUT
                    )
                    _LOGGER.debug(
                        "Symetrix %s:%s: %s -> %s",
                        self._host,
                        self._port,
                        command,
                        response.replace("\r", " | "),
                    )
                    return response
                except TimeoutError:
                    last_exc = self._last_error or TimeoutError(
                        f"no response to {command!r} (attempt {attempt}/{REQUEST_RETRIES})"
                    )
                    _LOGGER.debug("Timeout waiting for response to %r", command)
                finally:
                    self._response = None
            raise SymetrixError(
                f"{self._host}:{self._port} did not answer {command!r} "
                f"after {REQUEST_RETRIES} attempts"
            ) from last_exc

    def _on_datagram(self, data: bytes) -> None:
        text = data.decode("ascii", errors="replace").strip("\r\n\x00 ")
        # Push datagrams are lines of "#<ctrl>=<pos>". We never send GSB2/GPU
        # (whose responses could also start with '#'), so the prefix is an
        # unambiguous discriminator.
        if text.startswith("#"):
            if self._push_callback is not None:
                values = self._parse_push(text)
                if values:
                    self._push_callback(values)
            return
        if self._response is not None and not self._response.done():
            self._response.set_result(text)
        else:
            _LOGGER.debug("Unexpected datagram from appliance: %r", text)

    @staticmethod
    def _parse_push(text: str) -> dict[int, int]:
        values: dict[int, int] = {}
        for line in text.split("\r"):
            line = line.strip()
            if not line.startswith("#") or "=" not in line:
                continue
            ctrl_str, _, pos_str = line[1:].partition("=")
            try:
                pos = int(pos_str)
                if pos >= 0:  # -0001 means "no such controller"
                    values[int(ctrl_str)] = pos
            except ValueError:
                _LOGGER.debug("Ignoring malformed push line: %r", line)
        return values

    def _on_error(self, exc: Exception) -> None:
        self._last_error = exc
        _LOGGER.debug("UDP error from %s:%s: %s", self._host, self._port, exc)

    def _on_connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            _LOGGER.debug("UDP endpoint closed with error: %s", exc)
        self._transport = None

    @staticmethod
    def _expect_ack(response: str, command: str) -> None:
        if response != "ACK":
            raise SymetrixNakError(f"appliance rejected {command!r}: {response!r}")

    # --- commands (PROTOCOL.md) ----------------------------------------------

    async def async_set(self, controller: int, position: int) -> None:
        """CS: set a controller to an absolute position (0-65535)."""
        command = f"CS {controller} {position}"
        self._expect_ack(await self._async_request(command), command)

    async def async_change(self, controller: int, amount: int) -> None:
        """CC: change a controller by a relative amount (sign = direction)."""
        command = f"CC {controller} {1 if amount >= 0 else 0} {abs(amount)}"
        self._expect_ack(await self._async_request(command), command)

    async def async_get(self, controller: int) -> int:
        """GS: read one controller position."""
        response = await self._async_request(f"GS {controller}")
        try:
            return int(response)
        except ValueError:
            raise SymetrixNakError(
                f"appliance rejected GS {controller}: {response!r}"
            ) from None

    async def async_get_block(self, start: int, count: int) -> dict[int, int]:
        """GSB: read up to 256 consecutive controllers in one exchange.

        Returns {controller: position}; nonexistent controllers (-0001) are
        omitted.
        """
        response = await self._async_request(f"GSB {start} {count}")
        if response == "NAK":
            raise SymetrixNakError(f"appliance rejected GSB {start} {count}")
        values: dict[int, int] = {}
        for offset, line in enumerate(response.split("\r")):
            line = line.strip()
            if not line:
                continue
            try:
                pos = int(line)
            except ValueError:
                _LOGGER.debug("Ignoring malformed GSB line: %r", line)
                continue
            if pos >= 0:
                values[start + offset] = pos
        return values

    async def async_load_preset(self, preset: int) -> None:
        """LP: load a preset."""
        command = f"LP {preset}"
        self._expect_ack(await self._async_request(command), command)

    async def async_get_preset(self) -> int:
        """GPR D: return the last loaded preset number (0 = none)."""
        response = await self._async_request("GPR D")
        if response.startswith("PrstD="):
            try:
                return int(response[len("PrstD=") :])
            except ValueError:
                pass
        raise SymetrixNakError(f"unexpected GPR response: {response!r}")

    async def async_flash(self) -> None:
        """FU: flash the front panel LEDs (harmless comms test)."""
        self._expect_ack(await self._async_request("FU"), "FU")

    async def async_ping(self) -> None:
        """Verify the appliance answers at all; raises SymetrixError otherwise.

        Uses GPR D because it is read-only and side-effect free, but accepts
        ANY response - even NAK proves the device is reachable (some
        firmware does not support GPR D itself). Only silence fails.
        """
        await self._async_request("GPR D")

    # --- push control ---------------------------------------------------------

    async def async_push_enable(self, low: int, high: int | None = None) -> None:
        """PUE: enable push for a controller or range (additive)."""
        command = f"PUE {low}" if high is None else f"PUE {low} {high}"
        self._expect_ack(await self._async_request(command), command)

    async def async_push_disable_all(self) -> None:
        """PUD: disable push for all controllers."""
        self._expect_ack(await self._async_request("PUD"), "PUD")

    async def async_push_clear(self) -> None:
        """PUC: drop pending unreported changes (use before enabling push)."""
        self._expect_ack(await self._async_request("PUC"), "PUC")

    async def async_push_refresh(self, low: int = 1, high: int = 10000) -> None:
        """PUR: force enabled controllers to push their current values."""
        command = f"PUR {low} {high}"
        self._expect_ack(await self._async_request(command), command)
