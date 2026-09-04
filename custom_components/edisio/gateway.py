"""Passerelle serie Edisio : lecture/ecriture + modes inclusion/exclusion."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Callable

import serial_asyncio_fast as serial_asyncio

from homeassistant.config_entries import ConfigEntry, SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import protocol, rfplayer
from .const import (
    CONF_BANNED, CONF_DISCOVERED, CONF_DONGLE, DOMAIN, DONGLE_EDISIO,
    DONGLE_RFPLAYER, EVENT_TYPES, INCLUSION_TIMEOUT, KNOWN_USB_IDS,
    RFPLAYER_BAUDRATE, SERIAL_BAUDRATE, SIGNAL_DISCOVERY, SIGNAL_INCLUSION,
    SIGNAL_REMOVED, SIGNAL_RX, SIGNAL_STATUS, TX_DELAY, TX_REPEAT,
)

_LOGGER = logging.getLogger(__name__)

_HEADER = bytes.fromhex(protocol.HEADER)
_FOOTER = bytes.fromhex(protocol.FOOTER)

# Reconnexion serie : backoff exponentiel borne (secondes).
_RECONNECT_MIN = 5
_RECONNECT_MAX = 60

# Repertoire des liens stables par identifiant materiel (Linux).
_SERIAL_BY_ID = "/dev/serial/by-id"


def _serial_by_id(device: str) -> str:
    """Chemin stable ``/dev/serial/by-id/...`` correspondant a ``device``.

    Resout un ``/dev/ttyUSBn`` (nom volatil qui change a la re-enumeration USB)
    vers son lien by-id stable, pour que la reconnexion survive au changement de
    numero sans redemarrage. Retourne l'entree inchangee si c'est deja un by-id,
    s'il est introuvable, ou hors Linux. Appel bloquant : lancer dans un executor.
    """
    if device.startswith(_SERIAL_BY_ID):
        return device
    try:
        target = os.path.realpath(device)
        for name in os.listdir(_SERIAL_BY_ID):
            link = os.path.join(_SERIAL_BY_ID, name)
            if os.path.realpath(link) == target:
                return link
    except OSError:
        pass
    return device


def _dongle_info(port: str) -> tuple[str | None, str | None] | None:
    """(bloquant) ``(description, 'VID:PID')`` du port, ou None si introuvable.

    Compare par ``realpath`` pour fonctionner que ``port`` soit un ``ttyUSBn`` ou
    un chemin ``by-id``.
    """
    from serial.tools import list_ports
    try:
        target = os.path.realpath(port)
        ports = list_ports.comports()
    except OSError:
        return None
    for p in ports:
        try:
            if os.path.realpath(p.device) != target:
                continue
        except OSError:
            continue
        desc = p.description if (p.description and p.description != "n/a") else None
        vidpid = f"{p.vid:04X}:{p.pid:04X}" if (p.vid and p.pid) else None
        return (desc, vidpid)
    return None


def classify(decoded: dict) -> set[str]:
    """Determine les capacites (kinds) d'un emetteur a partir d'une trame."""
    kinds: set[str] = set()
    if decoded.get("battery") is not None:
        kinds.add("battery")
    if "temperature" in decoded:
        kinds.add("temperature")
    val = decoded.get("value")
    if val in ("on", "off"):
        kinds.add("binary")
    if isinstance(val, str) and val in EVENT_TYPES:
        kinds.add("event")
    return kinds


class _EdisioProtocol(asyncio.Protocol):
    """Bufferise le flux serie et extrait les trames completes."""

    def __init__(self, on_frame, on_lost):
        self._on_frame = on_frame
        self._on_lost = on_lost
        self._buf = bytearray()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.debug("Connexion serie Edisio etablie")

    def data_received(self, data: bytes) -> None:
        self._buf.extend(data)
        while True:
            start = self._buf.find(_HEADER)
            if start == -1:
                if len(self._buf) > 2:
                    del self._buf[:-2]
                return
            if start > 0:
                del self._buf[:start]
            end = self._buf.find(_FOOTER, len(_HEADER))
            if end == -1:
                return
            frame = bytes(self._buf[: end + len(_FOOTER)])
            del self._buf[: end + len(_FOOTER)]
            self._on_frame(frame)

    def connection_lost(self, exc):
        _LOGGER.warning("Connexion serie Edisio perdue : %s", exc)
        self.transport = None
        self._on_lost()


class _RFPlayerProtocol(asyncio.Protocol):
    """Bufferise le flux RFPlayer et le decoupe en lignes (packets ZIA)."""

    def __init__(self, on_line, on_lost):
        self._on_line = on_line
        self._on_lost = on_lost
        self._buf = ""
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.debug("Connexion serie RFPlayer etablie")

    def data_received(self, data: bytes) -> None:
        self._buf += data.decode(errors="replace")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip("\0 \t\r")
            if line:
                self._on_line(line)
        # Garde-fou : evite une croissance illimitee du buffer sur lien degrade.
        if len(self._buf) > 8192:
            self._buf = self._buf[-256:]

    def connection_lost(self, exc):
        _LOGGER.warning("Connexion serie RFPlayer perdue : %s", exc)
        self.transport = None
        self._on_lost()


class EdisioGateway:
    """Liaison serie + dispatch des trames + gestion inclusion/exclusion."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.port = entry.data["port"]
        self.dongle = entry.data.get(CONF_DONGLE, DONGLE_EDISIO)
        # etat accumule (hors config) persiste dans un Store dedie
        self._store: Store = Store(hass, 1, f"edisio_{entry.entry_id}")
        self.accepted: dict[str, set[str]] = {}   # {id: set(kinds)}
        self.names: dict[str, str] = {}           # {id: nom choisi a la decouverte}
        self.remote_ids: set[str] = set()         # id des telecommandes (sous-entrees)
        self.banned: set[str] = set()
        # derniere temperature/batterie recue, pour amorcer les capteurs a l'ajout
        self.last_values: dict[str, dict] = {}
        self.inclusion = False
        self._capturing = False                     # assistant « Ajouter un appareil »
        self._pending_emitter: dict | None = None
        self._inclusion_cancel: Callable | None = None
        self._transport = None
        self._protocol = None
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._reconnect_task = None
        self._reconnect_delay = _RECONNECT_MIN
        # etat expose aux entites de diagnostic du hub
        self.connected = False
        self.frames_received = 0
        self.last_frame_at = None
        self.dongle_description: str | None = None
        self.dongle_vidpid: str | None = None
        # id de registre de l'appareil hub (pour rattacher les autres via
        # via_device_id ; renseigne a l'enregistrement du hub, cf. __init__.py)
        self.hub_device_id: str | None = None

    @property
    def paired_count(self) -> int:
        """Nombre d'emetteurs appaires (acceptes)."""
        return len(self.accepted)

    @callback
    def _notify_status(self) -> None:
        """Previent les entites de diagnostic d'un changement d'etat."""
        async_dispatcher_send(self.hass, SIGNAL_STATUS)

    # ------------------------------------------------------------------ vie
    async def async_start(self) -> None:
        self._closing = False
        # Le mode inclusion n'est jamais persiste : il redemarre TOUJOURS sur OFF
        # (au demarrage de HA comme au rechargement de l'integration).
        self.inclusion = False
        self._capturing = False
        await self._async_load()
        await self._resolve_dongle()
        await self._connect()

    @callback
    def async_announce_known(self) -> None:
        """Re-cree les entites des emetteurs deja connus (apres redemarrage).

        A appeler APRES la mise en place des plateformes : sinon les listeners
        SIGNAL_DISCOVERY ne sont pas encore branches et les entites decouvertes
        (capteurs temperature/batterie, binaires, evenements) ne sont pas
        recreees et restent « Inconnu ».
        """
        for dev_id, kinds in self.accepted.items():
            data = {"id": dev_id, "kinds": set(kinds), "name": self.names.get(dev_id)}
            data.update(self.last_values.get(dev_id, {}))  # amorce si valeur connue
            async_dispatcher_send(self.hass, SIGNAL_DISCOVERY, data)

    async def _resolve_dongle(self) -> None:
        """Identifie le dongle (description USB, VID:PID) pour l'appareil hub."""
        info = await self.hass.async_add_executor_job(_dongle_info, self.port)
        if info is None:
            return
        self.dongle_description, self.dongle_vidpid = info

    async def _connect(self) -> None:
        # Verrouille sur le chemin stable by-id des qu'on peut le resoudre : la
        # reconnexion survit alors a la re-enumeration USB (ttyUSB0 -> ttyUSB1)
        # sans redemarrage de HA. Reste sur le nom courant tant qu'il est absent.
        resolved = await self.hass.async_add_executor_job(_serial_by_id, self.port)
        if resolved != self.port:
            _LOGGER.info("Port Edisio : %s -> chemin stable %s", self.port, resolved)
            self.port = resolved
        rfplayer_mode = self.dongle == DONGLE_RFPLAYER
        baudrate = RFPLAYER_BAUDRATE if rfplayer_mode else SERIAL_BAUDRATE
        if rfplayer_mode:
            def factory():
                return _RFPlayerProtocol(self._handle_rfplayer_line, self._handle_lost)
        else:
            def factory():
                return _EdisioProtocol(self._handle_frame, self._handle_lost)
        try:
            self._transport, self._protocol = await serial_asyncio.create_serial_connection(
                self.hass.loop, factory, self.port, baudrate=baudrate,
            )
            _LOGGER.info("Passerelle Edisio demarree sur %s (%s, %d bauds)",
                         self.port, self.dongle, baudrate)
            self.connected = True
            self._reconnect_delay = _RECONNECT_MIN  # succes -> reinitialise le backoff
            self._notify_status()
            if rfplayer_mode:
                for command in rfplayer.INIT_COMMANDS:
                    self._write_line(command)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Ouverture du port %s impossible : %s", self.port, err)
            self.connected = False
            self._notify_status()
            self._schedule_reconnect()

    def _write_line(self, command: str) -> None:
        """Envoie une commande ZIA au RFPlayer (prefixe « ZIA++ », fin « \\n\\r »)."""
        if self._transport is None:
            _LOGGER.warning("RFPlayer : envoi impossible (port ferme) : %s", command)
            return
        self._transport.write(f"ZIA++{command}\n\r".encode())
        _LOGGER.debug("RFPlayer TX : ZIA++%s", command)

    @callback
    def _handle_rfplayer_line(self, line: str) -> None:
        """Traite une ligne recue du RFPlayer (packet ZIA)."""
        header, body = line[:5], line[5:]
        if header != "ZIA33":
            _LOGGER.debug("RFPlayer RX (ignore) : %s", line)
            return
        try:
            data = json.loads(body)
            decoded = rfplayer.parse_event(data)
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as err:
            _LOGGER.debug("RFPlayer : trame ignoree (%s) : %s", err, body)
            return
        if decoded is None:
            return
        self._mark_frame()
        self._dispatch(decoded)

    @callback
    def _handle_lost(self):
        self._transport = None
        self.connected = False
        self._notify_status()
        if not self._closing:
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        if self._closing or (self._reconnect_task and not self._reconnect_task.done()):
            return
        delay = self._reconnect_delay
        # Backoff exponentiel borne : evite de marteler un port absent (et de
        # boucler serre sur un dongle qui se reconnecte puis retombe aussitot).
        self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_MAX)

        async def _retry():
            await asyncio.sleep(delay)
            if not self._closing:
                await self._connect()

        self._reconnect_task = self.hass.async_create_task(_retry())

    async def async_stop(self):
        self._closing = True
        if self._inclusion_cancel:
            self._inclusion_cancel()
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._transport:
            self._transport.close()
            self._transport = None
        self.connected = False
        self._notify_status()
        _LOGGER.info("Passerelle Edisio arretee")

    # -------------------------------------------------------------- reception
    @callback
    def _mark_frame(self) -> None:
        self.frames_received += 1
        self.last_frame_at = dt_util.utcnow()
        self._notify_status()

    @callback
    def _handle_frame(self, frame: bytes) -> None:
        """Chemin dongle Edisio : decode la trame brute puis dispatch."""
        decoded = protocol.decode(frame)
        if decoded is None:
            return
        _LOGGER.debug("Edisio RX : %s (id=%s bouton=%s cmd=%s val=%s)",
                      decoded.get("raw"), decoded.get("id"),
                      decoded.get("button"), decoded.get("cmd"),
                      decoded.get("value"))
        self._mark_frame()
        self._dispatch(decoded)

    @callback
    def _dispatch(self, decoded: dict) -> None:
        """Logique commune (Edisio brut ET RFPlayer) : banni/capture/connu/RX."""
        dev_id = decoded["id"]
        if dev_id in self.banned:
            _LOGGER.debug("Trame ignoree (banni) : %s", dev_id)
            return

        kinds = classify(decoded)

        # Memorise la derniere temperature/batterie : sert a amorcer les capteurs
        # des l'ajout (sinon ils restent « Inconnu » jusqu'a la prochaine emission).
        seed = {k: decoded[k] for k in ("temperature", "battery")
                if decoded.get(k) is not None}
        if seed:
            self.last_values[dev_id] = seed

        # Assistant « Ajouter un appareil » : on capture le premier appui recu,
        # que l'emetteur soit deja connu ou non (pas de carte pendant la capture).
        if self._capturing:
            self._pending_emitter = {
                "id": dev_id, "kinds": sorted(kinds),
                "button": decoded.get("button"),
            }
            _LOGGER.info("Capture : emetteur %s bouton %s %s",
                         dev_id, decoded.get("button"), sorted(kinds))
            return

        # Une telecommande (sous-entree) est connue meme si absente du store.
        known = dev_id in self.accepted or dev_id in self.remote_ids
        if not known:
            if not self.inclusion:
                _LOGGER.debug("Emetteur %s ignore (hors mode inclusion)", dev_id)
                return
            # Mode inclusion : proposer l'emetteur via une carte de decouverte
            # (Appareils et services) plutot qu'un ajout silencieux.
            _LOGGER.info("Mode inclusion : emetteur %s detecte %s", dev_id, kinds)
            self._async_discover_emitter(dev_id, kinds)
            return
        if dev_id in self.accepted:
            # enrichit les capacites si une nouvelle apparait (emetteurs du store)
            new_kinds = kinds - self.accepted[dev_id]
            if new_kinds:
                self.accepted[dev_id] |= new_kinds
                self._persist()
                decoded["kinds"] = set(self.accepted[dev_id])
                async_dispatcher_send(self.hass, SIGNAL_DISCOVERY, decoded)

        async_dispatcher_send(self.hass, f"{SIGNAL_RX}_{dev_id}", decoded)
        async_dispatcher_send(self.hass, SIGNAL_RX, decoded)

    # -------------------------------------------------------------- decouverte
    @callback
    def _async_discover_emitter(self, dev_id: str, kinds: set[str]) -> None:
        """Ouvre une carte de decouverte (Appareils et services) pour un emetteur.

        Deduplique : n'ouvre pas une 2e carte si un flux est deja en cours pour
        ce meme identifiant.
        """
        for flow in self.hass.config_entries.flow.async_progress():
            ctx = flow.get("context", {})
            if flow.get("handler") == DOMAIN and ctx.get("edisio_id") == dev_id:
                return
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY, "edisio_id": dev_id},
                data={"id": dev_id, "kinds": sorted(kinds)},
            )
        )

    async def async_accept_emitter(
        self, dev_id: str, kinds, name: str | None = None
    ) -> None:
        """Ajoute un emetteur decouvert (validation de la carte) et cree ses entites."""
        dev_id = dev_id.upper()
        self.banned.discard(dev_id)
        self.accepted[dev_id] = set(kinds)
        if name:
            self.names[dev_id] = name
        else:
            self.names.pop(dev_id, None)
        await self._store.async_save(self._data_to_save())
        payload = {"id": dev_id, "kinds": set(kinds), "name": self.names.get(dev_id)}
        payload.update(self.last_values.get(dev_id, {}))  # amorce temperature/batterie
        async_dispatcher_send(self.hass, SIGNAL_DISCOVERY, payload)
        _LOGGER.info("Emetteur %s ajoute via la decouverte (nom=%s)", dev_id, name)

    # --------------------------------------------------- capture (assistant)
    @callback
    def async_begin_capture(self, duration: int = INCLUSION_TIMEOUT) -> None:
        """Active l'inclusion et capture le prochain emetteur detecte.

        Utilise par l'assistant « Ajouter un appareil » : pendant la capture,
        un emetteur inconnu est bufferise (au lieu d'ouvrir une carte).
        """
        self._pending_emitter = None
        self._capturing = True
        self.async_set_inclusion(True, duration)

    @callback
    def async_end_capture(self) -> None:
        """Termine la capture et coupe l'inclusion."""
        self._capturing = False
        self._pending_emitter = None
        self.async_set_inclusion(False)

    @callback
    def take_pending_emitter(self) -> dict | None:
        """Renvoie (et consomme) l'emetteur capture, ou None si aucun appui."""
        pending = self._pending_emitter
        self._pending_emitter = None
        return pending

    @callback
    def async_set_known_remotes(self, ids: set[str]) -> None:
        """Declare les id des telecommandes (sous-entrees) : trames routees, pas de carte."""
        self.remote_ids = {i.upper() for i in ids}

    # -------------------------------------------------------------- inclusion
    @callback
    def async_set_inclusion(self, enabled: bool, duration: int = INCLUSION_TIMEOUT):
        """Active/desactive le mode inclusion.

        Securite : quand on l'active, un arret automatique est TOUJOURS
        programme, et la fenetre est bornee a INCLUSION_TIMEOUT au maximum.
        Une duree nulle/negative/absente ou superieure a la borne est ramenee a
        INCLUSION_TIMEOUT : le mode ecoute ne peut donc jamais rester bloque en
        permanence (meme via le service avec duration: 0).
        """
        if self._inclusion_cancel:
            self._inclusion_cancel()
            self._inclusion_cancel = None
        if not enabled:
            self._capturing = False
        self.inclusion = enabled
        if enabled:
            if not duration or duration <= 0 or duration > INCLUSION_TIMEOUT:
                duration = INCLUSION_TIMEOUT
            _LOGGER.info("Mode inclusion : ON (arret auto dans %d s)", duration)
            self._inclusion_cancel = async_call_later(
                self.hass, duration, self._auto_off
            )
        else:
            _LOGGER.info("Mode inclusion : OFF")
        async_dispatcher_send(self.hass, SIGNAL_INCLUSION, enabled)

    @callback
    def _auto_off(self, _now):
        self._inclusion_cancel = None
        self.inclusion = False
        self._capturing = False
        _LOGGER.info("Mode inclusion : OFF (fin de fenetre)")
        async_dispatcher_send(self.hass, SIGNAL_INCLUSION, False)

    # -------------------------------------------------------------- exclusion
    async def async_forget(self, dev_id: str, ban: bool = False) -> None:
        """Exclut un emetteur : retire entites/appareil et oublie l'id."""
        dev_id = dev_id.upper()
        self.accepted.pop(dev_id, None)
        self.names.pop(dev_id, None)
        if ban:
            self.banned.add(dev_id)
        self._persist()
        await self._remove_from_registries(dev_id)
        # Purge les caches "seen" des plateformes -> permet un re-ajout ulterieur.
        async_dispatcher_send(self.hass, SIGNAL_REMOVED, dev_id)
        _LOGGER.info("Emetteur %s exclu%s", dev_id, " et banni" if ban else "")

    async def _remove_from_registries(self, dev_id: str) -> None:
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        ent_reg = er.async_get(self.hass)
        prefix = f"edisio_{dev_id}_"
        for ent in list(ent_reg.entities.values()):
            if ent.platform == "edisio" and ent.unique_id.startswith(prefix):
                ent_reg.async_remove(ent.entity_id)
        dev_reg = dr.async_get(self.hass)
        for ident in (("edisio", f"emitter_{dev_id}"), ("edisio", dev_id)):
            device = dev_reg.async_get_device(identifiers={ident})
            if device:
                dev_reg.async_remove_device(device.id)

    # ---------------------------------------------------------------- import
    async def async_import_emitters(self, emitters: list[dict]) -> int:
        """Pre-enregistre des emetteurs (import Jeedom) et sauvegarde aussitot.

        Les entites seront creees au rechargement de l'entree (declenche par la
        mise a jour des options) via la re-emission de SIGNAL_DISCOVERY.
        """
        added = 0
        for e in emitters:
            dev_id = str(e.get("id", "")).upper()
            if not dev_id or dev_id in self.banned:
                continue
            kinds = set(e.get("kinds", []))
            if dev_id not in self.accepted:
                self.accepted[dev_id] = kinds
                added += 1
            else:
                self.accepted[dev_id] |= kinds
        if added:
            await self._store.async_save(self._data_to_save())
        return added

    # ---------------------------------------------------------------- emission
    async def async_send_action(self, edisio_id: str, group: int, action: str,
                                template: str | None, level: int | None = None) -> None:
        """Emet une action : trame brute (Edisio) ou commande ZIA (RFPlayer)."""
        if self.dongle == DONGLE_RFPLAYER:
            command = rfplayer.build_command(action, edisio_id, group, level)
            if command is None:
                _LOGGER.warning("Action %s non traduisible en commande RFPlayer", action)
                return
            async with self._write_lock:
                self._write_line(command)
            return
        if not template:
            return
        await self.async_send(protocol.render(template, edisio_id, group, level))

    async def async_learn(self, edisio_id: str, mid: str = "04",
                          group: int = 1) -> None:
        """Appaire un recepteur : trame d'apprentissage (Edisio) ou ASSOC (RFPlayer)."""
        if self.dongle == DONGLE_RFPLAYER:
            command = rfplayer.build_assoc_command(edisio_id, group)
            if command is None:
                _LOGGER.warning("Appairage %s non traduisible en commande RFPlayer",
                                edisio_id)
                return
            async with self._write_lock:
                self._write_line(command)
            return
        await self.async_send(protocol.cmd_learn(edisio_id, mid))

    async def async_send(self, frames: list[str]) -> None:
        """Emission : trames Edisio brutes (dongle transparent) ou lignes ZIA (RFPlayer).

        En mode RFPlayer, ``send_raw`` sert de passe-plat ZIA : chaque « trame »
        est une commande ZIA (sans le prefixe « ZIA++ » qu'ajoute ``_write_line``),
        ce qui permet de mettre au point la syntaxe directement sur materiel reel
        via les outils de developpement, sans nouvelle publication.
        """
        if self.dongle == DONGLE_RFPLAYER:
            async with self._write_lock:
                for line in frames:
                    self._write_line(line.strip())
            return
        if self._transport is None:
            _LOGGER.warning("Envoi impossible : port serie ferme")
            return
        async with self._write_lock:
            for frame in frames:
                if not protocol.is_valid(bytes.fromhex(frame)):
                    _LOGGER.error("Trame a emettre invalide : %s", frame)
                    continue
                _LOGGER.debug("Edisio TX : %s (x%d)", frame, TX_REPEAT)
                payload = bytes.fromhex(frame)
                for i in range(TX_REPEAT):
                    self._transport.write(payload)
                    # 0,14 s entre les 3 répétitions, 0,02 s en fin de trame :
                    # enchaîne vite la 2e trame d'une commande « && » (cf. edisiod.py).
                    await asyncio.sleep(TX_DELAY if i < TX_REPEAT - 1 else 0.02)

    # ---------------------------------------------------------------- persist
    async def _async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.accepted = {}
        self.names = {}
        for d in data.get(CONF_DISCOVERED, []):
            self.accepted[d["id"]] = set(d.get("kinds", []))
            if d.get("name"):
                self.names[d["id"]] = d["name"]
        self.banned = set(data.get(CONF_BANNED, []))

    @callback
    def _data_to_save(self) -> dict:
        return {
            CONF_DISCOVERED: [
                {"id": i, "kinds": sorted(k), "name": self.names.get(i)}
                for i, k in self.accepted.items()
            ],
            CONF_BANNED: sorted(self.banned),
        }

    @callback
    def _persist(self) -> None:
        # sauvegarde debattue (1 s) : ne declenche PAS de rechargement de l'entree
        self._store.async_delay_save(self._data_to_save, 1)
