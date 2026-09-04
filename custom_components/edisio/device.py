"""Helpers d'appareils : passerelle (hub) et emetteurs rattaches au hub."""
from __future__ import annotations

from awesomeversion import AwesomeVersion

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MANUFACTURER

# HA 2026.9 deprecie DeviceInfo['via_device'] (identifiant) au profit de
# via_device_id (id de registre du parent) ; l'ancien champ devient une erreur
# a partir de 2027.8. On bascule selon la version installee pour rester
# compatible avec les versions anterieures (min declare : 2026.3).
_USE_VIA_DEVICE_ID = AwesomeVersion(HA_VERSION) >= AwesomeVersion("2026.9.0")


def gateway_id(entry_id: str) -> tuple[str, str]:
    """Identifiant de l'appareil passerelle (hub) pour une entree donnee."""
    return (DOMAIN, f"gateway_{entry_id}")


def via_hub_kwargs(entry_id: str, hub_device_id: str | None) -> dict:
    """Rattachement d'un appareil au hub, compatible avant/apres HA 2026.9.

    Renvoie ``{'via_device_id': ...}`` sur HA >= 2026.9 (si l'id du hub est
    connu), sinon l'ancien ``{'via_device': (identifiant)}``. A eclater dans un
    ``DeviceInfo(**...)``.
    """
    if _USE_VIA_DEVICE_ID and hub_device_id:
        return {"via_device_id": hub_device_id}
    return {"via_device": gateway_id(entry_id)}


def gateway_device_info(entry_id: str, port: str) -> DeviceInfo:
    """DeviceInfo de la passerelle, partagee par ses entites de diagnostic."""
    return DeviceInfo(
        identifiers={gateway_id(entry_id)},
        manufacturer=MANUFACTURER,
        name=f"Passerelle Edisio ({port})",
    )


def emitter_device_info(
    entry_id: str, dev_id: str, name: str | None = None, model: str | None = None,
    hub_device_id: str | None = None,
) -> DeviceInfo:
    """DeviceInfo d'un emetteur decouvert, rattache au hub."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"emitter_{dev_id}")},
        manufacturer=MANUFACTURER,
        name=name or f"Edisio {dev_id}",
        model=model,
        **via_hub_kwargs(entry_id, hub_device_id),
    )
