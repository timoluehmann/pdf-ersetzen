"""Ersetzt einzelne Textstellen in maschinell erzeugten PDFs, ohne das Layout anzufassen.

Prinzip: Es wird nichts neu gesetzt. Nur die Zeichen der Fundstelle werden entfernt,
der neue Text kommt an dieselbe Grundlinie, in derselben Schrift, Größe und Farbe.
Alles andere im PDF bleibt unverändert.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

def _schrift_ordner() -> list[Path]:
    """Übliche Schriftordner unter macOS, Windows und Linux. Fehlende Ordner werden übersprungen."""
    heim = Path.home()
    ordner = [
        Path("/System/Library/Fonts"),  # macOS
        Path("/Library/Fonts"),
        heim / "Library/Fonts",
        Path("/usr/share/fonts"),  # Linux
        Path("/usr/local/share/fonts"),
        heim / ".local/share/fonts",
        heim / ".fonts",
    ]
    if windir := os.environ.get("WINDIR"):  # Windows
        ordner.append(Path(windir) / "Fonts")
    if lokal := os.environ.get("LOCALAPPDATA"):
        ordner.append(Path(lokal) / "Microsoft/Windows/Fonts")
    return ordner


SCHRIFT_ORDNER = _schrift_ordner()
MIN_STAUCHUNG = 0.80  # darunter wird gewarnt, Text wird trotzdem eingepasst
# Metrisch gleiche TrueType-Schriften mit vollem Unicode (u. a. €). Kandidaten je System:
# macOS, Windows (Kurznamen), Linux (Liberation, gleiche Laufweiten wie Arial/Times/Courier).
TRUETYPE_GLEICH = {
    "helv": {
        (0, 0): ["Arial.ttf", "arial.ttf", "LiberationSans-Regular.ttf"],
        (1, 0): ["Arial Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf"],
        (0, 1): ["Arial Italic.ttf", "ariali.ttf", "LiberationSans-Italic.ttf"],
        (1, 1): ["Arial Bold Italic.ttf", "arialbi.ttf", "LiberationSans-BoldItalic.ttf"],
    },
    "tiro": {
        (0, 0): ["Times New Roman.ttf", "times.ttf", "LiberationSerif-Regular.ttf"],
        (1, 0): ["Times New Roman Bold.ttf", "timesbd.ttf", "LiberationSerif-Bold.ttf"],
        (0, 1): ["Times New Roman Italic.ttf", "timesi.ttf", "LiberationSerif-Italic.ttf"],
        (1, 1): ["Times New Roman Bold Italic.ttf", "timesbi.ttf", "LiberationSerif-BoldItalic.ttf"],
    },
    "cour": {
        (0, 0): ["Courier New.ttf", "cour.ttf", "LiberationMono-Regular.ttf"],
        (1, 0): ["Courier New Bold.ttf", "courbd.ttf", "LiberationMono-Bold.ttf"],
        (0, 1): ["Courier New Italic.ttf", "couri.ttf", "LiberationMono-Italic.ttf"],
        (1, 1): ["Courier New Bold Italic.ttf", "courbi.ttf", "LiberationMono-BoldItalic.ttf"],
    },
}


# ---------------------------------------------------------------- Regeln


@dataclass
class Regel:
    alt: str
    neu: str
    ausrichtung: str = "auto"  # auto | links | rechts
    # Leer = jedes Vorkommen im Dokument. Sonst nur Treffer, deren Fläche einen dieser
    # Punkte (Seite, x, y in PDF-Punkten) enthält.
    stellen: list[tuple[int, float, float]] = field(default_factory=list)

    def trifft(self, nr: int, rect: pymupdf.Rect) -> bool:
        if not self.stellen:
            return True
        groesser = pymupdf.Rect(rect.x0 - 1, rect.y0 - 1, rect.x1 + 1, rect.y1 + 1)
        return any(s == nr and groesser.contains(pymupdf.Point(x, y)) for s, x, y in self.stellen)

    def als_text(self) -> str:
        teile = [f"{self.alt} => {self.neu}"]
        if self.ausrichtung != "auto":
            teile.append(self.ausrichtung)
        if self.stellen:
            teile.append(" ".join(f"@{s}:{x:.1f}:{y:.1f}" for s, x, y in self.stellen))
        return " | ".join(teile)


STELLE = re.compile(r"@(\d+):(-?[\d.]+):(-?[\d.]+)")


def lies_regeln(regeln_cli: list[str], datei: Path | None) -> list[Regel]:
    """Format: `alt => neu`, optional `| rechts` und `| @Seite:x:y` (nur diese Stelle).
    `#` leitet Kommentare ein."""
    zeilen = list(regeln_cli)
    if datei:
        zeilen += datei.read_text(encoding="utf-8").splitlines()
    regeln = []
    for nr, zeile in enumerate(zeilen, 1):
        if not zeile.strip() or zeile.lstrip().startswith("#"):
            continue
        if "=>" not in zeile:
            raise SystemExit(f"Regel {nr} ohne '=>': {zeile!r}")
        alt, rest = zeile.split("=>", 1)
        ausrichtung, stellen = "auto", []
        # Zusätze von hinten abtrennen, solange sie bekannt sind
        while "|" in rest:
            vorne, _, zusatz = rest.rpartition("|")
            zusatz = zusatz.strip()
            if zusatz in ("links", "rechts", "auto"):
                ausrichtung = zusatz
            elif zusatz and all(STELLE.fullmatch(t) for t in zusatz.split()):
                stellen = [(int(a), float(b), float(c)) for a, b, c in STELLE.findall(zusatz)] + stellen
            else:
                break
            rest = vorne
        alt, neu = alt.strip(), rest.strip()
        if not alt:
            raise SystemExit(f"Regel {nr}: Suchtext ist leer")
        regeln.append(Regel(alt, neu, ausrichtung, stellen))
    if not regeln:
        raise SystemExit("Keine Regeln angegeben (-r oder -f).")
    return regeln


# ---------------------------------------------------------------- Textanalyse


@dataclass
class Zeichen:
    c: str
    bbox: pymupdf.Rect
    origin: pymupdf.Point
    span: dict


@dataclass
class Fundstelle:
    regel: Regel
    seite: int
    zeichen: list[Zeichen]
    zeile: list[Zeichen]
    links_frei: float = 0.0
    rechts_frei: float = 0.0
    ausrichtung: str = "links"
    notizen: list[str] = field(default_factory=list)
    ersatz: str = ""  # tatsächlich zu setzender Text (neu + mitgezogener Rest der Zeile)
    mit: list["Fundstelle"] = field(default_factory=list)  # im selben Lauf mit ersetzt
    neu_rect: pymupdf.Rect | None = None  # Fläche des eingesetzten Texts

    def __post_init__(self):
        self.ersatz = self.regel.neu

    @property
    def alt_text(self) -> str:
        return "".join(z.c for z in self.zeichen)

    @property
    def rect(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.zeichen[0].bbox)
        for z in self.zeichen[1:]:
            r |= z.bbox
        return r

    @property
    def span(self) -> dict:
        return self.zeichen[0].span


def zeilen_der_seite(seite: pymupdf.Page) -> list[list[Zeichen]]:
    zeilen = []
    for block in seite.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            if abs(line["dir"][1]) > 1e-3:  # gedrehter Text wird nicht angefasst
                continue
            zeile = [
                Zeichen(ch["c"], pymupdf.Rect(ch["bbox"]), pymupdf.Point(ch["origin"]), span)
                for span in line["spans"]
                for ch in span["chars"]
            ]
            if zeile:
                zeilen.append(zeile)
    return zeilen


def ist_wortzeichen(c: str) -> bool:
    return c.isalnum()


# PDF-Erzeuger schreiben oft Sonderformen statt normaler Zeichen. Abbildung 1:1,
# damit Indizes im Text weiter den Zeichenpositionen entsprechen.
NORMALISIERUNG = str.maketrans({
    "\xa0": " ", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "\xad": "-", "‐": "-", "‑": "-", "−": "-",
})


def norm(text: str) -> str:
    return text.translate(NORMALISIERUNG)


def finde(seite: pymupdf.Page, nr: int, regeln: list[Regel], teilstring: bool) -> list[Fundstelle]:
    zeilen = zeilen_der_seite(seite)
    funde = []
    # Regeln für eine bestimmte Stelle gehen vor, damit eine "überall"-Regel sie nicht verdeckt
    for regel in sorted(regeln, key=lambda r: not r.stellen):
        if regel.stellen and not any(s == nr for s, _, _ in regel.stellen):
            continue
        alt = norm(regel.alt)
        for zeile in zeilen:
            text = norm("".join(z.c for z in zeile))
            start = text.find(alt)
            while start != -1:
                ende = start + len(alt)
                grenze_ok = teilstring or (
                    (start == 0 or not (ist_wortzeichen(text[start - 1]) and ist_wortzeichen(regel.alt[0])))
                    and (ende == len(text) or not (ist_wortzeichen(text[ende]) and ist_wortzeichen(regel.alt[-1])))
                )
                if grenze_ok:
                    f = Fundstelle(regel, nr, zeile[start:ende], zeile)
                    if regel.trifft(nr, f.rect):
                        funde.append(f)
                start = text.find(alt, start + 1)
    # Überlappende Treffer verschiedener Regeln: der zuerst gefundene gewinnt
    belegt: list[pymupdf.Rect] = []
    eindeutig = []
    for f in funde:
        if any(f.rect.intersects(r) and (f.rect & r).width > 0.5 for r in belegt):
            continue
        belegt.append(f.rect)
        eindeutig.append(f)
    for f in eindeutig:
        bestimme_ausrichtung(f, zeilen)
    eindeutig.sort(key=lambda f: (f.zeichen[0].origin.y, f.zeichen[0].origin.x))
    aufgenommen: set[int] = set()
    ergebnis = []
    for f in eindeutig:
        if id(f) in aufgenommen:
            continue
        if f.ausrichtung == "links":
            ziehe_mit(f, eindeutig, aufgenommen)
        bestimme_platz(f, zeilen, seite.rect.width)
        ergebnis.append(f)
    return ergebnis


def gleicher_stil(a: dict, b: dict) -> bool:
    return a["font"] == b["font"] and abs(a["size"] - b["size"]) < 0.1 and a["color"] == b["color"]


def ziehe_mit(f: Fundstelle, alle: list[Fundstelle], aufgenommen: set[int]) -> None:
    """Zieht den Rest des zusammenhängenden Textlaufs hinter der Fundstelle mit, damit
    kürzerer oder längerer Text keine Lücke bzw. Überlappung im Satz erzeugt.
    Der Lauf endet an einer Spaltenlücke, einem doppelten Leerzeichen oder Stilwechsel."""
    zeile = f.zeile
    starts = {id(g.zeichen[0]): g for g in alle if g is not f and g.zeile is zeile}
    i = next(k for k, z in enumerate(zeile) if z is f.zeichen[-1]) + 1
    groesse = f.span["size"]
    vorher = f.zeichen[-1]
    neu_zeichen, neu_text = [], ""
    while i < len(zeile):
        z = zeile[i]
        if not gleicher_stil(z.span, f.zeichen[-1].span) or z.bbox.x0 - vorher.bbox.x1 > groesse * 0.4:
            break
        if z.c.isspace() and i + 1 < len(zeile) and zeile[i + 1].c.isspace():
            break
        g = starts.get(id(z))
        if g:
            neu_zeichen += g.zeichen
            neu_text += g.regel.neu
            f.mit.append(g)
            aufgenommen.add(id(g))
            i += len(g.zeichen)
            vorher = g.zeichen[-1]
            continue
        neu_zeichen.append(z)
        neu_text += "-" if z.c == "\xad" else z.c
        vorher = z
        i += 1
    while neu_zeichen and neu_zeichen[-1].c.isspace():
        neu_zeichen.pop()
        neu_text = neu_text[:-1]
    f.zeichen = f.zeichen + neu_zeichen
    f.ersatz = f.regel.neu + neu_text


def bestimme_platz(f: Fundstelle, zeilen: list[list[Zeichen]], breite: float) -> None:
    """Freier Platz links/rechts auf derselben Grundlinie."""
    r = f.rect
    y = f.zeichen[0].origin.y
    groesse = f.span["size"]
    eigene = {id(z) for z in f.zeichen}
    links, rechts = None, None
    for zeile in zeilen:
        for z in zeile:
            if id(z) in eigene or not z.c.strip() or abs(z.origin.y - y) > groesse * 0.6:
                continue
            if z.bbox.x0 >= r.x1 - 0.5:
                rechts = z.bbox.x0 if rechts is None else min(rechts, z.bbox.x0)
            elif z.bbox.x1 <= r.x0 + 0.5:
                links = z.bbox.x1 if links is None else max(links, z.bbox.x1)
    abstand = groesse * 0.3  # Mindestabstand zum Nachbartext
    f.rechts_frei = (rechts - abstand) if rechts is not None else max(seitenrand_rechts(zeilen), breite - seitenrand_links(zeilen))  # symmetrischer Rand
    f.links_frei = (links + abstand) if links is not None else seitenrand_links(zeilen)


def bestimme_ausrichtung(f: Fundstelle, zeilen: list[list[Zeichen]]) -> None:
    r = f.rect
    if f.regel.ausrichtung != "auto":
        f.ausrichtung = f.regel.ausrichtung
        return
    f.ausrichtung = "links"
    sichtbar = [z for z in f.zeile if z.c.strip()]
    ist_ganze_zeile = sichtbar and sichtbar[0] is next(z for z in f.zeichen if z.c.strip()) and \
        sichtbar[-1] is [z for z in f.zeichen if z.c.strip()][-1]
    if not ist_ganze_zeile:
        return
    # Spaltenprüfung: enden andere Zeilen an derselben x-Position oder beginnen sie dort?
    rechtsbuendig = linksbuendig = 0
    for zeile in zeilen:
        if zeile is f.zeile:
            continue
        sz = [z for z in zeile if z.c.strip()]
        if not sz:
            continue
        rechtsbuendig += abs(sz[-1].bbox.x1 - r.x1) < 1.0
        linksbuendig += abs(sz[0].bbox.x0 - r.x0) < 1.0
    if rechtsbuendig > linksbuendig:
        f.ausrichtung = "rechts"


def seitenrand_rechts(zeilen) -> float:
    return max(z.bbox.x1 for zeile in zeilen for z in zeile)


def seitenrand_links(zeilen) -> float:
    return min(z.bbox.x0 for zeile in zeilen for z in zeile)


# ---------------------------------------------------------------- Schriften


def basisname(name: str) -> str:
    return name.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", name) else name


def normiert(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    for endung in ("mt", "ps"):
        if n.endswith(endung):
            n = n[: -len(endung)]
    return n.replace("regular", "").replace("roman", "")


_systemschriften: dict[str, Path] | None = None
_sammlungen: dict[str, Path] = {}  # normierter Dateiname -> .ttc
_dateinamen: dict[str, Path] = {}  # dateiname.ttf (klein) -> Pfad


def ttc_schnitte(pfad: Path) -> list[bytes]:
    """Zerlegt eine TrueType-Sammlung (.ttc) in einzelne Schriftdateien. PyMuPDF kann aus
    einer .ttc keinen bestimmten Schnitt laden; macOS liefert u. a. Helvetica nur so aus."""
    import struct

    daten = pfad.read_bytes()
    if daten[:4] != b"ttcf":
        return []
    anzahl = struct.unpack_from(">I", daten, 8)[0]
    schnitte = []
    for i in range(anzahl):
        start = struct.unpack_from(">I", daten, 12 + 4 * i)[0]
        version, n, *suche = struct.unpack_from(">IHHHH", daten, start)
        tabellen = [struct.unpack_from(">4sIII", daten, start + 12 + 16 * k) for k in range(n)]
        kopf = struct.pack(">IHHHH", version, n, *suche)
        verzeichnis, inhalt = b"", b""
        versatz = 12 + 16 * n
        for tag, pruefsumme, ab, laenge in tabellen:
            stueck = daten[ab : ab + laenge]
            verzeichnis += struct.pack(">4sIII", tag, pruefsumme, versatz + len(inhalt), laenge)
            inhalt += stueck + b"\0" * (-len(stueck) % 4)
        schnitte.append(kopf + verzeichnis + inhalt)
    return schnitte


def _lies_schriftordner() -> dict[str, Path]:
    global _systemschriften
    if _systemschriften is None:
        _systemschriften = {}
        for ordner in SCHRIFT_ORDNER:
            if not ordner.is_dir():
                continue
            for datei in ordner.rglob("*"):
                endung = datei.suffix.lower()
                if endung in (".ttf", ".otf"):
                    _systemschriften.setdefault(normiert(datei.stem), datei)
                    _dateinamen.setdefault(datei.name.lower(), datei)
                elif endung == ".ttc":
                    _sammlungen.setdefault(normiert(datei.stem), datei)
    return _systemschriften


def schriftdatei(*kandidaten: str) -> Path | None:
    """Erste installierte Datei aus einer Liste von Dateinamen (Groß-/Kleinschreibung egal)."""
    _lies_schriftordner()
    return next((_dateinamen[k.lower()] for k in kandidaten if k.lower() in _dateinamen), None)


def systemschrift(name: str) -> tuple[str, bytes] | None:
    """Installierte Schrift zum Namen im PDF, als (Schlüssel, Schriftdaten)."""
    _lies_schriftordner()
    gesucht = normiert(basisname(name))
    if pfad := _systemschriften.get(gesucht):
        return str(pfad), pfad.read_bytes()
    # Sammlungen, deren Name der Anfang des gesuchten ist: "helveticabold" -> Helvetica.ttc
    for familie, pfad in sorted(_sammlungen.items(), key=lambda kv: -len(kv[0])):
        if not gesucht.startswith(familie):
            continue
        for i, puffer in enumerate(ttc_schnitte(pfad)):
            try:
                if normiert(pymupdf.Font(fontbuffer=puffer).name) == gesucht:
                    return f"{pfad}#{i}", puffer
            except Exception:
                continue
    return None


def deckt_ab(font: pymupdf.Font, text: str) -> bool:
    for c in text:
        if c.isspace():
            continue
        gid = font.has_glyph(ord(c))
        # Subsets behalten teils die Zuordnung, aber ohne Glyphenumriss
        if not gid or pymupdf.Rect(font.glyph_bbox(ord(c))).is_empty:
            return False
    return True


@dataclass
class Schriftwahl:
    font: pymupdf.Font
    alias: str
    quelle: str  # original | system | ersatz
    puffer: bytes | None = None
    base14: str | None = None


class Schriften:
    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        self.eingebettet: dict[str, bytes] = {}
        for seite in doc:
            for xref, _ext, _typ, name, *_ in seite.get_fonts():
                if name not in self.eingebettet:
                    try:
                        _, _, _, puffer = doc.extract_font(xref)
                    except Exception:
                        puffer = b""
                    self.eingebettet[name] = puffer or b""
        self.zaehler = 0
        self.cache: dict[str, Schriftwahl] = {}

    def waehle(self, span: dict, text: str) -> Schriftwahl:
        name = span["font"]
        puffer = next(
            (p for n, p in self.eingebettet.items() if p and basisname(n) == basisname(name)), None
        )
        if puffer:
            try:
                font = pymupdf.Font(fontbuffer=puffer)
                if deckt_ab(font, text):
                    return self._neu("orig:" + basisname(name), font, "original", puffer=puffer)
            except Exception:
                pass
        gefunden = systemschrift(name)
        if gefunden:
            schluessel, puffer = gefunden
            font = pymupdf.Font(fontbuffer=puffer)
            if deckt_ab(font, text):
                return self._neu("sys:" + schluessel, font, "system", puffer=puffer)
        return self._ersatz(span, name, text)

    def _neu(self, schluessel, font, quelle, puffer=None, base14=None) -> Schriftwahl:
        if schluessel not in self.cache:
            self.zaehler += 1
            self.cache[schluessel] = Schriftwahl(font, f"PE{self.zaehler}", quelle, puffer, base14)
        return self.cache[schluessel]

    def _ersatz(self, span: dict, name: str, text: str) -> Schriftwahl:
        n, flags = name.lower(), span["flags"]
        fett = "bold" in n or "black" in n or "heavy" in n or flags & 16
        kursiv = "italic" in n or "oblique" in n or flags & 2
        mono = {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"}
        serif = {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"}
        sans = {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"}
        # Der Name ist verlässlicher als die Flags (Chrome setzt z. B. bei Helvetica das Serif-Flag)
        if any(w in n for w in ("courier", "mono", "consol")):
            familie, gleich = mono, "courier" in n
        elif any(w in n for w in ("helvetica", "arial", "sans", "grotesk", "calibri", "verdana", "segoe")):
            familie, gleich = sans, "helvetica" in n or "arial" in n
        elif any(w in n for w in ("times", "serif", "garamond", "georgia", "cambria", "roman")):
            familie, gleich = serif, "times" in n
        else:
            familie = mono if flags & 8 else serif if flags & 4 else sans
            gleich = False
        stil = (bool(fett), bool(kursiv))
        kurz = familie[stil]
        quelle = "standard" if gleich else "ersatz"
        pfad = schriftdatei(*TRUETYPE_GLEICH[familie[(0, 0)]][stil])
        if pfad:
            font = pymupdf.Font(fontfile=str(pfad))
            if deckt_ab(font, text):
                return self._neu("sys:" + str(pfad), font, quelle, puffer=pfad.read_bytes())
        # Letzter Ausweg: PDF-Standardschrift, kann nur Latin-1 (kein €)
        return self._neu("b14:" + kurz, pymupdf.Font(kurz), quelle, base14=kurz)


# ---------------------------------------------------------------- Ersetzen


@dataclass
class Ergebnis:
    fund: Fundstelle
    schrift: str
    stauchung: float


def farbe(span: dict) -> tuple[float, float, float]:
    c = span["color"]
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def kalibrierung(zeichen: list[Zeichen], font: pymupdf.Font, groesse: float) -> float:
    """Wie breit setzt das PDF diesen Text tatsächlich, im Verhältnis zur gewählten Schrift?
    Gleicht horizontale Skalierung (Tz) und Metrikunterschiede aus. Gemessen wird nur über
    Wörter: Wortabstände setzen Erzeuger oft frei (Chrome z. B. vor dem €), das würde
    sonst die Buchstaben dehnen."""
    ist = soll = 0.0
    wort: list[Zeichen] = []
    for z in [*zeichen, None]:
        if z is None or z.c.isspace():
            if wort:
                ist += wort[-1].bbox.x1 - wort[0].origin.x
                soll += font.text_length("".join(c.c for c in wort), groesse)
                wort = []
        else:
            wort.append(z)
    if soll <= 0:
        return 1.0
    return min(max(ist / soll, 0.7), 1.3)


def bette_ein(seite: pymupdf.Page, wahl: Schriftwahl, eingefuegt: set[str]) -> None:
    if wahl.base14 is None and wahl.alias not in eingefuegt:
        seite.insert_font(fontname=wahl.alias, fontbuffer=wahl.puffer)
        eingefuegt.add(wahl.alias)


def sichtbare_zeichen(seite: pymupdf.Page) -> list[Zeichen]:
    return [z for zeile in zeilen_der_seite(seite) for z in zeile if z.c.strip()]


def entferne(seite: pymupdf.Page, zeichen: list[Zeichen]) -> None:
    """Winziges Rechteck um die Zeichenmitte: MuPDF entfernt nur Zeichen, deren Mitte darin liegt."""
    for z in zeichen:
        cx, cy = (z.bbox.x0 + z.bbox.x1) / 2, (z.bbox.y0 + z.bbox.y1) / 2
        seite.add_redact_annot(pymupdf.Rect(cx - 0.3, cy - 0.3, cx + 0.3, cy + 0.3))
    seite.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )


def vergleiche(erwartet: list[Zeichen], ist: list[Zeichen]) -> tuple[list[Zeichen], list[Zeichen]]:
    """Liefert (fehlt an Originalposition, liegt an falscher Position)."""
    toleranz = 0.25
    eimer: dict[int, list[Zeichen]] = {}
    for z in ist:
        eimer.setdefault(round(z.origin.y), []).append(z)
    benutzt: set[int] = set()
    fehlend = []
    for e in erwartet:
        treffer = None
        for y in (round(e.origin.y) - 1, round(e.origin.y), round(e.origin.y) + 1):
            for z in eimer.get(y, []):
                if id(z) not in benutzt and z.c == e.c and abs(z.origin.x - e.origin.x) < toleranz \
                        and abs(z.origin.y - e.origin.y) < toleranz:
                    treffer = z
                    break
            if treffer:
                break
        if treffer:
            benutzt.add(id(treffer))
        else:
            fehlend.append(e)
    return fehlend, [z for z in ist if id(z) not in benutzt]


def entferne_kontrolliert(seite, weg: list[Zeichen], schriften: Schriften, eingefuegt: set[str]) -> int:
    """MuPDF verschiebt beim Entfernen von Glyphen teils den Rest des Textstrings (gemessen:
    um eine Glyphenbreite). Deshalb: Soll-Ist-Vergleich aller Zeichen der Seite, verrutschte
    entfernen und an exakter Originalposition neu setzen."""
    def schluessel(z: Zeichen):
        return z.c, round(z.origin.x, 2), round(z.origin.y, 2)

    weg_keys = {schluessel(z) for z in weg}
    erwartet = [z for z in sichtbare_zeichen(seite) if schluessel(z) not in weg_keys]
    entferne(seite, weg)
    for _ in range(5):
        fehlend, zuviel = vergleiche(erwartet, sichtbare_zeichen(seite))
        if not zuviel:
            break
        entferne(seite, zuviel)
    else:
        raise RuntimeError(f"Seite {seite.number + 1}: Textpositionen ließen sich nicht stabilisieren")
    for z in fehlend:
        wahl = schriften.waehle(z.span, z.c)
        bette_ein(seite, wahl, eingefuegt)
        seite.insert_text(z.origin, "-" if z.c == "\xad" else z.c, fontsize=z.span["size"],
                          fontname=wahl.base14 or wahl.alias, color=farbe(z.span))
    return len(fehlend)


def ersetze_seite(seite: pymupdf.Page, funde: list[Fundstelle], schriften: Schriften,
                  eingefuegt: set[str] | None = None) -> tuple[list[Ergebnis], int]:
    # 1. Vorbereiten, solange der alte Text noch da ist
    plaene = []
    for f in funde:
        span = f.span
        wahl = schriften.waehle(span, f.ersatz)
        alte_breite = f.zeichen[-1].bbox.x1 - f.zeichen[0].origin.x
        k = kalibrierung(f.zeichen, wahl.font, span["size"])
        plaene.append((f, wahl, k, alte_breite))

    # 2. Nur die Zeichen der Fundstellen entfernen, Grafik und Bilder bleiben.
    #    Danach jedes übrige Zeichen gegen seine Originalposition prüfen und reparieren.
    eingefuegt = set() if eingefuegt is None else eingefuegt
    korrigiert = entferne_kontrolliert(seite, [z for f in funde for z in f.zeichen], schriften, eingefuegt)

    # 3. Neuen Text an alter Grundlinie einsetzen
    ergebnisse = []
    for f, wahl, k, alte_breite in plaene:
        if not f.ersatz.strip():
            ergebnisse += [Ergebnis(g, "-", 1.0) for g in [f, *f.mit]]
            continue
        span, groesse = f.span, f.span["size"]
        bette_ein(seite, wahl, eingefuegt)
        breite = wahl.font.text_length(f.ersatz, groesse) * k
        x0, x1 = f.zeichen[0].origin.x, f.zeichen[-1].bbox.x1
        platz = (f.rechts_frei - x0) if f.ausrichtung == "links" else (x1 - f.links_frei)
        platz = max(platz, alte_breite)
        sx = k
        if breite > platz:
            sx = k * platz / breite
            breite = platz
            stufe = "WARNUNG: " if sx / k < MIN_STAUCHUNG else ""
            f.notizen.append(f"{stufe}auf {sx / k:.0%} gestaucht, sonst Überlappung")
        start_x = x0 if f.ausrichtung == "links" else x1 - breite
        f.neu_rect = pymupdf.Rect(start_x, f.rect.y0, start_x + breite, f.rect.y1)
        punkt = pymupdf.Point(start_x, f.zeichen[0].origin.y)
        seite.insert_text(
            punkt,
            f.ersatz,
            fontsize=groesse,
            fontname=wahl.base14 or wahl.alias,
            color=farbe(span),
            morph=(punkt, pymupdf.Matrix(sx, 1)) if abs(sx - 1) > 1e-3 else None,
        )
        if wahl.quelle == "ersatz":
            f.notizen.append(f"Ersatzschrift {wahl.font.name} statt {basisname(span['font'])}")
        if wahl.base14 and any(ord(c) > 255 for c in f.ersatz):
            f.notizen.append("WARNUNG: Standardschrift ohne Unicode, Sonderzeichen wie € fehlen")
        ergebnisse += [Ergebnis(g, wahl.quelle, sx / k) for g in [f, *f.mit]]
    return ergebnisse, korrigiert


# ---------------------------------------------------------------- Einfügen


@dataclass
class Einfuegung:
    """Neuer Text im Stil einer Vorlage. (x, y) ist die Grundlinie, x die linke Kante
    bzw. bei `rechts` die rechte Kante."""
    seite: int
    x: float
    y: float
    text: str
    stil: dict  # font, size, color, flags – wie ein PyMuPDF-Span
    skalierung: float = 1.0
    ausrichtung: str = "links"
    # Ergebnis nach dem Einfügen
    rect: pymupdf.Rect | None = None
    schrift: str = ""
    notizen: list[str] = field(default_factory=list)


def stil_an(seite: pymupdf.Page, x: float, y: float, schriften: Schriften) -> dict | None:
    """Stil des Texts an einem Punkt (nächstes Zeichen, höchstens 2 pt entfernt), inklusive
    tatsächlicher Laufweite, damit eingefügter Text genauso läuft wie die Vorlage."""

    def abstand(z: Zeichen) -> float:
        dx = max(z.bbox.x0 - x, 0, x - z.bbox.x1)
        dy = max(z.bbox.y0 - y, 0, y - z.bbox.y1)
        return (dx * dx + dy * dy) ** 0.5

    bester = None
    for zeile in zeilen_der_seite(seite):
        for i, z in enumerate(zeile):
            if z.c.strip() and (bester is None or abstand(z) < bester[0]):
                bester = (abstand(z), zeile, i)
    if bester is None or bester[0] > 2:
        return None
    _, zeile, i = bester
    span = zeile[i].span
    # zusammenhängender Lauf gleichen Stils um den Punkt herum
    a = i
    while a > 0 and gleicher_stil(zeile[a - 1].span, span):
        a -= 1
    b = i
    while b + 1 < len(zeile) and gleicher_stil(zeile[b + 1].span, span):
        b += 1
    lauf = zeile[a : b + 1]
    wahl = schriften.waehle(span, "".join(c.c for c in lauf).strip() or "x")
    return {
        "stil": {k: span[k] for k in ("font", "size", "color", "flags")},
        "skalierung": round(kalibrierung(lauf, wahl.font, span["size"]), 4),
        "grundlinie": zeile[i].origin.y,
    }

def fuege_ein(seite: pymupdf.Page, einfuegungen: list[Einfuegung], schriften: Schriften,
              eingefuegt: set[str]) -> None:
    for e in einfuegungen:
        if not e.text.strip():
            continue
        groesse = e.stil["size"]
        wahl = schriften.waehle(e.stil, e.text)
        bette_ein(seite, wahl, eingefuegt)
        breite = wahl.font.text_length(e.text, groesse) * e.skalierung
        x0 = e.x if e.ausrichtung != "rechts" else e.x - breite
        punkt = pymupdf.Point(x0, e.y)
        seite.insert_text(
            punkt, e.text, fontsize=groesse, fontname=wahl.base14 or wahl.alias, color=farbe(e.stil),
            morph=(punkt, pymupdf.Matrix(e.skalierung, 1)) if abs(e.skalierung - 1) > 1e-3 else None,
        )
        e.rect = pymupdf.Rect(x0, e.y - groesse * 0.8, x0 + breite, e.y + groesse * 0.22)
        e.schrift = wahl.quelle
        if wahl.quelle == "ersatz":
            e.notizen.append(f"Ersatzschrift {wahl.font.name} statt {basisname(e.stil['font'])}")


def bearbeite(
    eingabe: Path,
    ausgabe: Path,
    regeln: list[Regel],
    teilstring: bool = False,
    seiten: set[int] | None = None,
    vorschau: Path | None = None,
    einfuegungen: list[Einfuegung] | None = None,
) -> tuple[list[Ergebnis], list[Regel], dict[int, int]]:
    """Einfügungen bekommen ihr Ergebnis (rect, schrift, notizen) direkt gesetzt."""
    doc = pymupdf.open(eingabe)
    if doc.needs_pass:
        raise SystemExit("PDF ist passwortgeschützt.")
    schriften = Schriften(doc)
    alle: list[Ergebnis] = []
    korrekturen: dict[int, int] = {}
    geaendert = []
    for nr, seite in enumerate(doc, 1):
        if seiten and nr not in seiten:
            continue
        funde = finde(seite, nr, regeln, teilstring)
        neue = [e for e in einfuegungen or [] if e.seite == nr]
        eingefuegt: set[str] = set()
        if funde:
            erg, korrigiert = ersetze_seite(seite, funde, schriften, eingefuegt)
            alle += erg
            if korrigiert:
                korrekturen[nr] = korrigiert
        if neue:
            fuege_ein(seite, neue, schriften, eingefuegt)
        if funde or neue:
            geaendert.append(nr)
    if vorschau and geaendert:
        erzeuge_vorschau(eingabe, doc, geaendert, vorschau)
    doc.subset_fonts()  # sonst landet z. B. die komplette Arial (ca. 750 KB) im PDF
    doc.save(ausgabe, garbage=3, deflate=True)
    nicht_gefunden = [r for r in regeln if not any(e.fund.regel is r for e in alle)]
    return alle, nicht_gefunden, korrekturen


def erzeuge_vorschau(eingabe: Path, neu: pymupdf.Document, seiten: list[int], ordner: Path) -> None:
    ordner.mkdir(parents=True, exist_ok=True)
    alt = pymupdf.open(eingabe)
    for nr in seiten:
        a = alt[nr - 1].get_pixmap(dpi=110)
        b = neu[nr - 1].get_pixmap(dpi=110)
        bild = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, a.width * 2 + 20, a.height), False)
        bild.clear_with(200)
        a.set_origin(0, 0)
        b.set_origin(a.width + 20, 0)
        bild.copy(a, a.irect)
        bild.copy(b, b.irect)
        bild.save(ordner / f"seite_{nr}_vorher_nachher.png")


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="pdf-ersetzen",
        description="Ersetzt Textstellen in PDFs, ohne dass sich etwas verschiebt.",
        epilog='Beispiel: pdf-ersetzen rechnung.pdf -r "1.250,00 => 1.380,00" -r "Muster GmbH => Beispiel AG"',
    )
    p.add_argument("pdf", type=Path)
    p.add_argument("-o", "--ausgabe", type=Path, help="Standard: <name>_bearbeitet.pdf")
    p.add_argument("-r", "--regel", action="append", default=[], metavar='"ALT => NEU"')
    p.add_argument("-f", "--regeldatei", type=Path, help="eine Regel pro Zeile: ALT => NEU [| rechts]")
    p.add_argument("-s", "--seiten", help="z. B. 1,3-4 (Standard: alle)")
    p.add_argument("--teilstring", action="store_true", help="auch innerhalb von Wörtern/Zahlen ersetzen")
    p.add_argument("--vorschau", action="store_true", help="PNG vorher/nachher je geänderter Seite")
    a = p.parse_args(argv)

    if not a.pdf.exists():
        p.error(f"{a.pdf} nicht gefunden")
    ausgabe = a.ausgabe or a.pdf.with_name(a.pdf.stem + "_bearbeitet.pdf")
    if ausgabe.resolve() == a.pdf.resolve():
        p.error("Ausgabe darf nicht die Eingabedatei sein")
    seiten = None
    if a.seiten:
        seiten = set()
        for teil in a.seiten.split(","):
            von, _, bis = teil.partition("-")
            seiten.update(range(int(von), int(bis or von) + 1))
    vorschau = ausgabe.with_name(ausgabe.stem + "_vorschau") if a.vorschau else None

    regeln = lies_regeln(a.regel, a.regeldatei)
    ergebnisse, fehlend, korrekturen = bearbeite(a.pdf, ausgabe, regeln, a.teilstring, seiten, vorschau)

    for e in ergebnisse:
        f = e.fund
        zusatz = f"; {'; '.join(f.notizen)}" if f.notizen else ""
        print(f"S. {f.seite}: {f.regel.alt!r} → {f.regel.neu!r}  [{f.ausrichtung}, Schrift: {e.schrift}{zusatz}]")
    for nr, anzahl in korrekturen.items():
        print(f"S. {nr}: {anzahl} Nachbarzeichen, die MuPDF verschoben hatte, auf Originalposition zurückgesetzt")
    for r in fehlend:
        print(f"NICHT GEFUNDEN: {r.alt!r}", file=sys.stderr)
    print(f"\n{len(ergebnisse)} Ersetzung(en) → {ausgabe}")
    if vorschau and ergebnisse:
        print(f"Vorschau: {vorschau}/")
    return 1 if fehlend else 0


if __name__ == "__main__":
    sys.exit(main())
