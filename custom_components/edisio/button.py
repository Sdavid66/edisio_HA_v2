"""Plateforme button : bouton « Appairer » (apprentissage) des recepteurs.

Envoie la trame d'apprentissage Edisio (``09 <MID> 1F000010``) avec le MID lu
dans le modele du recepteur. Evite le piege du MID par defaut : les
micro-modules s'apparient en MID ``01``, le rail DIN en ``05``, etc.
"""
from __future__ import annotations

from collections import defaultdict

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NAME, DOMAIN
from .entity import EdisioReceiver, model_emitter_mid


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    gw = hass.data[DOMAIN][entry.entry_id]
    by_sub: dict[str | None, list[ButtonEntity]] = defaultdict(list)
    for sub_id, data in EdisioReceiver.receiver_modules(entry):
        by_sub[sub_id].append(EdisioLearnButton(gw, data))
    for sub_id, buttons in by_sub.items():
        async_add_entities(buttons, config_subentry_id=sub_id)


class EdisioLearnButton(EdisioReceiver, ButtonEntity):
    """Bouton d'apprentissage : appaire le module a l'emetteur (virtuel) Edisio."""

    _attr_icon = "mdi:link-variant-plus"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, gw, dev):
        super().__init__(gw, dev)
        base = dev[CONF_NAME].rsplit(" C", 1)[0]
        self._attr_name = f"{base} — Appairer"
        self._attr_unique_id = f"{DOMAIN}_{self._id}_learn"
        self._mid = model_emitter_mid(self._model)

    async def async_press(self) -> None:
        # Module en apprentissage (LED clignotante) -> il memorise cet emetteur.
        # Route selon le dongle : trame Edisio brute ou commande ASSOC RFPlayer.
        await self._gateway.async_learn(self._id, self._mid)
