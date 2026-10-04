<div align="center">

<img src="icon.svg" width="96">

# omarchy-haltung

**Gerade sitzen beim Zocken: Deine AirPods Pro messen die Kopfneigung, ein Panel auf dem zweiten Monitor zeigt sie an.**
Für [Omarchy](https://omarchy.org) / Hyprland mit dem librepods-Daemon aus [omapods](https://github.com/thisisgm/omarchy-pods).

[![Omarchy](https://img.shields.io/badge/Omarchy-Hyprland-1793d1?style=for-the-badge&logo=archlinux&logoColor=white)](https://omarchy.org)
[![Python](https://img.shields.io/badge/Python-GTK4-3776ab?style=for-the-badge&logo=python&logoColor=white)](#voraussetzungen)
[![Lizenz: MIT](https://img.shields.io/badge/Lizenz-MIT-green?style=for-the-badge)](LICENSE)
[![English](https://img.shields.io/badge/read_me-English-black?style=for-the-badge)](README.md)

</div>

---

![Panel](screenshot.png)

## Warum?

AirPods Pro haben Beschleunigungs- und Drehsensoren für Apples 3D-Audio. Unter Linux liest die niemand aus,
sie lassen sich aber über Bluetooth abfragen. Aus der Schwerkraft ergibt sich genau, wie weit dein Kopf nach vorn
kippt, ohne Drift und ohne Kamera. Das ist der typische Zock-Buckel.

## Was es zeigt

- 🧑 **3D-Kopf**, der mit deinem nickt und kippt (flüssig während der Bewegung, im Stillstand wird nichts gezeichnet)
- 📐 **Pitch** (vor / zurück) und **Roll** (seitlich) gegenüber deiner kalibrierten aufrechten Haltung
- 📈 **Plots:** die letzten 2 Minuten mit 25 Hz und die letzten 30 Minuten als 10-s-Mittel, Schwellwerte gestrichelt
- 📊 **Werte:** Mittel der letzten 60 s, Anteil der Sitzung über 10°, wie lange du schon drüber bist
- 🔊 **Piept wie ein Einparksensor:** ein weicher Ton, langsam ab 6°, je weiter du dich vorbeugst desto schneller, am schnellsten ab 18°, beim Geraderichten wieder langsamer (`haltung --mute` schaltet es um)
- 🟢🟡🔴 **Status:** gelb ab 10° Pitch, nach 15 s am Stück rot **SITZ GERADE**

Nur Pitch zählt als krumm: Ein Blick auf den zweiten Monitor oder seitliches Kopfkippen löst nichts aus.

## Installieren

```bash
git clone https://github.com/vsvito420/omarchy-haltung
cd omarchy-haltung
./install.sh
```

`install.sh` patcht und baut den librepods-Daemon einmal neu (siehe unten), installiert das Panel nach
`~/.local/share/haltung/` und startet es mit der grafischen Sitzung (`haltung.service`).
Entfernen mit `./uninstall.sh`.

### Voraussetzungen

- AirPods Pro 2 (oder ein anderes Modell mit Head-Tracking), verbunden
- Das Plugin [omapods](https://github.com/thisisgm/omarchy-pods) mit seinem librepods-Daemon
- `python-gobject` und `gtk4-layer-shell` (`omarchy pkg add python-gobject gtk4-layer-shell`)

## Kalibrieren

- **Linksklick** (oder `haltung --calibrate`): gerade hinsetzen, geradeaus schauen. 5 s, fertig.
- **Rechtsklick:** dasselbe, plus die Kopfachse neu lernen: 3 s nach **unten** schauen, dann 3 s nach **oben**.
  Beim ersten Kalibrieren passiert das automatisch. Danach bleibt die Achse gespeichert, sie hängt nur davon ab, wie die AirPods sitzen.

Ein großer Bildschirm mit einem Balken pro Schritt führt durch, dazu ein weicher Ton pro Schritt und zwei steigende Töne, wenn es fertig ist.
Du musst also nicht aufs Panel schauen. Aus dem Blick nach unten und oben ergibt sich die Ohr-zu-Ohr-Achse, und damit lassen sich Pitch und Roll trennen.

## Einstellungen

`~/.config/haltung/config.json`, danach `systemctl --user restart haltung`:

| Schlüssel | Standard | |
|---|---|---|
| `monitor` | `DP-1` | Anschlussname des Monitors (`hyprctl monitors`) |
| `height` | `380` | Panelhöhe in px, wird freigehalten, damit Fenster es nicht verdecken |
| `position` | `top` | `top` oder `bottom` |
| `warn_deg` / `bad_deg` | `10` / `18` | Pitch-Schwellwerte |
| `alert_after_s` | `15` | Sekunden über `warn_deg` bis zum roten Alarm |
| `beep` | `true` | Piepen wie ein Einparksensor (umschalten mit `haltung --mute`) |
| `beep_near_deg` | `4` | so viele Grad unter `warn_deg` fängt es an (am schnellsten bei `bad_deg`) |
| `beep_volume` | `0.25` | 0 … 1 |

## So funktioniert's

**Code:** [`haltung.py`](haltung.py), [`head3d.py`](head3d.py), [`librepods-headtrack.patch`](librepods-headtrack.patch)

- AirPods sprechen AAP über Bluetooth-L2CAP (PSM `0x1001`). Opcode `0x17` startet das Head-Tracking
  (Pakete aus [LibrePods](https://github.com/kavishdevar/librepods)). Danach kommt 25-mal pro Sekunde ein 81-Byte-Frame.
- Der Beschleunigungssensor steht in Byte 67/69/71 (int16, 1024 = 1 g). Das Panel glättet ihn (τ 0,5 s) und misst den
  Winkel zum kalibrierten Vektor.
- Die AirPods streamen **nur an die erste AAP-Verbindung**, und die hält librepods. Ein zweiter Client bekommt nichts.
  Deshalb ergänzt der Patch librepods' Steuer-Socket um das Kommando `headtrack`: Wer es schickt, bekommt jeden
  Frame als Hex-Zeile, bis er auflegt. Der Stream läuft nur, solange jemand zuhört.
- Das Panel ist eine GTK4-Layer-Shell-Fläche mit reserviertem Bereich, also kein Fenster. Es nimmt nie den Fokus
  und wird nicht verdeckt.
- Fürs Zocken gebaut: `SCHED_IDLE` + nice 19, Cairo-Software-Rendering (keine GPU), Werte mit 5 Hz,
  der 3D-Kopf (~350 flach schattierte Flächen) mit 30 fps nur, solange er sich bewegt.

> [!WARNING]
> Der Patch ändert Dateien des omapods-Plugins. Nach einem Update des Plugins `./install.sh` erneut ausführen.
> Verweigert das Plugin-Update wegen lokaler Änderungen, vorher
> `git -C ~/.config/omarchy/plugins/io.github.thisisgm.omapods checkout daemon/` ausführen, updaten, neu installieren.

> [!NOTE]
> Gemessen wird die Neigung des Kopfes. Wer mit geradem Kopf im Stuhl nach vorn rutscht, wird nicht erkannt.

`tools/probe.py` ist die rohe Protokoll-Sonde, mit der das Frame-Layout gefunden wurde (librepods muss dafür gestoppt sein).

Der Patch steht wie librepods unter GPL-3.0, alles andere unter MIT.
