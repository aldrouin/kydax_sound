import asyncio
import logging
from homeassistant.helpers import discovery
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .const import DOMAIN, CHANNELS, UDP_IP, UDP_PORT

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    async def async_fete_commande(service):
        """Update the preset on the device."""
        preset_number = service.data.get("preset_number")
        preset_command = f"LP {preset_number}\r"

        # Create a UDP connection
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(), remote_addr=(UDP_IP, UDP_PORT)
        )

        # Send the command and wait for the response
        transport.sendto(preset_command.encode("ASCII"))
        # preset_received_data, addr = await protocol.receive()

        transport.close()
        # preset_decoded_data = preset_received_data.decode("ASCII")

        # Create another UDP connection
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(), remote_addr=("10.35.82.24", 2325)
        )

        # Send the command and wait for the response
        command = "PACINI diffSpecial 1"
        transport.sendto(command.encode("ASCII"))
        # received_data, addr = await protocol.receive()

        transport.close()
        # decoded_data = received_data.decode("ASCII")

    async def async_fete_anglais_commande(service):
        """Update the preset on the device."""
        preset_number = service.data.get("preset_number")
        preset_command = f"LP {preset_number}\r"

        # Create a UDP connection
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(), remote_addr=(UDP_IP, UDP_PORT)
        )

        # Send the command and wait for the response
        transport.sendto(preset_command.encode("ASCII"))
        #preset_received_data, addr = await protocol.receive()

        transport.close()
        # preset_decoded_data = preset_received_data.decode("ASCII")

        # Create another UDP connection
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(), remote_addr=("10.35.82.24", 2325)
        )



        # Send the command and wait for the response
        command = "PACINI diffSpecial 2"
        transport.sendto(command.encode("ASCII"))
        # received_data, addr = await protocol.receive()

        transport.close()
        # decoded_data = received_data.decode("ASCII")

    async def async_update_preset(service):
        """Update the preset on the device."""
        preset_number = service.data.get("preset_number")
        command = f"LP {preset_number}\r"

        # Create a UDP connection
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(), remote_addr=(UDP_IP, UDP_PORT)
        )

        # Send the command and wait for the response
        transport.sendto(command.encode("ASCII"))
        # received_data, addr = await protocol.receive()

        transport.close()
        # decoded_data = received_data.decode("ASCII")

    hass.services.async_register(DOMAIN, "update_preset", async_update_preset)
    hass.services.async_register(DOMAIN, "fete_commande", async_fete_commande)
    hass.services.async_register(DOMAIN, "fete_commande_anglais", async_fete_anglais_commande)
    await discovery.async_load_platform(hass, "switch", DOMAIN, {}, config)
    hass.async_add_executor_job(
        discovery.load_platform, hass, "media_player", DOMAIN, {}, config
    )
    # discovery.load_platform(hass, "switch", DOMAIN, {}, config)
    return True
