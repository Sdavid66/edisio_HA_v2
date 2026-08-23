"""Integration Edisio pour Home Assistant."""
from __future__ import annotations

import json
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integration

from . import jeedom_import, models
from .device import gateway_id
from .const import (
    CONF_DEV_ID, CONF_DEVICES, CONF_EDISIO_ID, CONF_KIND, CONF_PORT, DOMAIN,
    DONGLE_RFPLAYER, INCLUSION_TIMEOUT, KIND_REMOTE, PLATFORMS, SERVICE_EXCLUDE,
    SERVICE_IMPORT, SERVICE_INCLUSION, SERVICE_LEARN, SERVICE_SEND_RAW,
    SUBENTRY_TYPE_DEVICE,
)
from .gateway import EdisioGateway

_LOGGER = logging.getLogger(__name__)


def _read_text(path: str) -> str:
    """Lecture synchrone d'un fichier texte (appelee dans un executor)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Precharge le catalogue de modeles (lecture fichier hors boucle d'evenements).
    await models.async_load_catalog(hass)

    gateway = EdisioGateway(hass, entry)
    await gateway.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = gateway

    # Declare les telecommandes (sous-entrees) : leurs trames sont routees vers
    # les entites bouton, sans redeclencher de carte de decouverte.
    gateway.async_set_known_remotes({
        sub.data[CONF_DEV_ID]
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_DEVICE
        and sub.data.get(CONF_KIND) == KIND_REMOTE
        and sub.data.get(CONF_DEV_ID)
    })

    await _async_register_hub(hass, entry, gateway)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Les plateformes ont branche leurs listeners SIGNAL_DISCOVERY : on peut
    # maintenant re-annoncer les emetteurs deja connus pour recreer leurs
    # entites (sondes, binaires, evenements decouverts) apres un redemarrage.
    gateway.async_announce_known()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _register_services(hass)
    return True


async def _async_register_hub(
    hass: HomeAssistant, entry: ConfigEntry, gateway: EdisioGateway
) -> None:
    """Enregistre la passerelle comme appareil 'hub' (comme un coordinateur)."""
    integration = await async_get_integration(hass, DOMAIN)
    if gateway.dongle == DONGLE_RFPLAYER:
        manufacturer = "GCE Electronics"
        model = gateway.dongle_description or "RFPlayer RFP1000"
    else:
        manufacturer = "Edisio"
        model = gateway.dongle_description or "Dongle USB Edisio 868 MHz"
    if gateway.dongle_vidpid:
        model = f"{model} ({gateway.dongle_vidpid})"
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={gateway_id(entry.entry_id)},
        manufacturer=manufacturer,
        name=f"Passerelle Edisio ({gateway.port})",
        model=model,
        sw_version=str(integration.version) if integration.version else None,
        entry_type=dr.DeviceEntryType.SERVICE,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        gateway: EdisioGateway = hass.data[DOMAIN].pop(entry.entry_id)
        await gateway.async_stop()
        if not hass.data[DOMAIN]:
            for service in (SERVICE_LEARN, SERVICE_SEND_RAW,
                            SERVICE_INCLUSION, SERVICE_EXCLUDE, SERVICE_IMPORT):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Autorise la suppression d'un appareil depuis l'UI.

    - Emetteurs decouverts (identifiant ``(edisio, emitter_<id>)``) : oublies dans
      la passerelle (= exclusion).
    - Recepteurs « legacy » stockes dans les options (identifiant
      ``(edisio, <edisio_id>)``) : retires des options, sinon les plateformes les
      recreent au redemarrage et l'appareil « reapparait ».
    """
    gateway: EdisioGateway = hass.data[DOMAIN][entry.entry_id]
    for domain, ident in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        if ident.startswith("emitter_"):
            dev_id = ident[len("emitter_"):]
            if dev_id in gateway.accepted:
                await gateway.async_forget(dev_id)
        elif ident.startswith("gateway_"):
            continue  # la passerelle (hub) n'est pas supprimable individuellement
        else:
            # Recepteur legacy (options) : le retirer pour que la suppression tienne.
            edisio_id = ident
            devices = entry.options.get(CONF_DEVICES, [])
            kept = [d for d in devices if d.get(CONF_EDISIO_ID) != edisio_id]
            if len(kept) != len(devices):
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_DEVICES: kept}
                )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge l'entree quand la config (modules pilotables) change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_INCLUSION):
        return

    def _gateways() -> list[EdisioGateway]:
        return list(hass.data[DOMAIN].values())

    async def _handle_inclusion(call: ServiceCall) -> None:
        enabled = call.data.get("enable", True)
        duration = call.data.get("duration", INCLUSION_TIMEOUT)
        for gw in _gateways():
            gw.async_set_inclusion(enabled, duration)

    async def _handle_exclude(call: ServiceCall) -> None:
        dev_id = call.data["device_id"]
        ban = call.data.get("ban", False)
        for gw in _gateways():
            await gw.async_forget(dev_id, ban)

    async def _handle_learn(call: ServiceCall) -> None:
        await _gateways()[0].async_learn(call.data["edisio_id"],
                                         call.data.get("emitter_mid", "04"))

    async def _handle_send_raw(call: ServiceCall) -> None:
        # Trame simple, ou plusieurs trames séparées par « && » (comme les
        # templates du catalogue) émises dans l'ordre, timing Edisio.
        frames = [f for f in call.data["frame"].split("&&") if f]
        await _gateways()[0].async_send(frames)

    async def _handle_import(call: ServiceCall) -> None:
        path = call.data["path"]
        raw = await hass.async_add_executor_job(_read_text, path)
        try:
            data = jeedom_import.load_import(json.loads(raw))
        except (json.JSONDecodeError, jeedom_import.ImportError_) as err:
            _LOGGER.error("Import Edisio : fichier invalide (%s)", err)
            return
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            _LOGGER.error("Import Jeedom : aucune entree Edisio configuree")
            return
        entry = entries[0]
        devices = list(entry.options.get(CONF_DEVICES, []))
        keys = {(d["edisio_id"], d.get("channel", 1)) for d in devices}
        added = 0
        for d in data["receivers"]:
            if (d["edisio_id"], d["channel"]) not in keys:
                devices.append(d)
                keys.add((d["edisio_id"], d["channel"]))
                added += 1
        gateway = hass.data[DOMAIN].get(entry.entry_id)
        emit_added = 0
        if gateway is not None and data["emitters"]:
            emit_added = await gateway.async_import_emitters(data["emitters"])
        _LOGGER.info("Import Jeedom : %d recepteur(s), %d emetteur(s) ajoutes",
                     added, emit_added)
        # Met a jour les options -> declenche le rechargement de l'entree.
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_DEVICES: devices})

    hass.services.async_register(
        DOMAIN, SERVICE_INCLUSION, _handle_inclusion,
        schema=vol.Schema({
            vol.Optional("enable", default=True): cv.boolean,
            vol.Optional("duration", default=INCLUSION_TIMEOUT):
                vol.All(int, vol.Range(0, 600)),
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXCLUDE, _handle_exclude,
        schema=vol.Schema({
            vol.Required("device_id"): cv.string,
            vol.Optional("ban", default=False): cv.boolean,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LEARN, _handle_learn,
        schema=vol.Schema({
            vol.Required("edisio_id"): cv.string,
            vol.Optional("emitter_mid", default="04"): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_RAW, _handle_send_raw,
        schema=vol.Schema({vol.Required("frame"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT, _handle_import,
        schema=vol.Schema({vol.Required("path"): cv.string}),
    )
