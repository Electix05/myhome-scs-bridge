"""Which fields the web interface offers per device type.

The page renders its inputs from :data:`SCHEMA`, so a new option only has to be
added here.  Taken over unchanged from the previous add-on (1.17.0).

Home Assistant decides from the device class what an entity is called and
which icon it gets - a door says "geöffnet/geschlossen", a motion detector says
"erkannt/frei".  The identifiers have to be the English ones; the labels exist
so nobody has to guess what they mean.  A test keeps the offered classes equal
to the ones :func:`..devices.base.device_class` accepts.
"""

from __future__ import annotations

from typing import Any

CONTACT_CLASSES: list[dict[str, str]] = [
    {"value": "door", "label": "Tür – offen / geschlossen"},
    {"value": "window", "label": "Fenster – offen / geschlossen"},
    {"value": "garage_door", "label": "Garagentor – offen / geschlossen"},
    {"value": "opening", "label": "Öffnung – offen / geschlossen"},
    {"value": "lock", "label": "Schloss – entriegelt / verriegelt"},
    {"value": "motion", "label": "Bewegung – erkannt / frei"},
    {"value": "occupancy", "label": "Belegung – belegt / frei"},
    {"value": "presence", "label": "Anwesenheit – zu Hause / abwesend"},
    {"value": "smoke", "label": "Rauch – erkannt / frei"},
    {"value": "gas", "label": "Gas – erkannt / frei"},
    {"value": "carbon_monoxide", "label": "Kohlenmonoxid – erkannt / frei"},
    {"value": "heat", "label": "Hitze – heiß / normal"},
    {"value": "cold", "label": "Kälte – kalt / normal"},
    {"value": "moisture", "label": "Feuchtigkeit – nass / trocken"},
    {"value": "sound", "label": "Geräusch – erkannt / frei"},
    {"value": "vibration", "label": "Erschütterung – erkannt / frei"},
    {"value": "tamper", "label": "Manipulation – erkannt / frei"},
    {"value": "safety", "label": "Sicherheit – unsicher / sicher"},
    {"value": "problem", "label": "Problem – erkannt / in Ordnung"},
    {"value": "power", "label": "Strom – erkannt / kein Strom"},
    {"value": "plug", "label": "Stecker – eingesteckt / getrennt"},
    {"value": "light", "label": "Licht – erkannt / dunkel"},
    {"value": "connectivity", "label": "Verbindung – verbunden / getrennt"},
    {"value": "running", "label": "Betrieb – läuft / steht"},
    {"value": "moving", "label": "Bewegung – fährt / steht"},
    {"value": "battery", "label": "Batterie – schwach / normal"},
    {"value": "battery_charging", "label": "Batterie – lädt / lädt nicht"},
    {"value": "update", "label": "Aktualisierung – verfügbar / aktuell"},
]

COVER_CLASSES: list[dict[str, str]] = [
    {"value": "shutter", "label": "Rollladen"},
    {"value": "blind", "label": "Jalousie / Raffstore"},
    {"value": "awning", "label": "Markise"},
    {"value": "curtain", "label": "Vorhang"},
    {"value": "shade", "label": "Sonnenschutz"},
    {"value": "window", "label": "Fenster"},
    {"value": "door", "label": "Tür"},
    {"value": "garage", "label": "Garagentor"},
    {"value": "gate", "label": "Tor"},
    {"value": "damper", "label": "Klappe"},
]

SCHEMA: dict[str, dict[str, Any]] = {
    "lights": {
        "label": "Licht und Dimmer",
        "fields": [
            {"key": "dimmer", "label": "Dimmer", "type": "bool"},
            {"key": "rgbw", "label": "RGBW", "type": "bool"},
            {"key": "tunable_white", "label": "TunableW", "type": "bool"},
            {"key": "min_kelvin", "label": "Warm (K)", "type": "number",
             "placeholder": "2000"},
            {"key": "max_kelvin", "label": "Kalt (K)", "type": "number",
             "placeholder": "6200"},
        ],
        "note": ("Dimmer bedeutet Prozentschritte. Eine Leuchte, die von sich "
                 "aus eine Helligkeit, eine Farbe oder eine Weißtemperatur "
                 "meldet, bekommt das Häkchen selbst. RGBW und TunableW gehen "
                 "auch zusammen; die Helligkeit läuft immer getrennt über den "
                 "Dimmer. Warm und Kalt gelten nur für TunableW – leer heißt "
                 "2000 K bis 6200 K."),
    },
    "switches": {
        "label": "Schaltaktoren",
        "fields": [
            {"key": "who", "label": "WHO", "type": "number", "placeholder": "1"},
            {"key": "device_class", "label": "Geräteklasse", "type": "select",
             "options": [{"value": "outlet", "label": "Steckdose"},
                         {"value": "switch", "label": "Schalter"}]},
        ],
    },
    "covers": {
        "label": "Rollläden und Raffstores",
        "fields": [
            {"key": "device_class", "label": "Klasse", "type": "select",
             "options": COVER_CLASSES},
            {"key": "travel_time", "label": "Fahrzeit (s)", "type": "number"},
            {"key": "tilt_time", "label": "Lamellen (s)", "type": "number"},
            {"key": "invert_position", "label": "Position umkehren", "type": "bool"},
        ],
        "note": ("Eine Zwischenstellung wird im Regelkreis angefahren: starten, "
                 "Position abfragen, am Ziel stoppen. Aktoren, die einen "
                 "Positionsbefehl selbst annehmen, brauchen in der Gerätedatei "
                 "advanced: true – die hier verbauten können es nicht."),
    },
    "climate": {
        "label": "Heizzonen",
        "note": ("Heizen und Kühlen bestimmen, welche Betriebsarten Home Assistant "
                 "anbietet. Lüfter leer lassen heißt: selbst erkennen, ob die Zone "
                 "einen hat."),
        "fields": [
            {"key": "heizen", "label": "Heizen", "type": "bool"},
            {"key": "kuehlen", "label": "Kühlen", "type": "bool"},
            {"key": "luefter", "label": "Lüfter", "type": "bool"},
            {"key": "min_temp", "label": "min °C", "type": "number"},
            {"key": "max_temp", "label": "max °C", "type": "number"},
        ],
    },
    "energy": {
        "label": "Energiezähler",
        "fields": [
            {"key": "energy_total", "label": "Zählerstand", "type": "bool"},
            {"key": "energy_today", "label": "Tageswert", "type": "bool"},
        ],
    },
    "motion_sensors": {
        "label": "Melder (BMSE)",
        "fields": [
            {"key": "illuminance", "label": "Helligkeit", "type": "bool"},
            {"key": "motion", "label": "Bewegung", "type": "bool"},
            {"key": "motion_poll", "label": "Abfrage (s)", "type": "number"},
            {"key": "off_delay", "label": "Haltezeit (s)", "type": "number"},
            {"key": "lux_interval", "label": "Lux alle (s)", "type": "number"},
        ],
    },
    "pumps": {
        "label": "Pumpen und Heizungsaktoren",
        "note": ("Adresse = Nummer des Aktors. Die Umwälzpumpe hängt meist an der "
                 "Zentrale, dann Zone 0 lassen – daraus wird 0#3. Gehört der Aktor "
                 "zu einer Zone, deren Nummer eintragen."),
        "fields": [
            {"key": "zone", "label": "Zone", "type": "text", "placeholder": "0"},
            {"key": "device_class", "label": "Geräteklasse", "type": "select",
             "options": [{"value": "running", "label": "Betrieb – läuft / steht"},
                         {"value": "power", "label": "Strom – erkannt / kein Strom"},
                         {"value": "heat", "label": "Hitze – heiß / normal"},
                         {"value": "opening", "label": "Ventil – offen / geschlossen"}]},
        ],
    },
    "binary_sensors": {
        "label": "Kontakte",
        "note": ("Adresse = Nummer des Kontakts; auf dem Bus wird daraus 3 + Nummer "
                 "(aus 99 wird 399). Ohne Häkchen bleibt der Kontakt außen vor, "
                 "beide zugleich geht nicht."),
        "fields": [
            {"key": "schliesser", "label": "Schließer", "type": "bool"},
            {"key": "oeffner", "label": "Öffner", "type": "bool"},
            {"key": "device_class", "label": "Geräteklasse", "type": "select",
             "options": CONTACT_CLASSES, "wide": True},
        ],
    },
    "buttons": {
        "label": "Taster (CEN / CEN+)",
        "fields": [
            {"key": "kind", "label": "Art", "type": "select",
             "options": ["cen_plus", "cen"]},
            {"key": "buttons", "label": "Tasten", "type": "list",
             "placeholder": "1, 2, 3"},
        ],
    },
    "scenes": {
        "label": "Szenarien",
        "fields": [
            {"key": "scenarios", "label": "Szenarien", "type": "list",
             "placeholder": "1, 2, 3, 4"},
        ],
    },
}
