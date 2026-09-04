"""Plateforme sensor : temperature et batterie des modules Edisio (decouverte auto)."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEV_ID, CONF_KIND, CONF_NAME, DOMAIN, KIND_REMOTE, SIGNAL_DISCOVERY,
    SIGNAL_REMOVED, SIGNAL_RX, SIGNAL_STATUS, SUBENTRY_TYPE_DEVICE,
)
from .device import emitter_device_info, gateway_device_info
from .gateway import EdisioGateway


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    gw: EdisioGateway = hass.data[DOMAIN][entry.entry_id]
    seen: set[str] = set()

    # Capteurs de diagnostic de la passerelle (hub)
    async_add_entities([
        EdisioGatewayPortSensor(gw, entry.entry_id),
        EdisioGatewayPairedSensor(gw, entry.entry_id),
        EdisioGatewayFramesSensor(gw, entry.entry_id),
        EdisioGatewayLastFrameSensor(gw, entry.entry_id),
    ])

    # Batterie des telecommandes (sous-entrees)
    for sub_id, sub in entry.subentries.items():
        if sub.subentry_type == SUBENTRY_TYPE_DEVICE \
                and sub.data.get(CONF_KIND) == KIND_REMOTE:
            async_add_entities(
                [EdisioBatterySensor(entry.entry_id, sub.data[CONF_DEV_ID],
                                     sub.data.get(CONF_NAME),
                                     hub_device_id=gw.hub_device_id)],
                config_subentry_id=sub_id,
            )

    @callback
    def _discovered(data: dict) -> None:
        dev_id = data["id"]
        kinds = data.get("kinds") or set()
        new = []
        has_batt = "battery" in kinds or data.get("battery") is not None
        has_temp = "temperature" in kinds or "temperature" in data
        name = data.get("name")
        if has_batt and f"{dev_id}_battery" not in seen:
            seen.add(f"{dev_id}_battery")
            new.append(EdisioBatterySensor(entry.entry_id, dev_id, name,
                                           data.get("battery"),
                                           hub_device_id=gw.hub_device_id))
        if has_temp and f"{dev_id}_temp" not in seen:
            seen.add(f"{dev_id}_temp")
            new.append(EdisioTemperatureSensor(entry.entry_id, dev_id, name,
                                               data.get("temperature"),
                                               hub_device_id=gw.hub_device_id))
        if new:
            async_add_entities(new)

    @callback
    def _removed(dev_id: str) -> None:
        seen.discard(f"{dev_id}_battery")
        seen.discard(f"{dev_id}_temp")

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DISCOVERY, _discovered)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_REMOVED, _removed)
    )


class _Base(SensorEntity):
    _attr_should_poll = False

    def __init__(self, entry_id: str, dev_id: str, name: str | None = None,
                 hub_device_id: str | None = None):
        self._dev_id = dev_id
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
        raise NotImplementedError


class EdisioBatterySensor(_Base):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, entry_id: str, dev_id: str, name: str | None = None,
                 initial: int | None = None, hub_device_id: str | None = None):
        super().__init__(entry_id, dev_id, name, hub_device_id)
        self._attr_name = f"Edisio {dev_id} batterie"
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_battery"
        if initial is not None:
            self._attr_native_value = initial

    @callback
    def _update(self, data: dict) -> None:
        if data.get("battery") is not None:
            self._attr_native_value = data["battery"]
            self.async_write_ha_state()


class EdisioTemperatureSensor(_Base):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry_id: str, dev_id: str, name: str | None = None,
                 initial: float | None = None, hub_device_id: str | None = None):
        super().__init__(entry_id, dev_id, name, hub_device_id)
        self._attr_name = f"Edisio {dev_id} temperature"
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_temperature"
        if initial is not None:
            self._attr_native_value = initial

    @callback
    def _update(self, data: dict) -> None:
        if "temperature" in data:
            self._attr_native_value = data["temperature"]
            self.async_write_ha_state()


class _GatewaySensor(SensorEntity):
    """Base des capteurs de diagnostic de la passerelle (hub)."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, gateway: EdisioGateway, entry_id: str):
        self._gw = gateway
        self._attr_device_info = gateway_device_info(entry_id, gateway.port)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STATUS, self._refresh)
        )

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class EdisioGatewayPortSensor(_GatewaySensor):
    _attr_translation_key = "gateway_port"
    _attr_icon = "mdi:usb-port"

    def __init__(self, gateway, entry_id):
        super().__init__(gateway, entry_id)
        self._attr_name = "Edisio passerelle port"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_port"

    @property
    def native_value(self) -> str:
        return self._gw.port


class EdisioGatewayPairedSensor(_GatewaySensor):
    _attr_icon = "mdi:remote"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, gateway, entry_id):
        super().__init__(gateway, entry_id)
        self._attr_name = "Edisio passerelle emetteurs appaires"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_paired"

    @property
    def native_value(self) -> int:
        return self._gw.paired_count


class EdisioGatewayFramesSensor(_GatewaySensor):
    _attr_icon = "mdi:radio-tower"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, gateway, entry_id):
        super().__init__(gateway, entry_id)
        self._attr_name = "Edisio passerelle trames recues"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_frames"

    @property
    def native_value(self) -> int:
        return self._gw.frames_received


class EdisioGatewayLastFrameSensor(_GatewaySensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, gateway, entry_id):
        super().__init__(gateway, entry_id)
        self._attr_name = "Edisio passerelle derniere trame"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_last_frame"

    @property
    def native_value(self):
        return self._gw.last_frame_at
