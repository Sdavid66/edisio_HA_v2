"""Plateforme binary_sensor : etat ON/OFF des emetteurs/contacts Edisio."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN, SIGNAL_DISCOVERY, SIGNAL_REMOVED, SIGNAL_RX, SIGNAL_STATUS,
)
from .device import emitter_device_info, gateway_device_info
from .gateway import EdisioGateway


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    gw: EdisioGateway = hass.data[DOMAIN][entry.entry_id]
    seen: set[str] = set()

    async_add_entities([EdisioGatewayConnectivity(gw, entry.entry_id)])

    @callback
    def _discovered(data: dict) -> None:
        kinds = data.get("kinds") or set()
        if "binary" not in kinds and data.get("value") not in ("on", "off"):
            return
        dev_id = data["id"]
        if dev_id in seen:
            return
        seen.add(dev_id)
        async_add_entities([EdisioBinarySensor(
            entry.entry_id, dev_id, data.get("value") == "on", data.get("name"),
            hub_device_id=gw.hub_device_id,
        )])

    @callback
    def _removed(dev_id: str) -> None:
        seen.discard(dev_id)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DISCOVERY, _discovered)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_REMOVED, _removed)
    )


class EdisioBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, entry_id: str, dev_id: str, initial: bool,
                 name: str | None = None, hub_device_id: str | None = None):
        self._dev_id = dev_id
        self._attr_is_on = initial
        self._attr_name = f"Edisio {dev_id} etat"
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_state"
        self._attr_device_info = emitter_device_info(
            entry_id, dev_id, name, hub_device_id=hub_device_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_RX}_{self._dev_id}", self._update
            )
        )

    @callback
    def _update(self, data: dict) -> None:
        if data.get("value") == "on":
            self._attr_is_on = True
        elif data.get("value") == "off":
            self._attr_is_on = False
        else:
            return
        self.async_write_ha_state()


class EdisioGatewayConnectivity(BinarySensorEntity):
    """Etat de connexion du dongle (entite de diagnostic du hub)."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, gateway: EdisioGateway, entry_id: str):
        self._gw = gateway
        self._attr_name = "Edisio passerelle connectee"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_connected"
        self._attr_device_info = gateway_device_info(entry_id, gateway.port)

    @property
    def is_on(self) -> bool:
        return self._gw.connected

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STATUS, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()
