# Edisio for Home Assistant

[🇫🇷 Français](https://github.com/Sdavid66/Edisio_to_HACS/blob/main/README.md) · **🇬🇧 English**

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![release](https://img.shields.io/github/v/release/Sdavid66/Edisio_to_HACS)](https://github.com/Sdavid66/Edisio_to_HACS/releases)
[![Buy me a coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support%20the%20project-orange?logo=buy-me-a-coffee&logoColor=white)](https://buymeacoffee.com/sdavid66)

> **Custom** Home Assistant integration for **Edisio** home automation (868 MHz USB
> dongle), ported from the Jeedom plugin. 100% local, no cloud.

> ☕ Do you find this project useful? You can **[buy me a coffee](https://buymeacoffee.com/sdavid66)**
> to support its development — thanks!

> ℹ️ **Not yet in the default HACS store.** Install the integration as a **custom
> repository** (see below). Publishing to the official store will happen later.

## Installation via HACS (recommended)

1. Make sure [HACS](https://hacs.xyz) is installed.
2. In Home Assistant: **HACS → ⋮ menu (top right) → Custom repositories**.
3. Paste the GitHub repository URL `https://github.com/Sdavid66/Edisio_to_HACS`,
   choose the **Integration** category, then **Add**.
4. Open the **Edisio** card that appears → **Download**.
5. **Restart Home Assistant**.
6. **Settings → Devices & services → Add integration → Edisio**, then choose the
   **dongle type** and its **serial port**.

### Manual installation (without HACS)
Copy the `custom_components/edisio` folder into Home Assistant's
`config/custom_components/`, then restart.

A **custom component** bringing the Edisio protocol (868 MHz USB dongle) from the
Jeedom plugin to Home Assistant. 100% local communication (`local_push`), no cloud
dependency.

> Port of the serial protocol from Jeedom's `edisiod.py` daemon. Frame encoding was
> validated bit-for-bit against the original templates (see `tests/test_protocol.py`).

## Hardware
<img align="right" width="190" src="images/edisio-clef-usb-a-edisio-868mhz.jpg" alt="Edisio USB dongle 868 MHz">

- **Edisio USB dongle** (Prolific PL2303 `067B:2303` or FTDI FT232 `0403:6001`), 9600 baud.
- **GCE RFPlayer (RFP1000)** — 433/868 MHz radio gateway, in **test/beta** (see below).
- Edisio modules: switches/remotes (emitters) and receivers (micro-modules, DIN rail,
  EMV-400 shutter…).

## Dongle / gateway: Edisio or GCE RFPlayer
<img align="right" width="150" src="images/rfplayer.jpg" alt="GCE RFPlayer RFP1000 gateway">

When adding the integration (and via **Reconfigure**), choose the **dongle type**:

| Type | Description | Status |
|------|-------------|--------|
| **Edisio dongle** | Transparent USB adapter (PL2303/FT232), 9600 baud, raw Edisio frames. | ✅ Stable |
| **GCE RFPlayer (RFP1000)** | Smart gateway, ZIA API at 115200 baud, Edisio protocol on 868 MHz. | 🧪 **Beta** |

> ⚠️ **The type must match your hardware.** A wrong choice means the gateway will
> not work (different protocol and baud rate).

> 🧪 **RFPlayer support is in beta.** Basic transmit/receive (ON/OFF/TOGGLE, battery,
> temperature) is implemented, but some fine details (channel of multi-channel
> receivers, shutters/dimmers, association) still need **validation on real
> hardware**. If you hit an issue, enable debug logging and open an *issue*:
> ```yaml
> logger:
>   logs:
>     custom_components.edisio: debug
> ```
> (look for `RFPlayer TX` / `RFPlayer RX` lines).

## How it works

### SMILE / Diamond remotes (1 to 5 buttons)
<img align="right" width="230" src="images/diamond.jpg" alt="Edisio Diamond glass switches (multi-colour)">

**Add device → Detect a remote** → choose the type: **SMILE** (1 button) or
**Diamond** (1 to 5 buttons). Name the remote, then learn its buttons **one by one**
(name the button → inclusion turns on → press → learned → "Add another button?").
You get a **single device** grouping **one `event` entity per button** (+ battery).
To add one later: device page → **Reconfigure**.

### Other emitters (sensors, contacts) — automatic discovery
In **inclusion mode**, a received frame shows a discovery card:
- `event.edisio_<id>_telecommande`: button presses (types `on/off/toggle/up/down/stop`)
  → ideal to trigger automations.
- `sensor.edisio_<id>_batterie` and `…_temperature` (MID 08 sensors, e.g. the **ETS-200 temperature sensor**: °C temperature + battery).
- `binary_sensor.edisio_<id>_etat`: last ON/OFF state (contacts, switches).

### Receiver modules (lights, shutters) — added manually
On the integration page (**Settings → Devices & services → Edisio**), click the
**Add device** button (next to *Add hub*, like Z-Wave/Zigbee): choose the **model**
from the catalog, give it a **name** and, optionally, an *Edisio ID* (left blank → a
virtual emitter is generated). All channels of the module are created and attached
to the gateway. The device is then **reconfigurable** (name/ID) and **removable**
individually.

> Receivers added before v1.7.0 (via *Configure*) keep working unchanged.

#### Pairing a receiver — the "Appairer" (Learn) button

**Why.** A receiver (micro-module, DIN rail…) only obeys the emitters it has
**memorised**. The integration generates a **virtual emitter** for each receiver (its
*Edisio ID*), so you must **teach that emitter to the module, once**. That's what the
**"Appairer" (Learn) button** does (category *Configuration*), present on every receiver
device.

**What it does — and what it isn't.** When pressed, it **transmits** (TX) a single Edisio
learning frame (`…09<MID>1F000010…`) from the device's virtual emitter, with the **correct
MID read automatically from the model** (e.g. `01` micro-modules, `05` DIN rail) — no
setup. It's a **one-shot** send (3 repeats): it **listens to nothing**, opens no window,
changes no state in HA.

> 💡 **Depends on the dongle.** With the **Edisio dongle** the button transmits the raw
> frame above; with **RFPlayer** it sends the equivalent association command
> `ZIA++ASSOC … EDISIO` (beta). The procedure on the module side is the same.

> ⚠️ **Not to be confused with inclusion mode.** **Inclusion** = HA *listens* (RX) to
> **discover emitters** (remotes, sensors). The **"Appairer"** button = HA *transmits*
> (TX) so a **receiver** memorises HA. Rule of thumb: **emitter → inclusion, receiver →
> Appairer**.

**How to use it:**
1. Put the **module** into learning mode (see its manual: usually a press on its button →
   blinking LED / beeps).
2. Within its window (~10 s), **click "Appairer"** on the device in HA
   (**Settings → Devices & services → Edisio →** the device).
3. The module confirms (LED/beep). Then test the entity (ON/OFF, up/down…).

**Good practice:** one module at a time, in a quiet moment; **don't operate any remote**
during those few seconds (the module memorises the first *active* emitter it receives).
Passive sensors (e.g. ETS-200) send data, not learning frames → they don't interfere.

> The `edisio.learn` service remains available for advanced cases (`edisio_id`, explicit
> `emitter_mid`).

## Supported receiver models (exact catalog frames)

<p align="center">
  <img width="240" src="images/emv-400.jpg" alt="EMV-400 micro-module">
  &nbsp;
  <img width="240" src="images/emsd-300a.jpg" alt="EMSD-300A micro-module">
  <br>
  <img width="240" src="images/edr-b4.jpg" alt="EDR-B4 DIN rail module">
  &nbsp;
  <img width="240" src="images/edr-d4.jpg" alt="EDR-D4 DIN rail module">
</p>

Each model below is defined with its **original frames** (verified against the Jeedom
plugin). When adding a multi-channel module, **all its channels** are created under
the same paired ID.

| Ref. | Name | HA entity | Channels |
|------|------|-----------|----------|
| 0C | Pilot-wire module | select | 1 |
| 0F | Boiler module | select | 1 |
| 112 | EMV-400 micro-module (roller shutter) | cover | 1 |
| 113 | EMV-400 micro-module (light) | light | 2 |
| 114 | Light module | light | 1 |
| 115 | Roller shutter module | cover | 1 |
| 116 | EMSD-300A micro-module (ON/OFF) | light | 1 |
| 116D | EMSD-300A micro-module (dimmer) | light (dimmer) | 1 |
| EMR2000 | EMR-2000 micro-module (ON/OFF) | switch | 1 |
| 119 | EDR-D4 (ON/OFF/dimming) | light (dimmer) | 4 |
| EDRB4 | EDR-B4 (channel pairs: ON/OFF or cover) | switch **and/or** cover | 4 |

> **EDR-B4 — per channel-pair function.** The 4 outputs are configured **in pairs**:
> **channels 1 & 2** and **channels 3 & 4**. For each pair, choose when adding:
> "2 switches (ON/OFF)" or "1 cover / blind". In cover mode, **a single** `cover`
> entity drives the pair using the shutter's real **up / down / stop** commands.
> Changeable later from the device page → **Reconfigure**. (The old
> all-ON/OFF and all-shutter models remain supported for existing installs.)

> **EMSD-300A — ON/OFF or dimmer.** The mode is set by the module's **DIP switch 2**
> (*Up* = ON/OFF, *Down* = dimmer). Pick the matching variant when adding it. In dimmer
> mode, brightness is controlled from Home Assistant (the module remembers the last
> level; resistive load R only, 25–300 W).

The **SMILE / Diamond remotes** are not receivers: they are learned via **Detect a
remote** (see above). Other emitters (sensors, contacts) are **discovered
automatically** and exposed as `event`/`sensor`/`binary_sensor`.

## Migration from Jeedom (database import)

If you came from the **Edisio plugin for Jeedom**, you can re-import your devices
**without re-pairing anything**, in **two steps**:

**1. Beforehand (on your PC) — produce the import file**

- In Jeedom: **Settings → System → Backups**, generate and download a backup, and
  retrieve the `DB_backup.sql` it contains.
- Run the provided tool to convert it into an import file:
  ```bash
  python3 tools/jeedom_migration/edisio_migrate.py path/to/DB_backup.sql
  # -> produces edisio_import.json
  ```

**2. In Home Assistant — load the import file**

- **Settings → Devices & services → Edisio → Configure → *Import from Jeedom***:
  **upload `edisio_import.json` directly from your computer** (ideal if HA runs on a
  remote machine: Proxmox, NAS…), then confirm the summary.
- *Alternative*: if the file is already on the HA server (e.g. `/config` via the
  *Samba* / *File editor* add-on), give its path instead. The `edisio.import_jeedom`
  service (path-based) is also available.

The import rebuilds **one device per Edisio group actually used**, reusing the
**business name** of your Jeedom commands (`ON_Garage`/`OFF_Garage` → "Garage"), and
pre-registers remotes/sensors as discovered emitters. Existing duplicates are skipped
(safe re-import). Home Assistant never reads the Jeedom database: it only loads
`edisio_import.json`.

> **Shutters / blinds — two possible choices.** By default, groups driven Up/Down are
> imported as **switch** (ON = Up, OFF = Down), frames identical to Jeedom. To expose
> them as **`cover`** entities instead (model *EDR-B4 shutter/blind*, ref. `120C`),
> re-run the tool with `--stores-as-cover`. You can also, at any time, add a shutter
> manually via the *Add device → EDR-B4 (shutter/blind)* button.
>
> Details and file format: [`tools/jeedom_migration/`](tools/jeedom_migration/).

## Inclusion / exclusion mode

By default, **no unknown emitter is added**: frames from unknown devices
(neighbours, unwanted remotes) are ignored. To pair an emitter, you open an
inclusion window — exactly like on Jeedom.

**Inclusion:**
- `switch.edisio_mode_inclusion` switch (*Configuration* category), or
- `edisio.inclusion_mode` service (`enable`, `duration` in seconds).

During the window (120 s by default, auto-closing), press the remote button or let
the sensor emit: a **"Edisio emitter detected"** card appears in **Settings →
Devices & services**. Click **Configure** to link the device; its entities (`event`
button / `sensor` / `binary_sensor`) are then created, attached to the gateway, and
**remembered** (they survive restarts, without re-enabling inclusion). You therefore
see each device before adding it, and neighbours' emitters never clutter your setup.

**Exclusion:**
- Delete the device from the UI (**Device → Delete**), or
- `edisio.exclude` service (`device_id`, and `ban: true` to **permanently ban** an ID
  that can never be included again).

The accepted/banned state is kept in a dedicated *store* (outside configuration), so
discovery never triggers a reload of the integration.

## Services
- `edisio.inclusion_mode`: opens/closes the inclusion window.
- `edisio.exclude`: removes (and optionally bans) a discovered emitter.
- `edisio.learn`: sends a learning frame (`edisio_id`, `emitter_mid`).
- `edisio.send_raw`: sends a raw hex frame (debug).

## Protocol (reverse-engineering summary)
Frame (≥ 16 bytes), 9600 8N1:
```
6C 76 63 │ ID(4) │ BUTTON(1) │ MID(1) │ BATT(1) │ RMAX(1) │ RC(1) │ CMD(1) │ [DATA] │ 64 0D 0A
```
- Header `6C7663`, footer `640D0A`.
- `MID` = module type (`08` = temperature sensor, `1D` = multi-state…).
- `CMD`: `01`=ON, `02`=OFF, `03..08`=toggle, `09`=ON, `1B`=down, `0B`=stop, `F1..FA`=intensity 20..100 %.
- Battery: `pct = round((byte / 3.3) × 10)` (3.3 V ⇒ 100 %).
- Temperature (MID 08): `int(DATA[3:4] + DATA[0:2], 16) / 100`.
- Transmission: full frame written **3 times**, 140 ms apart.

## Limitations
- Receivers do not report their state: the state in HA is **optimistic**.
- The model → type mapping is intentionally generic; adjust the `group`/type when
  adding a module.
- The RFPlayer backend is in **beta** (see above): some fine details still need
  validation on real hardware.

## Support the project ☕
This plugin is free and developed in my spare time. If it helps you, you can
**[buy me a coffee](https://buymeacoffee.com/sdavid66)** — every little bit is much
appreciated and motivates the next improvements. Thanks! 🙏

[![Buy me a coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-support%20the%20project-orange?logo=buy-me-a-coffee&logoColor=white)](https://buymeacoffee.com/sdavid66)

## License
GPL-2.0 (consistent with the original Jeedom plugin).
