# Changelog

## 1.24.0

Schutz vor verlorenen Geräten. Home Assistant vergisst eine Entity samt Namen,
Raum und Verlauf, sobald ihre Anmeldung im Broker gelöscht wird – und das Add-on
räumt bei jedem Start weg, was es nicht mehr kennt. An mehreren Stellen konnte
das Geräte treffen, die es noch gibt. Am Bus und an den MQTT-Topics ändert sich
nichts.

- **Ein Tippfehler löscht keine Geräte mehr.** Ein falsch geschriebener
  Abschnitt (`ligths:` statt `lights:`) oder ein Wert, den eine Geräteart nicht
  lesen kann (`travel_time: 25s`), ließ das Gerät bisher still weg – und beim
  Aufräumen verschwand es aus Home Assistant. Jetzt gilt die Geräteliste dann als
  unvollständig: Es wird nichts entfernt, bis der Fehler behoben ist. Dasselbe
  gilt für einen Eintrag im Gedächtnis der erkannten Geräte, der sich nicht lesen
  lässt.
- **Viele Geräte auf einmal entfernt das Add-on nicht mehr ungefragt.** Würde ein
  Start mehr als fünf Geräte aus Home Assistant nehmen, bleibt alles stehen: Im
  Protokoll und in der Weboberfläche steht, welche Geräte es wären, und erst der
  Knopf „Aus Home Assistant entfernen“ (zwei Klicks) räumt auf. So sieht kein
  normales Löschen aus, sondern ein Versehen – ein falscher Pfad in
  `devices_file`, ein Add-on, das ohne seine Daten umgezogen ist, eine
  abgeschaltete Geräteerkennung. Bis zu fünf Geräte gehen wie bisher ohne
  Nachfrage. Solange eine Bestätigung aussteht, startet auch kein automatischer
  Bus-Scan.
- **Die Weboberfläche speichert nie mehr eine veraltete Liste.** Die Seite zeigt
  eine Kopie der Geräteliste. Fand das Add-on danach weitere Geräte, schrieb
  „Speichern“ sie bisher aus Gerätedatei und Gedächtnis zugleich heraus. Jetzt
  lädt die Seite neu erkannte Geräte selbst nach; ein Speichern von einem alten
  Stand lehnt das Add-on ab, die Seite ergänzt die Liste und bittet um ein
  zweites Speichern. Ein Gerät, das genau während des Schreibens auftaucht,
  bleibt gemerkt.
- **Nach „Nur speichern“ zeigt die Seite, was gespeichert wurde.** Bisher brachte
  ein Neuladen die alte Liste zurück, und das nächste Speichern schrieb sie über
  die neue Datei. Dazu ein Hinweis, dass die Änderungen erst ab dem nächsten Start
  gelten, mit Knopf zum Neustarten.
- **Die ersetzte Gerätedatei bleibt erhalten.** Vor jedem Speichern aus der
  Weboberfläche liegt der vorherige Stand als `devices.yaml.bak` daneben, der
  letzte von Hand geschriebene zusätzlich als `devices.yaml.manual.bak` – mit
  allen Kommentaren, die die Seite nicht kennt.
- **Die Weboberfläche zeigt mehr:** ob der MQTT-Broker verbunden ist, warum die
  Geräteliste unvollständig ist und welches Aufräumen auf eine Bestätigung wartet.
  `/api/status` liefert dafür `revision`, `problems`, `purge_held` und
  `restart_pending`.

## 1.23.1

- **Die Weboberfläche zeigt keinen Schriftzug mehr im Hintergrund.** Sonst bleibt
  alles, wie es war: Bus, MQTT, Geräte und Optionen arbeiten wie in 1.23.0.

## 1.23.0

- **Das Add-on läuft jetzt mehrfach parallel** – eine Instanz je MyHome-Anlage,
  zwei Gateways in einem Home Assistant. Vorher kollidierten zwei Instanzen an
  drei Stellen: Sie meldeten sich beim Broker mit **derselben Client-ID** an
  (der Broker trennt dann abwechselnd beide – eine Endlosschleife), sie
  erzeugten für dieselbe Bus-Adresse **dieselbe `unique_id`**, und beim
  Aufräumen löschte jede Instanz die Anmeldungen der anderen.
- **Neue Option „Kennung dieser Instanz" (`instance_id`, Standard
  `scs_bridge`).** Sie steckt in jeder `unique_id`, im Discovery-Knoten und in
  der MQTT-Client-ID. Erlaubt sind Kleinbuchstaben, Ziffern und `_`; alles
  andere wird beim Start mit einer verständlichen Meldung abgewiesen. Die
  Kennung darf `myhome_scs_bridge` nicht enthalten – das alte Add-on „MyHome SCS
  MQTT Bridge" löscht jede Discovery-Nachricht, deren Topic diesen Text enthält.
- **Beim Standardwert bleibt alles, wie es war.** Alle `unique_id`s, Topics,
  Discovery-Knoten, Gerätekennungen und Client-IDs sind zeichengleich zu
  1.22.0 – eine laufende Installation behält ihre Entities und ihre Historie.
  Ein eigener Test hält die erzeugten Discovery-Nutzlasten dagegen.
- **Neue Option „Namenspräfix" (`name_prefix`, Standard leer).** Steht vor jedem
  Gerätenamen („Haus 2 Küche"). Leer = Namen unverändert. In der Gerätedatei
  bleibt der Name ohne Präfix stehen, sonst würde er beim Speichern aus der
  Weboberfläche immer länger.
- **Warnung bei halb umgestellter zweiter Instanz:** Weichen `instance_id`,
  `mqtt_base_topic` und `devices_file` nicht gemeinsam vom Standard ab, steht
  beim Start eine Zeile im Protokoll. Wer nur eine Instanz betreibt, kann sie
  ignorieren.
- **Die Weboberfläche sagt, zu welcher Instanz sie gehört.** Läuft eine zweite
  Instanz, beginnt die Statuszeile mit „Instanz Haus 2 · " (Namenspräfix, sonst
  `instance_id`); `/api/status` liefert das Feld `instance`. Bei einer einzigen
  Instanz mit Standardwerten bleibt die Zeile unverändert. Grund: Zwei
  Browser-Tabs sahen gleich aus – ein von Hand getippter Frame wäre in der
  falschen Anlage gelandet.
- **Dokumentation:** neues Kapitel „Zweite Anlage: Add-on mehrfach installieren"
  (`DOCS.md` 4) – Schritt für Schritt vom Kopieren des Ordners bis zu den
  Optionen, die sich je Instanz unterscheiden müssen.

## 1.22.0

- **Befehle haben immer Vorfahrt.** Die Bremse hält jetzt nur noch den eigenen
  Hintergrundverkehr zurück – Abfragerunde, Fähigkeits-Sonden und Bus-Scan. Ein
  Befehl aus Home Assistant wartet nie auf die Bremse, sondern höchstens auf die
  harte Obergrenze von fünf Frames pro Sekunde. Gemessen an der Anlage: Eine
  einzige nicht vorhandene Adresse ließ das Schalten einer echten Lampe
  sekundenlang dauern.
- **Neue Option „Reserve für Befehle“ (`command_reserve`, Standard 1,5
  Frames/s).** So viel vom Bus bleibt für Befehle frei; Abfragen und Scan dürfen
  nur den Rest nutzen. Damit kommt ein Tastendruck auch mitten im Bus-Scan
  durch – träge, aber er kommt durch.
- **Zwei Sender statt einem.** Vordergrund und Hintergrund der Warteschlange
  werden getrennt abgearbeitet. Vorher stand ein Befehl hinter dem Frame, das
  gerade auf die Bremse wartete – die zwei Spuren nützten nichts.
- **Die Bremse misst jetzt Überlast statt Einzelfehler.** Abgelehnte Frames
  lassen sie nur noch greifen, wenn sie von **mehreren verschiedenen Adressen**
  kommen. Eine einzelne nicht vorhandene Adresse kann die Anlage damit nicht
  mehr ausbremsen. Ausbleibende Antworten zählen unverändert – sie sind das
  ehrlichste Zeichen für einen überlasteten Bus.
- **Die Bremse löst sich schnell wieder.** Nach jedem störungsfreien Zeitfenster
  halbiert sich der Mindestabstand. Vorher brauchte es 20 quittierte Frames, um
  aus 2 s herauszukommen – nach dem Scan waren das fünf Minuten Nachlauf.
- **Tote Adressen werden stillgelegt.** Wird eine Adresse `dead_after`-mal
  hintereinander abgelehnt (Standard 3), fragt das Add-on sie nicht mehr ab,
  schreibt eine verständliche Warnung ins Protokoll und zeigt sie in Home
  Assistant als **nicht verfügbar**. Alle zehn Minuten klopft es einmal an;
  meldet sich die Adresse – auch von selbst über den Bus –, ist sie sofort
  wieder normal. **Befehle gehen trotzdem raus**, man darf es jederzeit
  versuchen. Die Weboberfläche zeigt in der Statuszeile, welche Adressen
  stillgelegt sind.
- **Der Bus-Scan blockiert die Bedienung nicht mehr.** Seine Abfragen gelten
  als Hintergrundverkehr und lassen damit die Reserve für Befehle frei –
  Schalten wird während eines Scans träge, funktioniert aber. **Nach dem Scan**
  wird die Hintergrund-Warteschlange geleert (die Abfragen darin sind Minuten
  alt, und der Scan hat ohnehin gerade alles abgefragt); die Abfragerunde
  startet erst 30 s später wieder. Die Weboberfläche schreibt während des Scans
  „Bus-Scan läuft – Schalten kann einige Sekunden dauern“ in die Statuszeile.
- **Die Abfragerunde tröpfelt statt zu schwallen.** Der Takt richtet sich jetzt
  nach `poll_interval` und der Anzahl der Abfragen: Eine Runde verteilt sich
  über die Hälfte des Intervalls (höchstens 5 s je Frame, mindestens 0,5 s).
  Gemessen an der Anlage: 40 Geräte ergeben 68 Abfragen, die vorher als
  34-Sekunden-Schwall mit zwei Frames je Sekunde rausgingen – unabhängig vom
  eingestellten Intervall. Genau das waren die Spitzen „60 gesendet, 180
  empfangen pro Minute“. Passt eine Runde nicht in die Hälfte des Intervalls,
  steht ein Hinweis im Protokoll, der ein längeres Intervall vorschlägt.
- **Ein Hintergrund-Frame ohne Antwort blockiert keinen Befehl mehr lange.**
  Abfragen geben nach 2 s auf statt nach 6 s. Grund: OpenWebNet kennt keine
  Frame-Kennung, deshalb kann eine Kommando-Sitzung immer nur einen Austausch
  gleichzeitig führen – der wartende Befehl stand hinter der stummen Abfrage.

## 1.21.1

- **Basis-Image auf Alpine 3.22 gehoben** (`ghcr.io/home-assistant/base:3.22`).
  Grund: Alpine 3.21 wird von Home Assistant nicht mehr neu gebaut und endet am
  01.11.2026 – künftige Sicherheitsaktualisierungen kämen dort nicht mehr an.
- **Python bleibt bei 3.12** (Alpine 3.22 liefert 3.12.14). Die Paketversionen
  in `requirements.txt` sind unverändert; für beide Architekturen (`amd64`,
  `aarch64`) gibt es weiterhin fertige Pakete, kompiliert wird nichts.
- **Kein Unterschied im Verhalten.** Am Programmcode wurde nichts geändert –
  Bus, MQTT, Weboberfläche und Geräte arbeiten wie in 1.21.0. Beim Aktualisieren
  baut Home Assistant das Add-on einmal neu; das dauert etwas länger als sonst,
  weil das neue Basis-Image geladen wird.

## 1.21.0

- **Farbige DALI-Leuchten (RGBW) und Tunable White.** In der Weboberfläche gibt
  es bei *Licht und Dimmer* zwei neue Häkchen **RGBW** und **TunableW** sowie
  die Felder **Warm (K)** und **Kalt (K)**. Home Assistant zeigt dann das
  Farbrad bzw. den Weißregler; die Helligkeit bleibt davon getrennt und läuft
  wie bisher über den Dimmer.
  - Farbe: `*#1*<Adresse>*#12*Farbton*Sättigung*100##` – Farbton in Grad,
    Sättigung in Prozent.
  - Weißtemperatur: `*#1*<Adresse>*#14*Wert##` mit Wert = 100 000 000 / Kelvin
    (2700 K = 37040). Standardbereich 2000 K bis 6200 K; eigene Befehle werden
    auf den eingestellten Bereich begrenzt.
- **„Einschalten samt Farbe“ schaltet zuerst ein.** Eine ausgeschaltete Leuchte
  ignoriert einen Farbbefehl und geht davon nicht an – gemessen an der Anlage.
- **Eine Leuchte, die von sich aus eine Farbe oder eine Weißtemperatur meldet,
  bekommt die Häkchen selbst** und wird in Home Assistant neu angemeldet, wie
  schon bisher beim Dimmer. Auch der Bus-Scan trägt sie ein.
- Nach einem Neustart fragt das Add-on Farbe und Weißtemperatur **einmal** ab,
  bis die Leuchte geantwortet hat – als Sonde. Eine Ablehnung durch das Gateway
  löst die Bremse nie aus; bleibt die Antwort ganz aus, zählt das wie bei jedem
  anderen Frame. Im Ruhebetrieb entsteht dadurch keine zusätzliche Buslast.
- Die Gerätetabelle der Weboberfläche lässt sich auf schmalen Bildschirmen
  seitlich schieben, statt die Felder zusammenzuquetschen.

## 1.20.0

Erste Version der Neufassung **MyHome SCS Bridge**. Sie ist ein eigenes Add-on
(Slug `myhome_scs_bridge`) und läuft neben der bisherigen *MyHome SCS MQTT
Bridge* 1.17.0, ohne ihr in die Quere zu kommen: eigenes Basis-Topic
`scs_bridge`, eigene `unique_id`s (`scs_bridge_…`), Discovery unter dem eigenen
Knoten `homeassistant/<Typ>/scs_bridge/…`, eigene Gerätedatei und eigene
MQTT-Client-ID. Geräte und Verhalten am Bus entsprechen dem Bestand; neu
geschnitten ist der Code darunter.

- **Die Buslast regelt eine einzige Stelle.** Obergrenze (fünf Frames pro
  Sekunde, Spitzen bis zehn) und Bremse sitzen zusammen im Gateway und gelten
  für alles, was das Add-on sendet – Befehle, Abfragen, Keepalive **und den
  Bus-Scan**, der die Bremse bisher umging. Die strengere Regel gewinnt, die
  Wartezeiten addieren sich nicht.
- **Die Bremse greift erst bei einer Häufung**: Standard sind drei abgelehnte
  oder unbeantwortete Frames innerhalb von drei Sekunden. Eine einzelne nicht
  vorhandene Adresse, die bei jeder Abfrage abgelehnt wird, bremst nicht mehr
  alles aus. Abgelehnte Fähigkeits-Abfragen zählen weiterhin nie. Beide Werte
  sind vorläufig und über die Optionen `bus_brake_threshold` und
  `bus_brake_window` einstellbar. Nach einem Wiederverbinden startet die
  Verbindung ohne Bremse.
- **Abgelehnte und unbeantwortete Frames werden gezählt** – dauerhaft sichtbar
  als Diagnose-Sensoren „Abgelehnte Frames“ und „Unbeantwortete Frames“, in der
  Weboberfläche und unter `/api/status`, zusammen mit der aktuellen Bremse.
- Mit `log_level: debug` steht zu jedem Frame im Protokoll, wie lange er in der
  Warteschlange stand und wie lange es bis zur Antwort dauerte. Jede
  Protokollzeile trägt die Uhrzeit auf die Millisekunde.
- **Weboberfläche** wie gehabt. Sie nimmt nur noch Verbindungen über Home
  Assistants Ingress an,
  prüft Eingaben (ein leerer oder kaputter Speichervorgang leert die
  Gerätedatei nicht mehr) und startet das Add-on nur nach **bestätigtem**
  Speichern neu – bisher hieß es auch bei einem Fehler „Gespeichert“.
- **Speichern und „Speichern & neu starten“ halten den Bus nicht mehr an.**
  Beides lief bisher in der Hauptschleife: Solange die Datei geschrieben wurde
  oder der Supervisor antwortete (bis zu zehn Sekunden), kam kein Frame an.
- **Gleiche Nummer, anderes Gerät:** Aktor 1 zweier Heizzonen (`8#1`, `9#1`)
  und Kontakt 1 neben IR-Schnittstelle 1 (`31`, `41`) werden nicht mehr als
  doppelt verworfen; sie bekommen eigene Topics (`pump/8_1`, `contact/4_1`).
- Eine Entity mit fehlerhaften Einstellungen hält die regelmäßige Abfrage der
  übrigen Geräte nicht mehr auf.
- Home Assistant sieht eine Entity nur noch als verfügbar, wenn das Add-on
  läuft **und** die Verbindung zum Gateway steht.
- Eine fehlerhafte Gerätedatei räumt nichts mehr im Broker weg.
- Nur noch `aarch64` und `amd64`, wie von Home Assistant unterstützt; das
  Basis-Image steht fest im `Dockerfile`.
