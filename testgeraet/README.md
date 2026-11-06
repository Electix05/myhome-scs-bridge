# Testgerät und Ampel

## Ampel für die Kompatibilität

`scs_bridge_ampel.yaml` zeigt in Home Assistant, ob die nächtliche Prüfung gegen
Home Assistant `stable`, `beta` und `dev` grün ist. Der Sensor
`sensor.scs_bridge_ampel` kennt diese Zustände:

| Zustand | Bedeutung |
|---|---|
| `gruen` | Der letzte fertige Lauf war in Ordnung. |
| `rot` | Etwas passt nicht mehr, oder der Lauf wurde abgebrochen. Vor dem nächsten Update nachsehen. |
| `veraltet` | Seit drei Tagen kein fertiger Lauf: Die Prüfung läuft nicht mehr. |
| `unbekannt` | GitHub hat geantwortet, aber ohne fertigen Lauf, z. B. wegen des Abruflimits oder weil das Repo nicht gefunden wird. |
| `unavailable` | GitHub ist nicht erreichbar. |

Bei `rot` und `veraltet` erscheint sofort eine Benachrichtigung in Home Assistant,
bei `unbekannt` und `unavailable` erst nach zwölf Stunden am Stück: Ein nicht
lesbares Ergebnis ist unbekannt, nicht grün. Ist die Ampel wieder `gruen`,
verschwindet die Benachrichtigung von selbst.

Wird die Datei hier geändert, die neue Fassung genauso in den `packages`-Ordner
legen und Home Assistant neu starten.

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

**Probealarm:** Unter *Actions → Home Assistant compatibility → Run workflow* mit
dem Haken *probealarm* auf `main` gestartet, schreibt der Lauf absichtlich einen
Discovery-Schlüssel falsch und muss rot werden. So lässt sich die ganze Kette
prüfen: rotes Ergebnis, Mail von GitHub, Ampel auf `rot`, Benachrichtigung. Ein
normaler Lauf danach stellt die Ampel wieder auf `gruen`.

## Testgerät

Kommt in einem späteren Schritt dazu.
