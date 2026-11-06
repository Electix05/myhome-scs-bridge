# MyHome SCS Bridge

Bindet eine **BTicino / Legrand MyHome SCS**-Anlage über **OpenWebNet** in Home
Assistant ein – ohne Cloud, ohne Custom-Component. Das Add-on spricht direkt mit
dem Gateway (F461, MH201, F454, F455, MyHOMEServer1 …) und legt die Geräte per
**MQTT Discovery** an.

Neufassung der *MyHome SCS MQTT Bridge*: eigenes Add-on mit eigenem Slug,
eigenen Topics (`scs_bridge/…`) und eigenen `unique_id`s – beide können
nebeneinander laufen.

## Was unterstützt wird

| MyHome | Home Assistant |
|---|---|
| Beleuchtung und Dimmer, DALI (WHO 1) | `light`, `switch` |
| Rollläden mit Position und Lamellen (WHO 2) | `cover` |
| Heizzonen, Fancoils, Pumpen (WHO 4) | `climate`, `sensor`, `binary_sensor` |
| Energiezähler (WHO 18) | `sensor` |
| Kontakte (WHO 25) | `binary_sensor` |
| Lichtmanagement-Melder BMSE | `sensor`, `binary_sensor` |
| Taster CEN / CEN+ | `event` |
| Szenarien | `scene` |

Dazu Diagnose-Entities: Gateway-Verbindung, Bus-Scan, abgelehnte und
unbeantwortete Frames.

## Kurzanleitung

1. Im App-Store unter Menü ⋮ → *Repositories*
   `https://github.com/Electix05/myhome-scs-bridge` eintragen und das Add-on
   installieren. Umstieg von einer lokal installierten Fassung und der lokale Weg:
   Tab **Dokumentation**, Kapitel 1.
2. In der Konfiguration `gateway_host` und `gateway_password` eintragen.
3. Starten – beim ersten Start scannt das Add-on den Bus selbst.

Buslast: höchstens fünf Frames pro Sekunde; bei gehäuften Ablehnungen oder
Stille bremst das Add-on zusätzlich (einstellbar, siehe Dokumentation).
