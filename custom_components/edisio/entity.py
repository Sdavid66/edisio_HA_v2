"""Classe de base pour les recepteurs Edisio pilotables."""
from __future__ import annotations

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.device_registry import DeviceInfo

from . import models
from .const import (
    CONF_CHANNEL, CONF_CLOSE_CHANNEL, CONF_DEVICES, CONF_EDISIO_ID,
    CONF_FUNCTIONS, CONF_MODEL, CONF_NAME, DOMAIN, EDRB4_PAIRS, MODEL_EDRB4,
    SUBENTRY_TYPE_DEVICE,
)
from .device import via_hub_kwargs
from .gateway import EdisioGateway


def _expand_edrb4(data: dict) -> list[dict]:
    """EDR-B4 : chaque paire de voies (1&2, 3&4) = 2 interrupteurs OU 1 volet.

    En volet, une seule entite ``cover`` pilote la paire : ouverture sur la 1re
    voie, fermeture sur la 2e (``close_channel``).
    """
    base = data[CONF_NAME]
    functions = data.get(CONF_FUNCTIONS) or {}
    out: list[dict] = []
    for key, (c_open, c_close) in EDRB4_PAIRS.items():
        if functions.get(key) == "cover":
            out.append({
                CONF_NAME: f"{base} Volet {c_open}-{c_close}",
                CONF_MODEL: MODEL_EDRB4,
                CONF_CHANNEL: c_open,
                CONF_CLOSE_CHANNEL: c_close,
                CONF_EDISIO_ID: data[CONF_EDISIO_ID],
                "platform": "cover",
            })
        else:  # 2 interrupteurs independants
            for ch in (c_open, c_close):
                out.append({
                    CONF_NAME: f"{base} C{ch}",
                    CONF_MODEL: MODEL_EDRB4,
                    CONF_CHANNEL: ch,
                    CONF_EDISIO_ID: data[CONF_EDISIO_ID],
                    "platform": "switch",
                })
    return out


def expand_channels(data: dict) -> list[dict]:
    """Developpe un module (sous-entree) en un dict par entite (avec sa plateforme)."""
    model = models.model(data[CONF_MODEL])
    if not model:
        return []
    if data[CONF_MODEL] == MODEL_EDRB4:
        return _expand_edrb4(data)
    base = data[CONF_NAME]
    multi = len(model["channels"]) > 1
    return [
        {
            CONF_NAME: f"{base} C{ch}" if multi else base,
            CONF_MODEL: data[CONF_MODEL],
            CONF_CHANNEL: ch,
            CONF_EDISIO_ID: data[CONF_EDISIO_ID],
            "platform": model["platform"],
        }
        for ch in model["channels"]
    ]


def model_emitter_mid(model: dict) -> str:
    """MID (octet du type d'emetteur emule) d'un modele, lu dans ses trames.

    Les templates ont la forme ``6C7663#ID##GROUP#<MID>1E0100...`` : l'octet
    juste apres ``#GROUP#`` est le MID. Sert a envoyer la trame d'apprentissage
    avec le bon MID (ex. ``01`` pour les micro-modules, ``05`` pour le rail DIN).
    """
    for frame in (model.get("frames") or {}).values():
        if "#GROUP#" in frame:
            return frame.split("#GROUP#", 1)[1][:2]
    return "04"


class EdisioReceiver(Entity):
    """Base : detient la config, le modele et l'emission de trames."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, gateway: EdisioGateway, dev: dict):
        self._gateway = gateway
        self._dev = dev
        self._model = models.model(dev[CONF_MODEL])
        self._id = dev[CONF_EDISIO_ID]
        self._channel = dev.get(CONF_CHANNEL, 1)
        self._close_channel = dev.get(CONF_CLOSE_CHANNEL)
        self._attr_name = dev[CONF_NAME]
        platform = dev.get("platform") or self._model["platform"]
        self._attr_unique_id = (
            f"{DOMAIN}_{self._id}_{self._channel}_{platform}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._id)},
            manufacturer="Edisio",
            model=self._model["name"],
            name=dev[CONF_NAME].rsplit(" C", 1)[0],
            **via_hub_kwargs(gateway.entry.entry_id, gateway.hub_device_id),
        )

    async def _send(self, action: str, slider: int | None = None) -> None:
        await self._send_ch(action, self._channel, slider)

    async def _send_ch(self, action: str, channel: int,
                       slider: int | None = None) -> None:
        """Emet une action sur une voie precise (utile pour les volets EDR-B4)."""
        template = self._model["frames"].get(action)
        if not template:
            return
        await self._gateway.async_send_action(
            self._id, channel, action, template, slider
        )

    @staticmethod
    def groups_for(entry, platform: str) -> list[tuple[str | None, list[dict]]]:
        """Récepteurs d'une plateforme, groupés par source.

        Retourne une liste de couples ``(config_subentry_id | None, [dicts])`` :
        - ``None`` pour les récepteurs « legacy » stockés dans les options
          (compat : installations d'avant les sous-entrées) ;
        - l'``id`` de la sous-entrée pour ceux ajoutés via *Ajouter un appareil*.
        """
        groups: list[tuple[str | None, list[dict]]] = []

        legacy = [
            d for d in entry.options.get(CONF_DEVICES, [])
            if models.model(d[CONF_MODEL])
            and models.model(d[CONF_MODEL])["platform"] == platform
        ]
        if legacy:
            groups.append((None, legacy))

        for sub_id, sub in entry.subentries.items():
            if sub.subentry_type != SUBENTRY_TYPE_DEVICE:
                continue
            data = dict(sub.data)
            if not models.model(data.get(CONF_MODEL)):
                continue  # telecommandes (kind=remote) et modeles inconnus ignores
            chans = [c for c in expand_channels(data) if c["platform"] == platform]
            if chans:
                groups.append((sub_id, chans))

        return groups

    @staticmethod
    def receiver_modules(entry) -> list[tuple[str | None, dict]]:
        """Un couple ``(sub_id, data_module)`` par recepteur, dedupe par ID.

        Sert au bouton d'appairage : une entree par module physique (peu importe
        le nombre de voies), qu'il vienne des options « legacy » ou d'une
        sous-entree. Les telecommandes (sans modele) sont ignorees.
        """
        out: list[tuple[str | None, dict]] = []
        seen: set[str] = set()

        def _add(sub_id: str | None, data: dict) -> None:
            if not models.model(data.get(CONF_MODEL)):
                return
            eid = data.get(CONF_EDISIO_ID)
            if not eid or eid in seen:
                return
            seen.add(eid)
            out.append((sub_id, data))

        for d in entry.options.get(CONF_DEVICES, []):
            _add(None, d)
        for sub_id, sub in entry.subentries.items():
            if sub.subentry_type == SUBENTRY_TYPE_DEVICE:
                _add(sub_id, dict(sub.data))
        return out
