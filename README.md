# MyHome SCS Bridge

Home-Assistant-App, die eine **BTicino/Legrand-MyHome-SCS-Anlage** über **OpenWebNet** per **MQTT** in Home Assistant einbindet, ohne Cloud und ohne Custom-Component.

[![Home Assistant compatibility](https://github.com/Electix05/myhome-scs-bridge/actions/workflows/ha-compat.yaml/badge.svg?branch=main)](https://github.com/Electix05/myhome-scs-bridge/actions/workflows/ha-compat.yaml)

Aktuelle Version: 1.27.0. Änderungen stehen im [Changelog](myhome_scs_mqtt/CHANGELOG.md).

## Was sie kann

| MyHome | Home Assistant |
|---|---|
| Beleuchtung und Dimmer, DALI mit RGBW und Tunable White (WHO 1) | `light`, `switch` |
| Rollläden mit Position und Lamellen (WHO 2) | `cover` |
| Heizzonen, Fancoils, Pumpen (WHO 4) | `climate`, `sensor`, `binary_sensor` |
| Energiezähler (WHO 18) | `sensor` |
| Kontakte (WHO 25) | `binary_sensor` |
| Lichtmanagement-Melder BMSE | `sensor`, `binary_sensor` |
| Taster CEN / CEN+ | `event` |
| Szenarien | `scene` |

Dazu kommen ein Bus-Scan, eine Weboberfläche im Home-Assistant-Panel und der parallele Betrieb mehrerer Anlagen. Die Buslast ist begrenzt: im Mittel höchstens fünf Frames pro Sekunde, kurze Spitzen bis zehn, und Befehle haben Vorfahrt vor Abfragen.

## Voraussetzungen

- Home Assistant mit Supervisor, in der Regel Home Assistant OS
- MQTT-Broker über die Mosquitto-App
- OpenWebNet-Gateway im Netzwerk, zum Beispiel F461, MH201, F454, F455 oder MyHOMEServer1
- Prozessor `amd64` oder `aarch64`

## Installation

In Home Assistant unter *Einstellungen → Apps → App-Store* → Menü ⋮ → *Repositories* diese Adresse eintragen:

```
https://github.com/Electix05/myhome-scs-bridge
```

Danach erscheint die App im Store. Home Assistant baut sie beim Installieren auf dem Gerät und zeigt neue Versionen als Update an. Der Umstieg von einer lokal installierten Fassung, der lokale Weg, alle Optionen und die Fehlersuche stehen in [`DOCS.md`](myhome_scs_mqtt/DOCS.md), Kapitel 1.

## Entwicklung

Nötig ist Python 3.12 oder neuer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r myhome_scs_mqtt/requirements.txt ruff==0.16.8
.venv/bin/python -m unittest
.venv/bin/ruff check --isolated --target-version py312 --select F,E9,B,PLE --ignore B905 myhome_scs_mqtt tests
```

Die Tests brauchen weder Gateway noch Broker: `tests/fake_gateway.py` und `tests/fake_broker.py` laufen auf `127.0.0.1`.

Bei jedem Push und bei jedem Pull Request baut GitHub das Image für beide Architekturen und führt alle Tests darin aus.

Jede Nacht prüft GitHub außerdem gegen Home Assistant `stable`, `beta` und `dev`: Jede Discovery-Nachricht geht durch den echten Code von Home Assistant, und ein Gesamttest lässt das Add-on mit echtem Home Assistant und Mosquitto laufen (`tests/contract`, `tests/e2e`). Die Ampel für Home Assistant liegt in `testgeraet/`.

Verweise auf `berichte/` und `WISSEN.md` im Code zeigen auf interne Unterlagen, die nicht in diesem Repository liegen.

## Rechtliches

Alle Rechte vorbehalten, eine Lizenzdatei gibt es nicht.

BTicino, Legrand und MyHome sind Marken ihrer Inhaber. Dieses Projekt steht in keiner Verbindung zu ihnen.
