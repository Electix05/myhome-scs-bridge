# Testgerät und Ampel

## Ampel für die Kompatibilität

`scs_bridge_ampel.yaml` zeigt in Home Assistant, ob die nächtliche Prüfung gegen
Home Assistant `stable`, `beta` und `dev` grün ist. Der Sensor
`sensor.scs_bridge_ampel` kennt vier Zustände:

| Zustand | Bedeutung |
|---|---|
| `gruen` | Die letzte Nacht war in Ordnung. |
| `rot` | Etwas passt nicht mehr. Vor dem nächsten Update nachsehen. |
| `veraltet` | Seit drei Tagen kein fertiger Lauf: Die Prüfung läuft nicht mehr. |
| `unbekannt` | Noch keine Antwort von GitHub. |

Bei `rot` und `veraltet` erscheint eine Benachrichtigung in Home Assistant.

**Einrichten:**

1. In der `configuration.yaml` Pakete einschalten, falls noch nicht geschehen:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Die Datei als `packages/scs_bridge_ampel.yaml` in den Konfigurationsordner legen.
3. *Entwicklerwerkzeuge → YAML → Konfiguration prüfen*, dann Home Assistant neu starten.

**Bei `veraltet`:** GitHub schaltet geplante Läufe eines öffentlichen Repos nach
60 Tagen ohne Aktivität ab. Im Repo unter *Actions → Home Assistant compatibility*
auf *Enable workflow* klicken.

## Testgerät

Kommt in einem späteren Schritt dazu.
