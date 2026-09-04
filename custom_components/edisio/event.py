"""Plateforme event : appuis sur les telecommandes Edisio (pour automatisations)."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_BUTTONS, CONF_CODE, CONF_DEV_ID, CONF_KIND, CONF_NAME,
    CONF_REMOTE_MODEL, DOMAIN, KIND_REMOTE, SIGNAL_DISCOVERY, SIGNAL_REMOVED,
    SIGNAL_RX, SUBENTRY_TYPE_DEVICE,
)
from .device import emitter_device_info

EVENT_TYPES = ["on", "off", "toggle", "up", "down", "stop"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    gw = hass.data[DOMAIN][entry.entry_id]
    seen: set[str] = set()

    # Telecommandes (sous-entrees) : une entite event par bouton appris.
    for sub_id, sub in entry.subentries.items():
        if sub.subentry_type != SUBENTRY_TYPE_DEVICE:
            continue
        if sub.data.get(CONF_KIND) != KIND_REMOTE:
            continue
        dev_id = sub.data[CONF_DEV_ID]
        remote_name = sub.data.get(CONF_NAME)
        remote_model = sub.data.get(CONF_REMOTE_MODEL)
        buttons = [
            EdisioButtonEvent(entry.entry_id, dev_id, remote_name, remote_model,
                              b[CONF_CODE], b[CONF_NAME],
                              hub_device_id=gw.hub_device_id)
            for b in sub.data.get(CONF_BUTTONS, [])
        ]
        if buttons:
            async_add_entities(buttons, config_subentry_id=sub_id)

    @callback
    def _discovered(data: dict) -> None:
        kinds = data.get("kinds") or set()
        val = data.get("value")
        is_event = "event" in kinds or (isinstance(val, str) and val in EVENT_TYPES)
        if not is_event:
            return
        dev_id = data["id"]
        if dev_id in seen:
            return
        seen.add(dev_id)
        async_add_entities([EdisioRemoteEvent(entry.entry_id, dev_id,
                                              data.get("name"),
                                              hub_device_id=gw.hub_device_id)])

    @callback
    def _removed(dev_id: str) -> None:
        seen.discard(dev_id)

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DISCOVERY, _discovered)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_REMOVED, _removed)
    )


class EdisioRemoteEvent(EventEntity):
    _attr_should_poll = False
    _attr_event_types = EVENT_TYPES

    def __init__(self, entry_id: str, dev_id: str, name: str | None = None,
                 hub_device_id: str | None = None):
        self._dev_id = dev_id
        self._attr_name = f"Edisio {dev_id} telecommande"
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_remote"
        self._attr_device_info = emitter_device_info(
            entry_id, dev_id, name, hub_device_id=hub_device_id)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_RX}_{self._dev_id}", self._handle
            )
        )

    @callback
    def _handle(self, data: dict) -> None:
        val = data.get("value")
        if val in EVENT_TYPES:
            self._trigger_event(val, {"button": data.get("button"),
                                      "cmd": data.get("cmd")})
            self.async_write_ha_state()


class EdisioButtonEvent(EventEntity):
    """Un bouton d'une telecommande (sous-entree) : declenche sur son code."""

    _attr_should_poll = False
    _attr_event_types = EVENT_TYPES

    def __init__(self, entry_id: str, dev_id: str, remote_name: str | None,
                 remote_model: str | None, code: str, name: str,
                 hub_device_id: str | None = None):
        self._dev_id = dev_id
        self._code = code
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_btn_{code}"
        self._attr_device_info = emitter_device_info(
            entry_id, dev_id, remote_name, remote_model,
            hub_device_id=hub_device_id,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_RX}_{self._dev_id}", self._handle
            )
        )

    @callback
    def _handle(self, data: dict) -> None:
        if data.get("button") != self._code:
            return
        val = data.get("value")
        event_type = val if val in EVENT_TYPES else "toggle"
        self._trigger_event(event_type, {"cmd": data.get("cmd")})
        self.async_write_ha_state()
