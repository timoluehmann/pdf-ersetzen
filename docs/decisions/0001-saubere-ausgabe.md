# 0001 Saubere Ausgabe als Nachbearbeitung beim Speichern

Status: angenommen, 2026-11-24

## Kontext

Bearbeitete PDFs gehen an Kunden. Sie sollen aussehen wie in einem Zug erzeugt. Bisher blieben
Spuren: unterschiedliche Hälften der Dokument-ID, Erzeuger und Änderungsdatum früherer Werkzeuge,
XMP-Historie, Annotationen aus Editoren und vor allem eine zweite Kopie jeder Schrift mit dem
Ressourcennamen `PE<n>`, an dem man dieses Werkzeug erkennt.

## Entscheidung

Neues Modul `bereinigen.py`, aufgerufen aus `bearbeite()` an der Stelle, an der vorher gespeichert
wurde (`speichere(doc, ausgabe, sauber)`). Standard ist an, `--ohne-bereinigung` schaltet ab.

- Nur PyMuPDF, keine neue Abhängigkeit. Ghostscript oder qpdf wären gründlicher beim Neuschreiben,
  fehlen aber auf den Rechnern der Abteilungen (Windows) und würden ein externes Programm
  voraussetzen.
- Schriften werden zusammengeführt statt neu gesetzt: erst Umkodieren auf vorhandene Glyphen des
  Originals, dann bei gleicher Schriftdatei Übernahme des vollständigen Programms. Glyphen gelten
  nur als vorhanden, wenn sie Umrisse haben (TrueType `loca`), weil Subsets ToUnicode-Einträge für
  geleerte Glyphen behalten.
- Jeder Schritt wird geprüft und bei Abweichung zurückgenommen. Pixelgenau, wo sich das
  Schriftprogramm ändert; sonst Zeichen, Position und Tinte je Zeichen, weil ein anderes
  Schriftobjekt im Subpixelbereich anders rastert. Geprüft wird immer an einer frisch geladenen
  Kopie, weil MuPDF Schriftprogramme je Objekt zwischenspeichert.

## Folgen

- Speichern dauert länger (gemessen 0,2 bis 0,3 s auf 0,4 bis 0,5 s je Durchlauf bei 1 bis 4 Seiten).
- Die Endprüfung vergleicht gegen den Stand vor der Bereinigung. Schlägt sie fehl, wird wie
  bisher gespeichert und ein Hinweis ausgegeben, das Ergebnis ist also nie schlechter als vorher.
- Bleibt eine Schriftkopie übrig (Zeichen fehlt im Original und Schriftdatei weicht ab), wird sie
  neutral benannt und gemeldet, nicht still in Kauf genommen.
