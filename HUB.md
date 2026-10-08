# Haltung per AirPods

<img src="https://raw.githubusercontent.com/vsvito420/omarchy-haltung/main/icon.svg" width="72" alt="Icon Haltung">

AirPods Pro haben Bewegungssensoren für Apples 3D-Audio – unter Linux liest die nur niemand aus.
`haltung` holt sich die Schwerkraft-Werte über librepods und zeigt auf dem Hochkant-Monitor, wie weit der Kopf
nach vorn kippt. Gebaut, um nebenbei CS2 zu zocken, ohne dass es Leistung kostet.

![Panel](https://raw.githubusercontent.com/vsvito420/omarchy-haltung/main/screenshot.png)

## Was es zeigt

- **3D-Kopf** – nickt und kippt mit, flüssig während der Bewegung, im Stillstand wird nichts gezeichnet
- **Pitch** (vor/zurück) und **Roll** (seitlich) gegenüber der kalibrierten aufrechten Haltung
- **Plots**
  - letzte 2 Minuten mit 25 Hz
  - letzte 30 Minuten als 10-s-Mittel
  - Schwellwerte 10° / 18° gestrichelt
- **Werte** – Mittel der letzten 60 s, Anteil der Sitzung über 10°, wie lange schon drüber
- **Status** – grün, gelb ab 10° Pitch, nach 15 s am Stück rot **SITZ GERADE**
- nur Pitch zählt: Blick auf den zweiten Monitor oder seitliches Kippen löst nichts aus

## Bedienung

- **Kalibrieren:** aufs Panel klicken oder `haltung --calibrate` (gut für eine Tastenkombination)
  1. aufrecht hinsetzen, 3 s Countdown
  2. 2 s geradeaus schauen
  3. auf Aufforderung deutlich nach **unten**, dann nach **oben** schauen
- **Einstellungen:** `~/.config/haltung/config.json` – Monitor, Höhe, oben/unten, Schwellwerte, Alarm-Verzögerung

## Installieren

```bash
git clone https://github.com/vsvito420/omarchy-haltung.git
cd omarchy-haltung
./install.sh
```

- braucht das [omapods](https://github.com/thisisgm/omarchy-pods)-Plugin (librepods-Daemon) und `gtk4-layer-shell`
- `install.sh` patcht librepods einmal, installiert nach `~/.local/share/haltung/` und startet `haltung.service`
- entfernen mit `./uninstall.sh`

## So funktioniert's

- **Code:** [`haltung.py`](https://github.com/vsvito420/omarchy-haltung/blob/main/haltung.py), [`head3d.py`](https://github.com/vsvito420/omarchy-haltung/blob/main/head3d.py), [`librepods-headtrack.patch`](https://github.com/vsvito420/omarchy-haltung/blob/main/librepods-headtrack.patch)
- **Protokoll**
  - AirPods sprechen AAP über Bluetooth-L2CAP (PSM `0x1001`)
  - Opcode `0x17` startet Head-Tracking (Pakete aus [LibrePods](https://github.com/kavishdevar/librepods))
  - danach 25 Frames/s à 81 Byte, Beschleunigung in Byte 67/69/71 (int16, 1024 = 1 g)
- **Warum ein Patch**
  - die AirPods streamen nur an die *erste* AAP-Verbindung – und die hält librepods
  - der Patch ergänzt dessen Steuer-Socket um `headtrack`: jeder Frame als Hex-Zeile, bis der Client auflegt
  - der Stream läuft nur, solange jemand zuhört
- **Mathe**
  - Schwerkraft geglättet (τ 0,5 s), Winkel zum kalibrierten Vektor
  - beim Nicken läuft die Schwerkraft durch eine Ebene → deren Senkrechte ist die Ohr-zu-Ohr-Achse → Pitch und Roll getrennt
- **Fürs Zocken gebaut**
  - GTK4-Layer-Shell mit reserviertem Bereich: kein Fenster, nimmt nie den Fokus
  - `SCHED_IDLE` + nice 19, Cairo-Software-Rendering (keine GPU)
  - Werte mit 5 Hz, 3D-Kopf (~350 Flächen) mit 30 fps nur während der Bewegung

<div class="callout warning" markdown="1">
Der Patch ändert Dateien im omapods-Plugin. Nach einem Plugin-Update `./install.sh` erneut ausführen –
verweigert das Update wegen lokaler Änderungen, vorher `git checkout daemon/` im Plugin-Ordner.
</div>

<div class="callout tip" markdown="1">
Gemessen wird die **Kopfneigung**. Wer mit geradem Kopf im Stuhl nach vorn rutscht, wird nicht erkannt.
</div>

## Siehe auch

- [[Omarchy]]
- [[AirPods unter Omarchy]], [[Jitter-Widget]]
