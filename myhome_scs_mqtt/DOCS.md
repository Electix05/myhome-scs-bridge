# MyHome SCS Bridge – Dokumentation

Das Add-on verbindet sich mit dem OpenWebNet-Gateway einer BTicino/Legrand-
MyHome-Anlage und spiegelt den SCS-Bus auf MQTT. Home Assistant legt die Geräte
über MQTT Discovery selbst an.

```
SCS-Bus ──► Gateway (F461, MH201, F454, F455, MyHOMEServer1 …)
                │  TCP 20000, OpenWebNet, zwei Sitzungen
                ▼
        MyHome SCS Bridge  ──►  MQTT-Broker  ──►  Home Assistant
```

Die **MyHome SCS Bridge** ist die Neufassung der *MyHome SCS MQTT Bridge*
(1.17.0). Sie ist ein **eigenes Add-on** und kann neben der alten laufen:

| | alte MyHome SCS MQTT Bridge | neue MyHome SCS Bridge |
|---|---|---|
| Slug | `myhome_scs_mqtt` | `myhome_scs_bridge` |
| Basis-Topic | `myhome` | `scs_bridge` |
| `unique_id` | `myhome_scs_bridge_…` | `scs_bridge_…` |
| Discovery | `homeassistant/<Typ>/<id>/config` | `homeassistant/<Typ>/scs_bridge/<id>/config` |
| Gerätedatei auf dem Host | `/addon_configs/local_myhome_scs_mqtt/` | `/addon_configs/local_myhome_scs_bridge/`, aus dem Repository `/addon_configs/474ea02f_myhome_scs_bridge/` |

Keines der beiden Add-ons liest, ändert oder löscht etwas vom anderen.

---

## 1. Installation

Voraussetzungen: Home Assistant mit Supervisor und das Add-on **Mosquitto broker**
mit der **MQTT**-Integration. Für den Umstieg (1.2) und den lokalen Weg (1.3–1.5)
zusätzlich Zugriff auf die Ordner `/addons`, `/addon_configs` und `/share` – über
das Add-on **Samba share** oder ein SSH-Add-on.

### 1.1 Über das Repository (empfohlen)

1. *Einstellungen → Apps → App-Store* (ältere Versionen: *Add-ons →
   Add-on-Store*) → Menü ⋮ → **Repositories**.
2. `https://github.com/Electix05/myhome-scs-bridge` eintragen – genau so, ohne `/`
   am Ende – und **Hinzufügen**.
3. Im Store erscheint der Bereich **MyHome SCS Bridge** mit dem Add-on. Falls
   nicht: Menü ⋮ → **Nach Updates suchen**.
4. **Installieren.** Das Add-on wird auf dem Gerät gebaut, wie beim lokalen Weg
   (1.5, Schritt 3).
5. Konfigurieren und starten wie in 1.5, Schritte 4 und 5.

Neue Versionen zeigt Home Assistant danach selbst als Update an. Für Home
Assistant ist das ein **anderes Add-on** als das lokal installierte: Es heißt intern
`474ea02f_myhome_scs_bridge`, seine Gerätedatei liegt in
`/addon_configs/474ea02f_myhome_scs_bridge/`. Den Namen zeigt die Info-Seite des
Add-ons als Hostname (`474ea02f-myhome-scs-bridge`). Läuft schon die lokale Fassung,
erst umsteigen wie in 1.2. Eine zweite Instanz (Kapitel 4) geht vorerst nur lokal.

### 1.2 Umstieg vom lokalen Add-on

Entities und Verlauf bleiben erhalten: Sie hängen an den `unique_id`s und damit an
der Option `instance_id`, nicht am Add-on. Nicht von selbst mit kommen die Optionen,
die Gerätedatei und was das alte Add-on am Bus gefunden, aber noch nicht in die
Gerätedatei geschrieben hat.

1. **In der Weboberfläche des alten Add-ons auf „Speichern & neu starten“
   klicken.** Danach steht alles, was es kennt – auch die am Bus gefundenen
   Geräte –, in seiner `devices.yaml`. **Nicht überspringen:** Das neue Add-on
   räumt beim Start alle Discovery-Einträge weg, die es nicht kennt. Home
   Assistant entfernt die betroffenen Entities dann samt Raum und eigenem Namen.
   Ab 1.24.0 gibt es dafür ein Netz: Mehr als fünf Geräte auf einmal entfernt das
   Add-on erst nach einer Bestätigung in der Weboberfläche (Kapitel 7.2). Steht
   dort nach dem Umstieg „… Geräte stehen nicht mehr in der Geräteliste“, **nicht
   bestätigen** – dann fehlt die Gerätedatei aus Schritt 5.
2. Optionen des alten Add-ons sichern: Tab **Konfiguration** → Menü ⋮ → **Als YAML
   bearbeiten**, alles kopieren.
3. Altes Add-on **stoppen** und **Beim Start ausführen** ausschalten. Beide
   zugleich würden dieselben Entities melden, und ein MH201 nimmt nur vier
   Sitzungen an.
4. Neues Add-on installieren (1.1) und **einmal ohne Optionen starten.** Dabei
   legt Home Assistant den Ordner `/addon_configs/474ea02f_myhome_scs_bridge/` an,
   und das Add-on beendet sich gleich wieder mit `No gateway host configured` –
   an dieser Stelle richtig so.
5. `/addon_configs/local_myhome_scs_bridge/devices.yaml` (bzw. die Datei aus der
   Option `devices_file`) in den neuen Ordner kopieren.
6. Beim neuen Add-on: Tab **Konfiguration** → **Als YAML bearbeiten**, die gesicherten
   Optionen einfügen, speichern. `instance_id` muss gleich bleiben.
7. Starten und prüfen: im Protokoll `MyHome SCS Bridge … starting` und
   `gateway link is up`, in Home Assistant dieselben Entities mit ihrem Verlauf.

**Zurück:** neues Add-on stoppen, altes wieder starten. **Aufräumen** erst, wenn alles
ein paar Tage läuft: altes Add-on deinstallieren, Ordner `/addons/myhome_scs_bridge`
löschen.

### 1.3 Lokal: auf dem Mac packen

Im Terminal – `COPYFILE_DISABLE=1` verhindert die `._`-Dateien von macOS, über
die der Supervisor stolpert; Cache-Ordner kommen nicht mit:

```bash
cd "<Projektordner>"
COPYFILE_DISABLE=1 tar -czf myhome_scs_bridge.tar.gz \
  --exclude '__pycache__' --exclude '.ruff_cache' --exclude '._*' --exclude '.DS_Store' \
  -s '/^myhome_scs_mqtt/myhome_scs_bridge/' \
  myhome_scs_mqtt
tar -tzf myhome_scs_bridge.tar.gz | head -5
tar -tzf myhome_scs_bridge.tar.gz | grep -E '__pycache__|/\._|DS_Store'
```

Der erste Prüfbefehl muss Pfade zeigen, die mit `myhome_scs_bridge/` beginnen;
der zweite darf **nichts** ausgeben. (Sollte doch ein `__pycache__` mitkommen,
schadet das nicht – `.dockerignore` hält es aus dem Image. Entscheidend sind die
`._`-Dateien, und die verhindert `COPYFILE_DISABLE=1`.) Der Ordner heißt im Archiv bewusst
`myhome_scs_bridge`: Der Ordner des alten Add-ons unter `/addons` heißt
`myhome_scs_mqtt` und darf nicht überschrieben werden.

### 1.4 Lokal: nach Home Assistant kopieren

1. `myhome_scs_bridge.tar.gz` in die Samba-Freigabe **`share`** legen (oder per
   `scp` nach `/share/`). Bewusst nicht nach `addons`: Dort soll nur der
   entpackte Add-on-Ordner liegen.
2. Im Terminal (Add-on *Terminal & SSH*) entpacken:

   ```bash
   cd /addons && tar -xzf /share/myhome_scs_bridge.tar.gz
   ls /addons/myhome_scs_bridge
   ```

Danach muss es `/addons/myhome_scs_bridge/config.yaml` geben, und
`/addons/myhome_scs_mqtt` (das alte Add-on) ist unverändert.

Für einen geänderten Stand dasselbe noch einmal: neues Archiv packen und
entpacken (überschreibt die Dateien im Ordner `myhome_scs_bridge`), dann im
Add-on-Store *Nach Updates suchen*. Laut Home-Assistant-Entwicklerdoku zeigt das
Add-on nur bei höherer Version in `config.yaml` ein Update an; bei gleicher
Version das Add-on deinstallieren und neu installieren. **Vor dem
Deinstallieren** `/addon_configs/local_myhome_scs_bridge/devices.yaml` sichern
(z. B. nach `/share` kopieren).

### 1.5 Lokal: installieren

1. *Einstellungen → Add-ons* (in neueren Versionen *Apps*) → *Add-on-Store*
   (*App store*) → Menü ⋮ → **Nach Updates suchen**.
2. Oben erscheint **Lokale Add-ons** (*Local apps*) mit **MyHome SCS Bridge**.
3. **Installieren.** Das Add-on wird auf dem Gerät gebaut. Dafür braucht Home
   Assistant Internet: Es lädt das Basis-Image
   `ghcr.io/home-assistant/base:3.22` und die fertigen Python-Pakete
   (`paho-mqtt`, `PyYAML`). Kompiliert wird nichts.
4. Tab **Konfiguration**: mindestens `gateway_host` (IP des Gateways, z. B.
   `192.0.2.10`) und `gateway_password` eintragen, speichern.
5. **Starten** und den Tab **Protokoll** öffnen. Erwartet werden u. a.
   `MyHome SCS Bridge 1.27.0 starting`, `MQTT connected` und
   `gateway link is up`.

Bleibt `gateway_host` leer, beendet sich das Add-on sofort mit
`No gateway host configured - set 'gateway_host' in the add-on options`.

### 1.6 Vor dem ersten Start bedenken

- **Sitzungen am Gateway:** Das Add-on hält zwei Sitzungen. Ein **MH201 nimmt
  höchstens vier** an – laufen alte und neue Bridge gleichzeitig, ist er voll,
  und MyHOME_Suite kommt nicht mehr hinein. Die fünfte Verbindung wird
  angenommen und sofort geschlossen; im Protokoll steht dann
  `an MH201 allows only four sessions at a time`. Ein F461 nahm mindestens acht.
- **Für eine saubere Messung die alte Bridge vorher stoppen** – sonst mischt
  sich deren Busverkehr in die Zahlen, und beide fragen die Anlage doppelt ab.
- **Geräte:** Die neue Bridge startet mit einer leeren Gerätedatei und scannt
  den Bus beim ersten Start selbst (`scan_on_start`). Dabei entstehen in Home
  Assistant **eigene** Entities neben denen der alten Bridge. Wer die Namen
  übernehmen will: nach dem ersten Start das Add-on stoppen,
  `/addon_configs/local_myhome_scs_mqtt/devices.yaml` nach
  `/addon_configs/local_myhome_scs_bridge/devices.yaml` (aus dem Repository:
  `/addon_configs/474ea02f_myhome_scs_bridge/devices.yaml`) kopieren, wieder
  starten. Vorher die alte Datei auf unechte Einträge prüfen: Die alte Bridge
  hat eine bloße Kontakt-Abfrage (z. B. `*#25*312##`) als Gerät gewertet, und
  „Speichern“ in ihrer Weboberfläche hat solche Einträge in die Datei
  übernommen (siehe auch die offene Frage zu den Tastern 21, 23, 24 in
  `WISSEN.md`).

## 2. Hardware-Test: Messplan

Ziel ist, die vorläufige Bremse („3 Störungen in 3 Sekunden“) an echter
Hardware zu prüfen und den endgültigen Wert festzulegen.

### 2.1 Einstellungen für den Test

| Option | Wert für den Test | Warum |
|---|---|---|
| `log_level` | `trace` | jeder gesendete (`>>>`) und empfangene (`<<<`) Frame mit Uhrzeit auf die Millisekunde (`18:42:07.215`) – daraus lässt sich die echte Rate und jeder Abstand ablesen; enthält auch alles aus `debug` |
| `log_level` (Alternative) | `debug` | ohne Rohframes: Wartezeit je Frame, Bremse, Verbindungsdetails |
| `bus_brake_threshold` / `bus_brake_window` | `3` / `3.0` (Standard) | Startwert; bei Bedarf zum Vergleich z. B. `5` / `3.0` |
| `poll_interval` | `60` (nur während des Tests) | mehr Abfragerunden in kurzer Zeit; danach wieder `300` |

Mit `trace` wird das Protokoll schnell lang. Für die Auswertung das ganze
Protokoll sichern – im Protokoll-Tab oder im Terminal (Add-on *Terminal & SSH*)
mit `ha apps logs local_myhome_scs_bridge`, aus dem Repository
`ha apps logs 474ea02f_myhome_scs_bridge` (ältere Schreibweise `ha addons logs …`). Nach dem Test `log_level` wieder auf `info` stellen.

### 2.2 Wo die Zahlen stehen

- **Weboberfläche** (Seitenleiste *MyHome SCS Bridge*), Kacheln oben: **Bus**
  (`…/min` gesendet und empfangen), **Warteschlange**, **Bremse** (`aus` oder der
  Abstand in Sekunden, darunter abgelehnte Frames und Frames ohne Antwort) und
  **Geräte** (darunter die stillgelegten Adressen). Sie aktualisieren sich alle
  drei Sekunden.
- **`/api/status`** liefert dieselben Werte als JSON: `sent`, `received`,
  `sent_per_minute`, `received_per_minute`, `queue`, `throttled` (wie oft die
  harte Grenze von 5 Frames/s gebremst hat), `refused`, `unanswered`,
  `brake_gap` (aktueller Mindestabstand in Sekunden, `0` = keine Bremse),
  `dropped`, `silenced` (Liste der stillgelegten Adressen), `scanning` und
  `instance` (bei mehreren Instanzen das Namenspräfix bzw. die `instance_id`,
  bei einer einzigen Instanz leer – siehe Kapitel 4).
- **Home Assistant**, Gerät *MyHome SCS Bridge*: Diagnose-Sensoren
  **Abgelehnte Frames** und **Unbeantwortete Frames** (Zähler seit dem Start,
  höchstens alle 5 Sekunden aktualisiert).

### 2.3 Protokollzeilen

| Zeile | Stufe | Bedeutung |
|---|---|---|
| `[command] >>> *1*1*12##` / `[event] <<< *1*1*12##` | trace | ein Frame raus bzw. rein |
| `sent *1*1*12## after 0.00s in the queue, 0.06s until: acknowledged` | debug | Latenz eines Frames: Wartezeit in der Warteschlange, dann bis zur Antwort (enthält Obergrenze und Bremse) |
| `gateway is busy (3 troubles within 3.0s, last: refused) - slowing down` | info | Bremse greift (höchstens alle 10 Minuten als info) |
| `brake - 1.0s between two frames after: no answer` | debug | Bremse zieht weiter an |
| `holding back 0.42s (background)` | debug | eine Abfrage wartet – wegen Bremse, Reserve oder harter Grenze |
| `holding back 0.20s (command)` | debug | ein Befehl wartet; das kann **nur** die harte Grenze von 5 Frames/s sein |
| `the gateway keeps up again - brake released` | debug | Bremse wieder gelöst |
| `[command] gateway refused *#1*99##` | warning | Ablehnung (je Frame nur beim ersten Mal als Warnung) |
| `[command] no answer for *#1*99##` | warning | keine Antwort (je Frame nur beim ersten Mal als Warnung) |

### 2.4 Abläufe

1. **Ruhebetrieb, 10 Minuten:** nichts tun. Erwartet: ein gleichmäßiger Strom
   statt Schübe – bei 40 Geräten und `poll_interval: 300` rund **27 Frames pro
   Minute**, keine Spitze über 60. `refused`/`unanswered` bleiben nahe 0,
   `brake_gap` bleibt 0, `queue` bleibt klein. (Vor 1.22.0 sprang es hier auf
   60 gesendet / 180 empfangen pro Minute.)
2. **Schalten:** einige Lichter und Rollläden aus Home Assistant schalten. Im
   Protokoll (`debug`) die Zeilen `sent … after …` ansehen – Ziel: unter einer
   Sekunde vom Klick bis zur Antwort.
3. **Nicht vorhandene Adresse:** in `devices.yaml` einen Eintrag mit einer
   Adresse anlegen, die es auf der Anlage sicher nicht gibt, Add-on neu
   starten, drei Abfragerunden abwarten (`poll_interval: 60`). Dabei einen
   echten Schalter betätigen. Erwartet: `refused` zählt hoch, nach drei Runden
   steht `Adresse … wurde 3-mal abgelehnt` im Protokoll, das Gerät ist in Home
   Assistant **nicht verfügbar** und steht in der Kachel **Geräte** unter
   „stillgelegt“. Danach zählt `refused` nicht weiter (nur alle 10 Minuten ein
   Versuch). `brake_gap` bleibt 0, Schalten bleibt unter einer Sekunde.
4. **Scan-Schwall:** in Home Assistant **Bus gründlich scannen** drücken
   (rund 400 Abfragen, gut zwei Minuten). Erwartet: Die Kachel **Bus-Scan**
   zeigt den Fortschritt, Schalten ist träge, **funktioniert aber** – nicht mehr
   abgelehnt. Direkt nach dem Scan: `queue` fällt auf 0 (Protokoll: `N queued
   requests dropped`), `brake_gap` ist spätestens nach ein paar Sekunden 0 und
   alles ist wieder normal. **Kein Nachlauf von Minuten.**
5. **Viele Rollläden zugleich:** mehrere Rollläden gleichzeitig auf eine
   Zwischenstellung fahren. Beobachten: `queue`, `brake_gap`, erreichen alle ihr
   Ziel?
6. **Gateway-Neustart** (falls möglich): Gateway kurz stromlos machen.
   Erwartet: `gateway link is down`, nach dem Wiederkommen `gateway link is
   up`, `brake_gap` wieder 0.

Notieren: Uhrzeit, Ablauf, die Werte aus den Kacheln vorher/nachher und
auffällige Protokollzeilen. Daraus wird der endgültige Schwellenwert.

## 3. Zurück zur alten Bridge / aufräumen

- **Umschalten:** neue Bridge stoppen, alte starten. Die Entities der neuen
  Bridge stehen dann in Home Assistant als „nicht verfügbar“.
- **Entities der neuen Bridge ganz entfernen:** Die Discovery-Nachrichten sind
  aufbewahrt (retained) und überdauern eine Deinstallation. Im MQTT Explorer
  die Zweige `homeassistant/+/scs_bridge/…` und `scs_bridge/…` leeren – das
  betrifft ausschließlich die neue Bridge.
- **Basis-Topic nie auf `myhome` stellen, solange die alte Bridge läuft:** Beide
  räumen beim Start Topics unter ihrem Basis-Topic weg, die sie selbst nicht
  kennen.

## 4. Zweite Anlage: Add-on mehrfach installieren

Ein Home Assistant kann das Add-on **mehrfach** betreiben – eine Instanz je
MyHome-Anlage, also je Gateway. Home Assistant installiert ein lokales Add-on
aber nur einmal; für eine zweite Instanz braucht es eine **zweite Kopie des
Ordners mit eigenem Slug**.

**Warum es ohne eigene Kennungen nicht geht:** Zwei Instanzen würden sich sonst
mit **derselben MQTT-Client-ID** am Broker anmelden (der Broker trennt dann
abwechselnd beide – eine Endlosschleife), für dieselbe Bus-Adresse **dieselbe
`unique_id`** erzeugen (ein Licht „12" in Haus 1 und in Haus 2 wären für Home
Assistant ein Gerät) und beim Aufräumen die Discovery-Nachrichten der anderen
löschen. Deshalb müssen `instance_id`, `mqtt_base_topic` und `devices_file` je
Instanz **unterschiedlich** sein.

### 4.1 Zweiten Add-on-Ordner anlegen

Im Terminal (Add-on *Terminal & SSH*):

```bash
cp -r /addons/myhome_scs_bridge /addons/myhome_scs_bridge_2
```

In `/addons/myhome_scs_bridge_2/config.yaml` drei Zeilen ändern – der Slug muss
sich unterscheiden, sonst sieht der Supervisor nur ein Add-on:

```yaml
name: MyHome SCS Bridge Haus 2
slug: myhome_scs_bridge_2
panel_title: MyHome SCS Bridge Haus 2
```

Dann *Einstellungen → Add-ons → Add-on-Store* → Menü ⋮ → **Nach Updates
suchen**. Unter **Lokale Add-ons** steht nun zusätzlich *MyHome SCS Bridge
Haus 2*; installieren und starten wie in Kapitel 1.5.

Bei jedem Update sind es zwei Ordner: neues Archiv entpacken, danach erneut
`cp -r` und die drei Zeilen in der Kopie wieder anpassen. (Die Kopie liegt in
`/addons`; der Supervisor baut sie als eigenes Add-on.)

### 4.2 Optionen der zweiten Instanz

Diese Optionen müssen sich **zwingend** von der ersten Instanz unterscheiden:

| Option | erste Instanz | zweite Instanz |
|---|---|---|
| `instance_id` | `scs_bridge` | `scs_bridge_2` (oder `haus2`) |
| `mqtt_base_topic` | `scs_bridge` | `scs_bridge_2` |
| `devices_file` | `/config/devices.yaml` | `/config/devices_haus2.yaml` |

Dazu, wie bei jeder Installation: `gateway_host` und `gateway_password` der
**zweiten** Anlage. Optional `name_prefix`, z. B. `Haus 2` – dann heißen die
Geräte in Home Assistant „Haus 2 Küche" statt „Küche", und auch das
Diagnose-Gerät des Add-ons ist unterscheidbar.

`devices_file` zeigt auf den Ordner **dieser** Instanz: `/config` im Add-on ist
auf dem Host `/addon_configs/local_myhome_scs_bridge_2/`. Jede Instanz hat
also ohnehin ihr eigenes `/config` – ein eigener **Dateiname** ist trotzdem
sinnvoll, damit beim Kopieren von Hand nichts durcheinandergeht.

Lässt man `instance_id` auf dem Standard und ändert nur das Basis-Topic (oder
umgekehrt), steht beim Start eine Warnung im Protokoll
(`This looks like a half-converted second instance …`). Wer nur **eine** Instanz
betreibt, kann sie ignorieren.

### 4.3 Was dabei zu bedenken ist

- **Sitzungen am Gateway:** Jede Instanz hält **zwei** Sitzungen zu **ihrem**
  Gateway. Zwei Instanzen an zwei Gateways sind also je zwei – unkritisch. Ein
  **MH201 nimmt höchstens vier** Sitzungen an; zwei Instanzen auf dasselbe
  Gateway zu richten füllt es vollständig (siehe Kapitel 1.6).
- **Ein Broker genügt.** Beide Instanzen melden sich am selben MQTT-Broker an,
  mit eigener Client-ID (`instance_id` + Rechnername).
- **Die `instance_id` nachträglich zu ändern kostet die Historie.** Sie steckt
  in jeder `unique_id`; nach einer Änderung legt Home Assistant alle Entities
  neu an, die alten bleiben als „nicht verfügbar" zurück. Für die erste,
  bestehende Instanz deshalb `scs_bridge` **so lassen**.
- **Beide Instanzen aufräumen nur im eigenen Zweig:** `scs_bridge/…` bzw.
  `scs_bridge_2/…` und `homeassistant/+/<instance_id>/+/config`.

## 5. Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| `gateway_host` | leer (Pflicht) | IP-Adresse des Gateways |
| `gateway_port` | `20000` | OpenWebNet-Port |
| `gateway_password` | `12345` | OPEN-Passwort; das klassische Verfahren (MH201) verlangt reine Ziffern, HMAC (F461) erlaubt auch Buchstaben |
| `gateway_auth` | `auto` | `auto`, `none`, `open` (klassische Nonce) oder `hmac` – nur zur Fehlersuche erzwingen |
| `mqtt_host` … `mqtt_password` | leer | eigener Broker oder Benutzer; leer = Broker aus Home Assistant |
| `mqtt_discovery_prefix` | `homeassistant` | muss zum Präfix der MQTT-Integration passen |
| `mqtt_base_topic` | `scs_bridge` | Präfix aller Status- und Kommando-Topics |
| `devices_file` | `/config/devices.yaml` | Pfad im Add-on; auf dem Host `/addon_configs/local_myhome_scs_bridge/devices.yaml`, aus dem Repository `/addon_configs/474ea02f_myhome_scs_bridge/devices.yaml` |
| `instance_id` | `scs_bridge` | Kennung dieser Instanz: Präfix jeder `unique_id`, Discovery-Knoten, Teil der MQTT-Client-ID. Nur Kleinbuchstaben, Ziffern und `_`, höchstens 32 Zeichen, nicht `myhome_scs_bridge` enthalten. **Nur ändern, wenn das Add-on mehrfach läuft** (Kapitel 4) – eine Änderung legt alle Entities neu an |
| `name_prefix` | leer | steht vor jedem Gerätenamen, z. B. `Haus 2` → „Haus 2 Küche". Leer = Namen unverändert |
| `auto_discover` | `true` | unbekannte Bus-Adressen automatisch anlegen |
| `scan_on_start` | `true` | Bus beim Start scannen, solange kein Gerät bekannt ist |
| `scan_zones` / `scan_meters` / `scan_contacts` | `20` / `8` / `20` | Umfang des Scans |
| `poll_interval` | `300` | Sekunden zwischen zwei Abfragerunden, `0` = aus |
| `bus_brake_threshold` | `3` | so viele Störungen (abgelehnt oder unbeantwortet) … |
| `bus_brake_window` | `3.0` | … innerhalb so vieler Sekunden lassen die Bremse greifen – **vorläufig**, wird nach der Hardware-Messung festgelegt |
| `command_reserve` | `1.5` | Frames pro Sekunde, die für Befehle frei bleiben; Abfragen und Scan dürfen nur den Rest der fünf Frames nutzen |
| `dead_after` | `3` | so oft hintereinander abgelehnt = Adresse stillgelegt (nicht mehr abgefragt, in HA „nicht verfügbar“); `0` schaltet das ab |
| `log_level` | `info` | `trace`, `debug`, `info`, `warning`, `error` |

## 6. Buslast und Bremse

Wie schnell das Add-on sendet, entscheidet eine einzige Stelle – für Befehle,
Abfragen, Keepalive und den Bus-Scan gleichermaßen:

- **Harte Obergrenze:** höchstens fünf Frames pro Sekunde, kurze Spitzen bis
  zehn, damit ein einzelner Befehl nie wartet.
- **Reserve für Befehle:** Der eigene Hintergrundverkehr (Abfragerunde, Sonden,
  Bus-Scan) darf nur `5 − command_reserve` Frames pro Sekunde ziehen. So bleibt
  auch während eines Scans Platz für einen Tastendruck.
- **Bremse:** Liegen `bus_brake_threshold` Störungen innerhalb von
  `bus_brake_window` Sekunden, wächst der Mindestabstand zwischen zwei Frames je
  weiterer Störung um 0,5 s, höchstens auf 2 s. Jede Quittung verkleinert ihn
  um 0,1 s, und nach jedem störungsfreien Zeitfenster **halbiert** er sich –
  aus 2 s werden so in wenigen Sekunden 0 statt in Minuten.
- **Ablehnungen zählen nur, wenn sie von mehreren Adressen kommen.** Eine
  einzige nicht vorhandene Adresse, die bei jeder Runde abgelehnt wird, bremst
  damit nichts mehr (sie wird zusätzlich stillgelegt, siehe unten).
  **Ausbleibende Antworten** zählen dagegen immer – das ist das ehrlichste
  Zeichen für einen überlasteten Bus.
- **Die Bremse gilt nur für den Hintergrund.** Ein Befehl aus Home Assistant
  wartet nie auf sie; sie schützt den Bus vor der eigenen Last des Add-ons, und
  das ist kein Grund, einen Tastendruck zu verzögern. Jede Spur der
  Warteschlange hat dafür einen eigenen Sender.
- **Abgelehnte Fähigkeits-Abfragen zählen nie** („hat diese Zone einen
  Lüfter?“ – eine Ablehnung ist dort eine Antwort). Stille zählt immer.
- Es gilt jeweils die strengere Regel; die Wartezeiten addieren sich nicht.
  Nach einem Wiederverbinden beginnt die Verbindung ohne Bremse; die Zähler
  laufen weiter.
- Die Antworten des Bus-Scans kennt das Add-on nicht einzeln – der Scan wartet
  auf dieselbe Grenze und Bremse, bewegt die Bremse aber nicht selbst.
- Unabhängig davon lassen Rollläden Positionsabfragen aus, solange mehr als
  vier Frames in der Warteschlange stehen.
- **Öffnen und Schließen werden nicht im Sekundentakt verfolgt.** Während der
  Fahrt steht in Home Assistant „öffnet“ bzw. „schließt“, die Position bleibt bis
  zum Halt die alte. Bleibt der Motor stehen, holt das Add-on die Position mit
  **einer** Abfrage. Kommt die Stopp-Meldung nicht an, sieht es alle 30 Sekunden
  einmal nach. Nur eine Zwischenstellung, die das Add-on selbst anfahren muss
  (Regelkreis), fragt weiter jede Sekunde – ohne das träfe sie das Ziel nicht.

### 6.1 Takt der Abfragerunde

Eine Abfragerunde verteilt sich über die **Hälfte** von `poll_interval`, damit
die andere Hälfte ruhig bleibt – statt wie früher mit festen 0,5 s je Frame
einen Schwall zu erzeugen. Grenzen: nie schneller als 0,5 s (das erlaubt die
Buslast nicht) und nie langsamer als 5 s je Frame.

Wie viele Abfragen eine Runde hat, hängt am Gerätetyp (aus dem Code gezählt):

| Gerät | Frames je Runde |
|---|---|
| Licht/Dimmer | 1 (in der ersten Runde 2) |
| Rollladen ohne Positionsmeldung | 2 |
| Heizzone | 6 (Status, Temperatur, Sollwert, Ventile, zwei Sonden) |
| Melder | 2 (Lux, Präsenz) |
| Energiezähler | 2 bis 4 |
| Kontakt/Taster | 0 bis 1 |

Zwei Beispiele: **40 Geräte** (18 Lichter, 9 Rollläden, 5 Zonen, 1 Melder,
7 Kontakte) ergeben **68 Abfragen** – bei `poll_interval: 300` also ein Frame
alle 2,2 s, rund 27 Frames pro Minute. **200 Geräte** ergeben **365 Abfragen**;
bei 300 s wäre der Takt rechnerisch 0,41 s und damit schneller als erlaubt –
dann greift die Untergrenze von 0,5 s, die Runde dauert 3 Minuten und im
Protokoll steht ein Hinweis. Für so eine Anlage besser `poll_interval: 900`:
Takt 1,2 s, Runde 7,5 Minuten, etwa 25 Frames pro Minute im Mittel.

### 6.2 Stillgelegte Adressen

Wird eine Adresse `dead_after`-mal hintereinander abgelehnt, fragt das Add-on
sie nicht mehr ab:

- Im Protokoll steht einmal `Adresse 12342 (…) wurde 3-mal abgelehnt – sie wird
  vorerst nicht mehr abgefragt` (Stufe warning).
- In Home Assistant wird das Gerät **nicht verfügbar** (eigenes Topic
  `<basis>/<typ>/<adresse>/reachable`, retained).
- In der Weboberfläche steht in der Kachel **Geräte** `1 stillgelegt: lights 12342`.
- Alle zehn Minuten geht **eine** Abfrage raus. Antwortet die Adresse – oder
  meldet sie sich von selbst über den Bus –, ist sie sofort wieder normal
  (`… antwortet wieder`, Stufe info).
- **Befehle aus Home Assistant gehen immer raus.** Nur das regelmäßige Abfragen
  ruht; so lässt sich ein korrigierter Eintrag sofort ausprobieren.

Das ist der Normalfall für einen Tippfehler in der Gerätedatei oder ein
ausgebautes Gerät. Zeigt die Kachel ein Gerät, das es wirklich gibt, dann
stimmt die Adresse nicht – oder das Gateway erreicht es nicht.

Zwei Adressen werden **nie** stillgelegt: eine, die sich zwei Geräte absichtlich
teilen (ein BMSE-Melder neben einem Lichtpunkt – aus der Adresse allein ist
nicht zu erkennen, wen eine Abfrage meinte), und eine, deren Abfragen nur in
*derselben* Runde abgelehnt wurden (eine Heizzone fragt sechs Dinge auf einmal;
ein kurzer Lastschwall ist kein Beweis, dass sie fehlt).

### 6.3 Während eines Bus-Scans

Der Scan bleibt absichtlich schnell. Seine Abfragen gelten als
Hintergrundverkehr: Sie lassen die Reserve für Befehle frei, und ihre Ablehnung
(die meisten gescannten Adressen gibt es nicht) kann die Bremse nie auslösen.
**Schalten ist währenddessen träge, funktioniert aber.** Die Kachel **Bus-Scan**
zeigt den Fortschritt und den Hinweis „Schalten kann dauern“.

Nach dem Scan wird die Hintergrund-Warteschlange geleert – was dort steht, ist
Minuten alt, und der Scan hat gerade alles abgefragt. Die Abfragerunde startet
30 Sekunden später einmal neu. Im Protokoll: `N queued requests dropped - they
are out of date`.

## 7. Weboberfläche

In der Seitenleiste unter **MyHome SCS Bridge**, über Ingress – kein
zusätzlicher Port, keine zweite Anmeldung. Die Oberfläche nimmt nur
Verbindungen über Home Assistants Ingress an.

- **Kacheln oben** zeigen den Zustand auf einen Blick: Gateway, MQTT, Geräte
  (mit den stillgelegten Adressen), Bus, Warteschlange, Bremse und Bus-Scan. Der
  Rand links ist grün, wenn etwas in Ordnung ist, gelb bei einer Auffälligkeit
  und rot bei einer getrennten Verbindung. Unten rechts steht die Version.
- **Geräte bearbeiten, anlegen und löschen** – beim Speichern landen
  eingetragene und automatisch erkannte Geräte gemeinsam in der Gerätedatei. Ein
  Gerät mit dem Kennzeichen **neu** wurde am Bus gefunden und steht noch nicht in
  der Gerätedatei. Das **Suchfeld** neben den Knöpfen zeigt nur die Zeilen, deren
  Name oder Adresse passt.
- **Zustand je Gerät** – neben dem Namen steht, was das Gerät gerade tut (`an ·
  40 %`, `öffnet`, `21.5 °C · Soll 20.0 °C`, `850 W`, `stillgelegt`). Ein Strich
  heißt: Von dieser Adresse kam noch nichts über den Bus. Bei Lichtern und
  Schaltaktoren schaltet **Schalten** das Gerät um – so zeigt sich, welche Lampe
  an einer Adresse hängt. Rollläden und Heizzonen haben den Knopf bewusst nicht.
  Der Befehl geht denselben Weg wie einer aus Home Assistant.
- **Entfernte Adressen bleiben weg** – eine gelöschte Zeile käme sonst wieder,
  sobald sich die Adresse am Bus meldet (`auto_discover`). Mit dem Speichern merkt
  sich das Add-on die entfernten Adressen und legt sie nicht mehr an, auch nicht
  bei einem Bus-Scan. Sie stehen unten unter **Ausgeblendete Adressen**;
  **zurückholen** (vor dem Speichern) bringt die Zeile wieder, **wieder zulassen**
  (danach) nimmt die Adresse von der Liste. Die Liste liegt in den Daten des
  Add-ons (`ignored.json`), nicht in der Gerätedatei.
- **Die Liste wird geprüft, während sie bearbeitet wird** – mit denselben
  Geräteklassen, die die Gerätedatei beim nächsten Start lesen. Fehler stehen rot
  am Feld und über der Tabelle: eine Adresse doppelt (auch als Licht und als
  Schaltaktor), eine Zeile mit Namen ohne Adresse, Zeichen, die in keiner Adresse
  vorkommen, Schließer und Öffner zugleich, Warm über Kalt, negative Zeiten.
  Solange ein Fehler in der Liste steht, wird nicht gespeichert. Leere Zeilen
  werden wie bisher nicht mitgespeichert.
- **Speichern & neu starten** übernimmt Änderungen sofort, **Nur speichern**
  beim nächsten Start. Bis dahin steht über der Liste ein Hinweis mit dem Knopf
  **Jetzt neu starten**, und die Seite zeigt weiter den gespeicherten Stand.
  „Ungespeicherte Änderungen“ neben den Knöpfen erinnert daran, dass noch nichts
  geschrieben ist.
- **Hinweise über der Geräteliste**, wenn etwas eine Entscheidung braucht
  (Kapitel 7.2).
- **Bus scannen / Gründlich scannen** – die Kachel **Bus-Scan** zeigt den
  Fortschritt, die Knöpfe sind solange gesperrt, und neu gefundene Geräte
  erscheinen von selbst in der Liste.
- **Bus-Mitschnitt** mit Uhrzeit, Wiederholungen und der Geräteart samt Adresse;
  darüber ein Feld, um einen Frame von Hand zu senden. Angenommen wird genau ein
  Frame aus Ziffern, `*` und `#`; die Antwort des Gateways (angenommen,
  abgelehnt, keine Antwort) erscheint als Meldung. Eine Ablehnung von Hand zählt
  nicht für die Bremse. **Filtern** zeigt nur Zeilen mit einer Adresse, einem
  Frame oder einer Geräteart, **Anhalten** lässt die Liste stehen, **Leeren**
  räumt sie ab. Die Liste steht auch still, solange darin gescrollt oder Text
  markiert ist. Steht eine Adresse noch nicht in der Geräteliste (bei
  abgeschalteter Geräteerkennung oder nach dem Löschen einer Zeile), holt
  **+ übernehmen** sie dorthin.
- **Auf dem Handy** wird aus jeder Tabellenzeile eine kleine Karte mit
  beschrifteten Feldern. **Hell oder dunkel** richtet sich nach dem Thema von Home
  Assistant; außerhalb von Home Assistant nach dem Betriebssystem.

### 7.1 Farbige Leuchten (RGBW) und Tunable White

Gilt für DALI-Leuchten hinter dem DALI-Gateway, die Farbe oder Weißtemperatur
können. Gemessen an der Anlage am 19.09.2026.

In der Zeile der Leuchte gibt es fünf Felder:

| Feld | Bedeutung |
|---|---|
| **Dimmer** | Helligkeit in Prozentschritten (Dimension 1) |
| **RGBW** | Farbe über Dimension 12 – Home Assistant zeigt das Farbrad |
| **TunableW** | Weißtemperatur über Dimension 14 – Home Assistant zeigt den Weißregler |
| **Warm (K)** | wärmster Wert der Leuchte, Standard 2000 |
| **Kalt (K)** | kältester Wert der Leuchte, Standard 6200 |

- **Helligkeit und Farbe sind getrennt.** Die Farbe trägt nur Farbton und
  Sättigung; wie hell die Leuchte brennt, steht weiter im Dimmwert. Deshalb
  ändert ein Farbwechsel die Helligkeit nicht und umgekehrt. (Der dritte Wert
  im Farb-Frame ist immer 100 – er ist **nicht** die Helligkeit.)
- **RGBW und TunableW dürfen beide gesetzt sein.** Home Assistant zeigt immer
  die zuletzt gemeldete Art an: meldet die Leuchte eine Farbe, steht sie auf
  Farbe, meldet sie eine Weißtemperatur, steht sie auf Weiß.
- **Beide Häkchen setzen den Dimmer mit** – eine DALI-Leuchte mit Farbe dimmt
  immer in Prozentschritten.
- **Selbst erkannt:** Sobald die Leuchte einmal eine Farbe oder eine
  Weißtemperatur meldet (etwa weil jemand sie am Taster oder in MyHOME_Suite
  verstellt), setzt das Add-on das Häkchen selbst, meldet die Leuchte in Home
  Assistant neu an und schreibt es beim nächsten Speichern in die Gerätedatei.
- **Warm/Kalt begrenzen nur die eigenen Befehle.** Die Leuchte schneidet ohnehin
  auf ihren Bereich zu; die Felder verhindern Frames, die nichts bewirken, und
  sagen Home Assistant, welchen Bereich es anbieten soll. Meldet die Leuchte
  einen Wert außerhalb, wird er auf den Bereich gezogen.
- **Einschalten samt Farbe:** Eine ausgeschaltete Leuchte ignoriert einen
  Farbbefehl und geht davon *nicht* an. Das Add-on schickt deshalb zuerst den
  Einschaltbefehl (oder die Helligkeit) und danach die Farbe – zwei Frames im
  normalen Bustempo, also rund 0,2 s auseinander.
- **Nach einem Neustart** fragt das Add-on Farbe und Weißtemperatur einmal ab,
  bis die Leuchte geantwortet hat. Danach kommt jede Änderung von selbst über
  den Bus; im Ruhebetrieb entsteht keine zusätzliche Last.

Die Frames dazu, falls du im Bus-Mitschnitt mitliest:

```
*#1*0211*#12*120*100*100##   Farbe setzen: Farbton 120°, Sättigung 100 %
*#1*0211*12*120*100*100##    Antwort der Leuchte
*#1*29*#14*37037##           Weißtemperatur setzen: 2700 K
*#1*29*14*37037##            Antwort der Leuchte
*#1*0211*12##                Farbe abfragen      (Sonde)
*#1*29*14##                  Weißtemperatur abfragen (Sonde)
```

Der Wert hinter `#14` ist **Mired × 100**, also `100 000 000 / Kelvin`:
2000 K = 50000, 2700 K = 37037, 4000 K = 25000, 6200 K = 16129. Je größer der
Wert, desto wärmer das Licht. (Beim Messen wurde 2700 K von Hand als 37040
getippt – über 370,4 Mired gerundet. Beides ist dieselbe Einstellung, 0,2 K
auseinander.)

Home Assistant bekommt die Weißtemperatur in **Kelvin** (Discovery-Felder
`color_temp_kelvin`, `min_kelvin`, `max_kelvin` der MQTT-Light-Integration,
siehe <https://www.home-assistant.io/integrations/light.mqtt/>). Diese Felder
gibt es seit Anfang 2025; eine ältere Home-Assistant-Version würde stattdessen
Mired erwarten.

### 7.2 Was das Add-on aus Home Assistant entfernt – und wann nicht

Home Assistant behält eine Entity, solange ihre Anmeldung im Broker steht. Wird
ein Gerät aus der Geräteliste gelöscht, muss das Add-on deshalb auch die Anmeldung
löschen – das tut es bei jedem Start für alles, was es nicht mehr kennt. Home
Assistant vergisst die Entity dann samt Namen, Raum und Verlauf. Damit das nur
Geräte trifft, die wirklich weg sollen, gelten seit 1.24.0 vier Regeln:

1. **Unvollständige Liste = nichts wird entfernt.** Lässt sich die Gerätedatei
   nicht lesen, ist ein Abschnitt falsch geschrieben (`ligths:`) oder passt ein
   Wert nicht (`travel_time: 25s`), steht über der Geräteliste rot, was nicht
   gelesen wurde. Beheben und neu starten.
2. **Mehr als fünf Geräte auf einmal = nur nach Bestätigung.** Über der
   Geräteliste steht dann, welche Geräte es wären. Erst prüfen: Zeigt
   `devices_file` auf die richtige Datei? Wurde das Add-on ohne seine Daten neu
   installiert oder umgezogen? Wurde `auto_discover` abgeschaltet, bevor die
   erkannten Geräte gespeichert waren? Sollen die Geräte wirklich weg, räumt der
   Knopf **Aus Home Assistant entfernen** auf (zwei Klicks). Solange die
   Bestätigung aussteht, bleibt alles stehen und es startet kein automatischer
   Bus-Scan. Im Protokoll: `… devices … are no longer in the device file or the
   discovery memory`.
3. **Die Seite speichert keinen veralteten Stand.** Findet das Add-on ein Gerät,
   während die Seite offen ist, lädt sie es nach. Ein Speichern von einem älteren
   Stand wird abgelehnt; die Seite ergänzt die neuen Geräte und meldet „Nichts
   gespeichert – bitte prüfen und noch einmal speichern“.
4. **Die ersetzte Gerätedatei bleibt liegen.** Neben der Gerätedatei stehen nach
   dem Speichern `devices.yaml.bak` (der Stand davor) und
   `devices.yaml.manual.bak` (der letzte von Hand geschriebene Stand, mit allen
   Kommentaren). Zurückholen: Add-on stoppen, die Sicherung über die Gerätedatei
   kopieren, starten.

## 8. Bus-Scan

Der Scan fragt den Bus aktiv ab und sendet ausschließlich Status- und
Dimensionsabfragen – er schaltet nichts. Auslösen: automatisch beim ersten
Start ohne bekannte Geräte, über die Knöpfe **Bus scannen** / **Bus gründlich
scannen** am Gerät *MyHome SCS Bridge* oder in der Weboberfläche. Neben der
Gerätedatei entsteht `scan_result.yaml` als fertige Vorlage. Taster und
Szenario-Module findet ein Scan nicht – sie melden sich erst beim Drücken.

## 9. Diagnose-Werkzeug ohne Home Assistant

Im Add-on-Ordner steckt ein Kommandozeilen-Werkzeug, das nur Python ab 3.11
braucht – weder MQTT noch PyYAML. Auf dem Mac mit der Projekt-venv (das
mitgelieferte `/usr/bin/python3` von macOS ist dafür zu alt):

```bash
cd "<Projektordner>/myhome_scs_mqtt"
export OWN_PASSWORD=12345
../.venv/bin/python -m app.probe --host 192.0.2.10              # Anmeldung, Firmware
../.venv/bin/python -m app.probe --host 192.0.2.10 --scan       # Anlage abfragen, scan_result.yaml
../.venv/bin/python -m app.probe --host 192.0.2.10 --monitor 60 # 60 s Bus mitschneiden
```

`--scan` schreibt die Vorlage in den aktuellen Ordner; mit `--report <Pfad>`
landet sie woanders (sonst kommt sie beim nächsten Packen mit ins Archiv).

Das Passwort kommt nur aus `OWN_PASSWORD`, nie aus einem Argument. Das Werkzeug
öffnet wie das Add-on beide Sitzungen (ein MH201 antwortet nur auf der
Ereignis-Sitzung) – bei laufendem Add-on sind am MH201 damit alle vier Plätze
belegt. `--verbose` zeigt jeden Frame.

## 10. MQTT-Topics

Alles hängt unter `mqtt_base_topic` (Standard `scs_bridge`), nach Gerätetyp
sortiert: `scs_bridge/light/12/state`, `scs_bridge/cover/21/position`,
`scs_bridge/climate/1/temperature`, `scs_bridge/keypad/22/button/1` usw.
Kontakte heißen nach ihrer Nummer (`contact/1`), mit abweichendem `bus_prefix`
zusätzlich nach dem Präfix (`contact/4_1` für IR-Schnittstelle 1);
Heizungsaktoren an einer Zone tragen die Zone (`pump/8_1`), die Pumpe an der
Zentrale nur ihre Nummer (`pump/3`).
Befehle nur auf den `…/set`-Topics. Unter `scs_bridge/bridge/` liegen `lwt`
(läuft das Add-on?), `availability` (steht die Gateway-Verbindung?), `link`,
`scan`, `devices` (Verzeichnis aller Geräte), `refused` und `unanswered`.
Tastendrücke sind nie retained und haben eine konstante Nutzlast; Zustände sind
retained.

## 11. Fehlersuche

| Symptom im Protokoll | Ursache und Abhilfe |
|---|---|
| `No gateway host configured` | `gateway_host` in der Konfiguration eintragen |
| `gateway refused the session` | IP von Home Assistant liegt nicht im freigegebenen IP-Bereich des Gateways |
| `gateway rejected the OPEN password` / `(HMAC)` | falsches OPEN-Passwort |
| `gateway closed the connection during login - an MH201 allows only four sessions` | Sitzungen belegt: alte Bridge, MyHOME_Suite oder das Diagnose-Werkzeug schließen |
| `keepalive timed out - dropping the command session` | Gateway antwortet nicht; die Verbindung wird neu aufgebaut |
| `gateway is busy … slowing down` | Bremse greift – siehe Kapitel 6 und den Messplan |
| `No MQTT broker found` | Mosquitto-Add-on installieren oder `mqtt_*` von Hand setzen |
| `web interface: refused a request from …` | ein Zugriff kam nicht über Home Assistants Ingress |
| `Not every device could be read - nothing is removed from the broker` | ein Eintrag der Gerätedatei ließ sich nicht lesen; was genau, steht in den Zeilen davor und rot in der Weboberfläche (Kapitel 7.2) |
| `… devices … are no longer in the device file or the discovery memory` | so viele Geräte auf einmal entfernt das Add-on nicht ungefragt – Ursache prüfen oder in der Weboberfläche bestätigen (Kapitel 7.2) |
| Entities „nicht verfügbar“ | Add-on läuft nicht oder die Gateway-Verbindung steht nicht (Diagnose-Entity *Gateway link*) |
