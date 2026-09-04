# MiniMax H3 Ref2V Continuity Stream

[English documentation](README.md)

Diese Version verwendet den getesteten MiniMax-H3-Ref2V-Turbo-Workflow mit
vier Schritten. Standardmäßig werden `448 x 448` und 124 Frames verwendet;
Auflösung und Cliplänge lassen sich während des Betriebs ändern.
Charakterbilder bleiben echte
Ref2V-Referenzen. Nach jedem Clip wird der letzte brauchbare Frame extrahiert
und im nächsten Clip standardmäßig ausschließlich als `MiniMaxH3AddGuide` auf
Frame 0 verwendet. Dadurch wird das rekursiv erzeugte Bild nicht zusätzlich als
Ref2V-Referenz verstärkt.

Damit beginnt der Folgeclip tatsächlich am vorherigen Bild, während die
ursprünglichen Charakterbilder weiterhin die Identität steuern.

## Inhalt und Grenzen des ZIP-Pakets

Das ZIP enthält alle kleinen, von diesem Zusatzprojekt benötigten Dateien:

| Datei | Aufgabe |
| --- | --- |
| `stream_h3_r2v_continuity.py` | Generator, Warteschlangen, Control API, Stream und Audiomischer |
| `start_h3_r2v_continuity.ps1` | Starter mit Desktop-/Portable-Erkennung und Custom-Node-Installation |
| `MiniMaxH3_R2V_4step_5s.json` | getestete Ref2V-Workflowvorlage |
| `custom_nodes/h3_r2v_fixed/` | feste Bild-Sockets für zuverlässige API-Prompts |
| `check_sageattention.ps1` | SageAttention-Kompatibilitätstest |
| `README.md` / `README_DE.md` | GitHub-Dokumentation |
| `.gitignore` | schließt private Medien und Laufzeitdateien aus |

Es ist bewusst **kein eigenständiges Komplettpaket**. Nicht enthalten sind:

- Modellgewichte, FFmpeg und FFprobe;
- persönliche Referenzbilder, Musik und generierte Videos;
- die bereits im geschützten Upstream-Repository enthaltenen Dateien
  `submit_h3.py`, `prompts_scenes.txt`, `h3_characters.json`,
  `custom_nodes/h3_fast_writer` und optional `custom_nodes/h3_block_attention`.

Das Zusatzpaket wird deshalb über eine vollständige Kopie von
[`jacokon/fasth3-live`](https://huggingface.co/datasets/jacokon/fasth3-live)
gelegt. Das Upstream-Repository ist zugriffsbeschränkt und nennt Lizenz- sowie
Gebietsbeschränkungen. Prüfe die jeweils aktuellen Bedingungen selbst und
veröffentliche keine Modellgewichte oder fremden Medien mit deinem GitHub-Repo.

Für den eigenen Controller-Code, Custom Node, Workflow und die Dokumentation
ist **GNU General Public License v3.0 only (GPL-3.0-only)** vorgesehen. Wähle
beim Anlegen des GitHub-Repositories deshalb **GNU General Public License
v3.0**. Sie erlaubt Nutzung, Änderung, Weitergabe und kommerzielle Verwendung;
öffentlich weitergegebene Änderungen müssen ebenfalls unter GPL verfügbar
bleiben und den Quellcode enthalten.

Diese Projektlizenz gilt nur für die Dateien, an denen du die Rechte besitzt.
Sie lizenziert MiniMax-Modelle, Upstream-Dateien, Musik, Charakterbilder und
andere Inhalte Dritter nicht neu.

## Installation

Unter Windows erkennt der Starter sowohl die aktuelle Comfy-Desktop-Struktur
als auch ComfyUI Portable automatisch. Comfy Desktop verwendet standardmäßig:

```text
%LOCALAPPDATA%\Comfy-Desktop\
├── ComfyUI-Installs\
│   └── <Installation>\
│       ├── .venv\Scripts\python.exe
│       └── ComfyUI\custom_nodes\
└── ComfyUI-Shared\
    ├── input\
    ├── output\
    └── models\
```

Entpacke das vollständige Upstream-Repository in einen beliebigen
beschreibbaren Ordner. Kopiere anschließend den **Inhalt** dieses Zusatzprojekts
in dessen `fasth3-live`-Ordner. `prompts_scenes.txt` und `character_refs`
werden standardmäßig neben dem Controller-Script gesucht.

Die vorhandenen Dateien des ursprünglichen Repositories müssen dort bleiben:

```text
submit_h3.py
prompts_scenes.txt
h3_characters.json
character_refs\
```

Portable wird weiterhin erkannt, wenn `fasth3-live` direkt neben `ComfyUI` und
`python_embeded` liegt. Bei einem eigenen Installationsort oder mehreren
Desktop-Installationen können die Pfade vor dem Start ausdrücklich gesetzt
werden:

```powershell
$env:COMFYUI_ROOT = "$env:LOCALAPPDATA\Comfy-Desktop\ComfyUI-Installs\<Installation>\ComfyUI"
$env:COMFYUI_DATA_ROOT = "$env:LOCALAPPDATA\Comfy-Desktop\ComfyUI-Shared"
$env:COMFYUI_PYTHON = "$env:LOCALAPPDATA\Comfy-Desktop\ComfyUI-Installs\<Installation>\.venv\Scripts\python.exe"
```

Benötigte Modelle aus deinem funktionierenden Speedtest. Bei der aktuellen
Desktop-Version ist der Basisordner normalerweise
`%LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Shared\models`; Portable verwendet
`ComfyUI\models`:

```text
<ComfyUI-Daten>\models\diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors
<ComfyUI-Daten>\models\text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
<ComfyUI-Daten>\models\vae\minimax_h3_video_vae_fp16.safetensors
<ComfyUI-Daten>\models\vae\minimax_h3_audio_vae_fp32.safetensors
<ComfyUI-Daten>\models\loras\minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors
```

## Erster Start

PowerShell blockiert lokale Skripte auf deinem System. Verwende deshalb:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "C:\Pfad\zu\fasth3-live\start_h3_r2v_continuity.ps1"
```

Beim ersten Aufruf installiert beziehungsweise synchronisiert das Startscript
`H3ReferenceToVideoFixed` sowie – wenn sie im zusammengeführten Ordner
vorhanden sind – die Upstream-Nodes `h3_fast_writer` und
`h3_block_attention`. Danach:

1. ComfyUI vollständig beenden;
2. ComfyUI erneut starten;
3. denselben PowerShell-Befehl erneut ausführen.

Beim Start werden der erkannte ComfyUI-Core-Ordner, der Datenordner und die
verwendete Python-Datei angezeigt. Unterstützt werden die aktuellen
LocalAppData-Pfade von Comfy Desktop, die ältere Desktop-Struktur und Portable.

Anschließend öffnen:

- Steuerung: `http://127.0.0.1:9001`
- Stream in VLC: `http://127.0.0.1:9000`

Die Control UI startet auf Englisch. Oben rechts kann sie sofort auf
**Deutsch** umgestellt werden. Die Auswahl wird gespeichert. Der Schalter
übersetzt die Oberfläche, nicht den Inhalt deiner Promptdatei.

## Character References

Die Dateinamen sind die Aliase:

```text
character_refs\
├── jane.png
└── john.png
```

Wenn ein Prompt `Jane` nennt, wird `jane.png` als `<Picture 1>` gebunden;
`John` wird entsprechend `john.png` zugeordnet.
Mehrere Charaktere werden in der Reihenfolge ihrer ersten Erwähnung nummeriert.
Im Guide-only-Modus bleiben alle neun Bildslots für echte Charakterreferenzen
verfügbar. Wenn eine folgende Szene keine Namen nennt, werden die zuletzt
verwendeten Charakterbilder automatisch als Identitätsanker weitergeführt.

## Automatischer Fortsetzungstext

Ab dem zweiten Clip wird im visuellen Teil jedes Prompts automatisch ergänzt:

```text
Continue seamlessly from the supplied first-frame guide. Begin exactly from
that frame without a cut. Preserve the same framing, character positions,
clothing, lighting, environment, and camera direction.
```

In der Control UI stehen zwei Modi zur Verfügung:

- **Guide only – stabilere Farben**: neuer Standard; das letzte Bild wird nur
  als Frame-0-Guide verwendet.
- **Guide + Picture – stärkste Bildbindung**: bisheriges Verhalten; das letzte
  Bild wird zusätzlich als `<Picture N>` eingebunden. Das bindet den Übergang
  stärker, kann bei langen Loops aber Farbe und Kontrast rekursiv verstärken.

Der Button **Neue Geschichte / Reset** entfernt den Anschlussframe für den
nächsten noch nicht eingereichten Prompt. Dieser Clip startet eine neue Szene;
sein Schlussbild wird anschließend wieder zum Anfang der nächsten Fortsetzung.

Die Fortsetzung gilt auch für zufällig ausgewählte fertige Szenen aus
`prompts_scenes.txt`. Deren `{NAME}`-Platzhalter werden bei laufender Geschichte
nicht mehr mit neuen zufälligen Identitäten in Konflikt gebracht. Stattdessen
werden sie zu Rollen wie
`the same continuing person shown in the supplied first-frame guide`.
Außerdem steht die Anschlussanweisung am Anfang der visuellen Beschreibung,
damit sie auch bei längeren Vorlagen nicht vom neuen Szenentext überstimmt wird.

## Manuelle Prompts

Manuelle Prompts können weiterhin als nächster Clip oder ans Ende der
Warteschlange gesetzt, bearbeitet, verschoben und gelöscht werden. Bei kurzen
Clips sollten die Beschreibungen eine einzelne kurze Aktion enthalten. Das
Script entfernt vor dem Absenden automatisch den ersten außerhalb der aktuell
gewählten Cliplänge beginnenden Shot und alle späteren Shots. Die vorhandene
`prompts_scenes.txt` kann unverändert weiterverwendet werden; Soundscape und
Musikfelder werden entsprechend der Audio-Einstellung behandelt.

Beispiel:

```text
Jane picks up the letter, reads the first line and looks toward the closed door.
```

Das Script ergänzt die H3-Audiofelder, die passende Charakterreferenz und –
sofern vorhanden – den Fortsetzungstext selbst.

### Prompt wiederholen

Beim Einfügen und bei jedem vorhandenen Queue-Eintrag gibt es eine
**Repeat**-Checkbox. Ein Repeat-Prompt wird nach der Entnahme wieder ans Ende
der manuellen Warteschlange gestellt. Dadurch werden mehrere Repeat-Prompts
abwechselnd abgespielt; ein einzelner läuft unbegrenzt weiter. Zum Beenden den
Eintrag löschen oder Repeat deaktivieren und speichern. Eine bereits an
ComfyUI übermittelte Wiederholung kann nicht mehr zurückgezogen werden.

## Szenendatei und Aktualisieren

Für automatische Szenen stehen zwei Modi zur Verfügung:

- **Zufällig** zieht bei jedem Job einen zufälligen Block aus der Textdatei.
- **In Reihenfolge** beginnt beim ersten Block, läuft fortlaufend weiter und
  springt am Ende wieder zum Anfang.

Szenenblöcke werden in der Datei durch eine Zeile mit `---` getrennt. Manuelle
Prompts haben immer Vorrang. Der Button **Prompts aktualisieren** prüft den
eingetragenen Pfad erneut und setzt im Reihenfolgemodus den Cursor auf die erste
Szene zurück.

Der Button **Referenzen aktualisieren** liest den aktuell eingetragenen
`character_refs`-Ordner sofort neu ein. Das Script scannt die Referenzen ohnehin
vor jedem neuen Job; der Button liefert zusätzlich eine direkte Bestätigung und
aktualisiert die Anzeige.

## Dynamische Cliplänge

Die native Generierungsdauer kann während des Betriebs ungefähr zwischen 0,92
und 15,08 Sekunden eingestellt werden. H3 akzeptiert Framezahlen der Form
`17k + 5`; deshalb rundet der Controller die Eingabe auf die nächste gültige
Framezahl und zeigt das exakte Ergebnis an.

Bei der standardmäßigen Wiedergabe mit 14 fps ergeben sich beispielsweise:

| Eingabe | H3-Frames | Native Dauer | Wiedergabedauer |
| ---: | ---: | ---: | ---: |
| 5 s | 124 | 5,17 s | 8,86 s |
| 10 s | 243 | 10,13 s | 17,36 s |
| 15 s | 362 | 15,08 s | 25,86 s |

Jeder bereits eingereichte Job behält seine eigene Framezahl. Dadurch können
im Puffer Clips unterschiedlicher Länge liegen: Wiedergabe, Audio-Retiming,
Zeitstempel und Geschwindigkeitsstatistik werden für jeden Clip separat
berechnet. Die Änderung gilt ab dem nächsten noch nicht eingereichten Job.

## Geschwindigkeit

Der Standard ist `14 fps`. 124 Frames laufen damit 8,86 Sekunden und passen zu
deinen gemessenen 8,1–8,8 Sekunden Erzeugungszeit. Für 18 fps:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\start_h3_r2v_continuity.ps1" --fps 18 --prefill 6
```

18 fps verbraucht bei deinen bisherigen Messwerten langsam den Vorpuffer;
14 fps ist der konservative Dauerbetrieb.

## Sauberes, durchgehendes Musikbett

H3 erzeugt den Ton jedes Clips unabhängig. Außerdem wird dieser Ton beim
Standardstream von 24 auf 14 fps auf rund 58 Prozent Tempo gedehnt.
Das ist für Dialog und Geräusche meist brauchbar, kann generierte Musik aber
deutlich verstimmen und an jeder Clipgrenze neu beginnen lassen.

Die Control UI enthält deshalb den Bereich **Audio & Musikbett**:

- **Durchgehende externe Musik zumischen** aktiviert eine lokale Musikquelle;
- **Musikdatei oder Musikordner** akzeptiert MP3, WAV, FLAC, M4A, AAC, OGG
  und Opus;
- **Ordner-Wiedergabe** spielt alphabetisch nach Dateiname oder in zufälligen
  Zyklen; zufällige Zyklen vermeiden nach Möglichkeit eine direkte Wiederholung;
- **Musiklautstärke** und **H3-Tonlautstärke** sind live einstellbar;
- **Ducking** senkt das Musikbett ab, während H3-Ton hörbar ist;
- **H3-Musik unterdrücken** ersetzt im ausgehenden Prompt ausschließlich
  `non_diegetic_music` durch `N/A`. Dialog, Umgebung und Effekte unter
  `overall_soundscape` bleiben erhalten.

Empfohlener Startwert:

```text
Externe Musik:          an
Musiklautstärke:        0.20
H3-Tonlautstärke:       1.00
Ducking:                an bei Dialog, sonst aus
H3-Musik unterdrücken:  an
```

Eine einzelne Musikdatei wird geloopt. Ein Ordner wird als zustandsbehaftete
Playlist abgespielt. Position und Titel bleiben über Videoclipgrenzen erhalten;
endet ein Titel mitten in einem Video, wird der nächste Titel bereits innerhalb
dieses Clips angefügt. Änderungen an Pfad, Reihenfolge oder Lautstärke wirken
beim nächsten ausgespielten Clip, ohne den Stream neu zu starten. An der
AAC-Clipgrenze kann ein sehr kleiner Encoderübergang bleiben, musikalisch laufen
Position und Tempo aber kontinuierlich weiter.

Standardmäßig zeigt das Pfadfeld auf `music.mp3` neben dem Python-Script. Du
kannst dort eine Datei ablegen oder in der UI den absoluten Windows-Pfad zu
einer Datei beziehungsweise einem Ordner eintragen. Unterordner werden nicht
rekursiv durchsucht. Wenn die Quelle fehlt oder leer ist, läuft der Stream mit
H3-Ton weiter und zeigt den Fehler in der UI an.

## Adaptive Qualität

Die Steuerungsoberfläche enthält einen Schalter **Adaptive Qualität**. Im
Standardbetrieb rendert der Controller mit `448 x 448`, solange der Puffer
knapp ist. Sobald mehr als drei fertige Clips gepuffert sind, wechselt er auf
`800 x 800`. Fällt der Puffer auf drei Clips oder weniger, schaltet er zurück.
Damit wird die teure 800er-Stufe nur aus vorhandenem Zeitvorrat bezahlt.

Alle Werte lassen sich während des Betriebs ändern:

- schnelle und HQ-Auflösung;
- Pufferstand für HQ an;
- Pufferstand für HQ aus;
- adaptive Regelung an/aus.

Die Änderung wirkt auf den nächsten noch nicht eingereichten Prompt. Bereits
in ComfyUI befindliche Jobs behalten ihre Auflösung. Im Terminal erscheint bei
jedem Moduswechsel beispielsweise:

```text
adaptive quality: HQ 800x800 (buffer=4, thresholds 3/4)
```

Der MPEG-TS-Stream verwendet eine feste Canvas-Größe, die beim Programmstart
aus der größeren konfigurierten Auflösung gewählt wird. Dadurch muss VLC nicht
mitten im Stream zwischen verschiedenen H.264-Auflösungen wechseln. Änderungen
an der Stream-Canvas werden deshalb erst nach einem Neustart übernommen;
Änderungen an der Renderauflösung wirken live. Die Standard-Bitrate wurde für
die 800er-Canvas auf `4M` angehoben; sie kann weiterhin mit `--vbitrate`
überschrieben werden.

Der sinnvolle Ausgangspunkt ist:

```text
Schnell: 448 x 448
HQ:      800 x 800
HQ an:   4 Clips
HQ aus:  3 Clips
```

Wenn HQ den Puffer regelmäßig leert, zuerst die Wiedergabe mit `--fps 12`
starten oder HQ auf `448 x 448` zurückstellen. `ref_image_size=max` bleibt eine
separate, deutlich teurere Option und wird von der adaptiven Auflösung nicht
automatisch aktiviert.

## Zuschauererkennung und Vorratspuffer

Beim lokalen HTTP-Stream zählt der Controller die tatsächlich mit Port `9000`
verbundenen VLC-/Browser-Clients. Die Control UI auf Port `9001` zählt nicht als
Zuschauer. Sobald niemand mehr zusieht, wird nach dem gerade laufenden Clip die
Wiedergabe angehalten. Die Generierung läuft weiter und füllt den Dateipuffer
bis zum Limit `--queue-max` (standardmäßig acht Clips). Dadurch geht keine
Rechenzeit für ungesehene Wiedergabe verloren.

Sobald sich wieder ein Zuschauer verbindet, beginnt die Wiedergabe mit dem
ältesten gepufferten Clip. Die fortlaufende Geschichte und Prompt-Reihenfolge
bleiben erhalten. In der Control UI werden Zuschauerzahl, Pausenzustand und
Pufferstand angezeigt. Diese Erkennung ist nur für den eingebauten HTTP-Stream
verfügbar; bei einem UDP-Ziel kann der Sender nicht feststellen, ob jemand
empfängt.

Mit aktiver Fortsetzung wird absichtlich nur ein Prompt gleichzeitig an
ComfyUI gesendet. Der nächste Graph kann erst gebaut werden, nachdem der letzte
Frame des vorherigen Clips existiert. Wird Fortsetzung ausgeschaltet, gilt
wieder der Wert von `--pipeline`.

## LoRAs und Attention

Die Ref2V-Turbo-LoRA mit Stärke `1.0` ist bereits im Workflow enthalten und
aktiv. Füge dieselbe Turbo-LoRA nicht noch einmal in der Control UI hinzu.
Zusätzliche Model-only-LoRAs können weiterhin live gewählt und gewichtet
werden.

Wenn `custom_nodes/h3_block_attention` installiert ist, setzt das Script den
Attention-Node auch in den Ref2V-Graph ein. `auto` bevorzugt SageAttention,
danach Comfy Kitchen und schließlich PyTorch. Es wird nur ein Backend gesendet,
das ComfyUI über `/object_info` tatsächlich anbietet.

## Warum ein zusätzlicher Fixed-Node enthalten ist

Bestimmte ComfyUI-Versionen akzeptieren die dynamischen `ref_images`-Eingänge
des nativen Nodes über die HTTP-API, ignorieren die Bilder aber intern ohne
Fehlermeldung. Der kleine Fixed-Node stellt neun normale Bildanschlüsse bereit
und führt dieselbe Bild-Referenzkodierung aus. Dadurch wirken die Referenzen im
automatisierten Stream genauso wie im interaktiven Workflow.
