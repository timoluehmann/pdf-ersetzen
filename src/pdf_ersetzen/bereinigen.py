"""Saubere Ausgabe: Das gespeicherte PDF soll aussehen wie in einem Zug erzeugt.

Entfernt die Spuren, die Bearbeitungen hinterlassen (auch die anderer Werkzeuge vorher):
Revisionen, Bearbeitungs-Metadaten, ungleiche Dokument-ID, Annotationen aus Editoren, mehrere
Content-Streams je Seite, leere Grafikzustände und eigene Schriftkopien. Eingefügter Text nutzt
danach dieselbe Schrift-Ressource wie der Originaltext, sofern es dieselbe Schrift ist.

Sicherung: Jede Zusammenführung von Schriften wird einzeln geprüft und bei Abweichung
zurückgesetzt. Am Ende wird jede Seite verglichen: pixelgenau, bzw. bei umkodiertem Text über
Zeichen, Position und Größe. Weicht etwas ab, wird ohne Bereinigung gespeichert und das gemeldet.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

import pymupdf

EIGENER_ALIAS = re.compile(r"PE\d+")  # so benennt kern.Schriften eingefügte Schriften


# ---------------------------------------------------------------- Schriften


def _ref(wert: str) -> int | None:
    m = re.fullmatch(r"(\d+) 0 R", wert.strip())
    return int(m.group(1)) if m else None


def _objekt(doc: pymupdf.Document, xref: int, key: str) -> str:
    """Wert eines Schlüssels, indirekte Objekte aufgelöst."""
    typ, wert = doc.xref_get_key(xref, key)
    ref = _ref(wert) if typ == "xref" else None
    return doc.xref_object(ref, compressed=True) if ref else wert


def _cid_schrift(doc: pymupdf.Document, xref: int) -> dict | None:
    """Type0/Identity-H als CID-Schrift. `prog` sagt das eingebettete Format: "truetype"
    (CIDFontType2, FontFile2), "cff" (CIDFontType0, FontFile3, z. B. Trade Gothic LT Std) oder
    "type1" (CIDFontType0, FontFile; so verpackt MuPDF eine Type-1-Schrift beim Einfügen). Sonst None."""
    if doc.xref_get_key(xref, "Subtype")[1] != "/Type0" or doc.xref_get_key(xref, "Encoding")[1] != "/Identity-H":
        return None
    nachfahre = _ref(re.sub(r"[\[\]]", "", doc.xref_get_key(xref, "DescendantFonts")[1]))
    art = doc.xref_get_key(nachfahre, "Subtype")[1] if nachfahre else ""
    if art not in ("/CIDFontType2", "/CIDFontType0"):
        return None
    if art == "/CIDFontType2" and doc.xref_get_key(nachfahre, "CIDToGIDMap")[1] not in ("null", "/Identity"):
        return None
    deskriptor = _ref(doc.xref_get_key(nachfahre, "FontDescriptor")[1]) if nachfahre else None
    prog, datei = "truetype", None
    if deskriptor:
        for schluessel, p in (("FontFile2", "truetype"), ("FontFile3", "cff"), ("FontFile", "type1")):
            if (datei := _ref(doc.xref_get_key(deskriptor, schluessel)[1])):
                prog = p
                break
    unicode = _ref(doc.xref_get_key(xref, "ToUnicode")[1])
    if not (datei and unicode):
        return None
    name = doc.xref_get_key(xref, "BaseFont")[1].lstrip("/")
    return {"xref": xref, "nachfahre": nachfahre, "datei": datei, "unicode": unicode, "prog": prog,
            "name": re.sub(r"^[A-Z]{6}\+", "", name)}


def _lies_w(text: str) -> dict[int, str]:
    breiten: dict[int, str] = {}
    teile = re.findall(r"\[[^\]]*\]|[-\d.]+", text.strip()[1:-1])
    i = 0
    while i < len(teile):
        start = int(float(teile[i]))
        if teile[i + 1].startswith("["):
            for k, w in enumerate(teile[i + 1][1:-1].split()):
                breiten[start + k] = w
            i += 2
        else:
            for cid in range(start, int(float(teile[i + 1])) + 1):
                breiten[cid] = teile[i + 2]
            i += 3
    return breiten


def _lies_tounicode(cmap: bytes) -> dict[int, str]:
    text = cmap.decode("latin-1")
    karte: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            karte[int(src, 16)] = dst
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if rest.startswith("["):
                for k, dst in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", rest)):
                    karte[lo + k] = dst
            else:
                basis = int(rest[1:-1], 16)
                for k in range(hi - lo + 1):
                    karte[lo + k] = f"{basis + k:0{len(rest) - 2}X}"
    return karte


def _schreibe_tounicode(karte: dict[int, str], byte: bool = False) -> bytes:
    zeilen = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
              "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def",
              "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
              "1 begincodespacerange", "<00> <FF>" if byte else "<0000> <FFFF>", "endcodespacerange"]
    eintraege = sorted(karte.items())
    for i in range(0, len(eintraege), 100):
        teil = eintraege[i:i + 100]
        zeilen.append(f"{len(teil)} beginbfchar")
        zeilen += [f"<{cid:0{2 if byte else 4}X}> <{dst}>" for cid, dst in teil]
        zeilen.append("endbfchar")
    zeilen += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(zeilen).encode()


def _seitenschriften(doc: pymupdf.Document, seite: pymupdf.Page) -> dict[str, int]:
    return {alias: xref for xref, _e, _t, _n, alias, *_ in seite.get_fonts(full=True) if alias}


def _setze_ressource(doc: pymupdf.Document, seite: pymupdf.Page, art: str, name: str, xref: int | None) -> None:
    """Setzt oder entfernt (xref None) einen Eintrag in Resources/<art> der Seite."""
    typ, wert = doc.xref_get_key(seite.xref, f"Resources/{art}")
    wert = doc.xref_object(_ref(wert), compressed=True) if typ == "xref" else wert
    wert = re.sub(r"/" + re.escape(name) + r"\s*\d+ 0 R", "", wert if wert.startswith("<<") else "<<>>")
    if xref:
        wert = wert[:-2] + f"/{name} {xref} 0 R>>"
    if typ == "xref":
        doc.update_object(_ref(doc.xref_get_key(seite.xref, f"Resources/{art}")[1]), wert)
    else:
        doc.xref_set_key(seite.xref, f"Resources/{art}", wert)


def _benenne_um(seite: pymupdf.Page, alt: str, neu: str, operator: bytes = rb"\s+[-\d.]+\s+Tf") -> None:
    xref = seite.get_contents()[0]
    roh = seite.parent.xref_stream(xref)
    seite.parent.update_stream(xref, re.sub(rb"/" + alt.encode() + rb"(?=" + operator + rb")", b"/" + neu.encode(), roh))


def _neutrale_xobjekte(doc: pymupdf.Document) -> None:
    """Eingebrannte Annotationen heißen bei MuPDF /Annot<n>, das verrät die Herkunft."""
    for seite in doc:
        namen = {name for _x, name, *_ in seite.get_xobjects()}
        for xref, name, *_ in seite.get_xobjects():
            if re.fullmatch(r"Annot\d+", name):
                n = 1
                while f"Fm{n}" in namen:
                    n += 1
                namen.add(f"Fm{n}")
                _setze_ressource(doc, seite, "XObject", f"Fm{n}", xref)
                _setze_ressource(doc, seite, "XObject", name, None)
                _benenne_um(seite, name, f"Fm{n}", rb"\s+Do")


def _freier_alias(belegt: set[str]) -> str:
    n = 1
    while f"F{n}" in belegt:
        n += 1
    return f"F{n}"


# Adobe-Glyphennamen für Zeichen, die sich nicht aus einer Standardkodierung ergeben (seltene
# /Differences-Einträge in InDesign-PDFs). ASCII und Latin-1 kommen über cp1252/latin-1, hier nur
# der häufige Rest aus westeuropäischen Texten.
_GLYPHNAMEN = {
    "space": " ", "nbspace": "\xa0", "euro": "€", "bullet": "•", "periodcentered": "·",
    "endash": "–", "emdash": "—", "hyphen": "-", "sfthyphen": "\xad", "minus": "−",
    "quoteleft": "‘", "quoteright": "’", "quotedblleft": "“", "quotedblright": "”",
    "quotesinglbase": "‚", "quotedblbase": "„", "quotesingle": "'", "quotedbl": '"',
    "guillemotleft": "«", "guillemotright": "»", "guilsinglleft": "‹", "guilsinglright": "›",
    "ellipsis": "…", "trademark": "™", "copyright": "©", "registered": "®", "degree": "°",
    "section": "§", "paragraph": "¶", "dagger": "†", "daggerdbl": "‡", "perthousand": "‰",
    "fi": "fi", "fl": "fl", "germandbls": "ß", "Euro": "€",
    "adieresis": "ä", "odieresis": "ö", "udieresis": "ü", "Adieresis": "Ä", "Odieresis": "Ö",
    "Udieresis": "Ü", "aacute": "á", "agrave": "à", "acircumflex": "â", "eacute": "é",
    "egrave": "è", "ecircumflex": "ê", "edieresis": "ë", "iacute": "í", "ocircumflex": "ô",
    "ccedilla": "ç", "ntilde": "ñ", "oslash": "ø", "aring": "å", "ae": "æ",
}


# Adobe-Namen der druckbaren ASCII-Zeichen (Buchstaben und Ziffern programmatisch, Rest fest)
_ASCII_NAMEN = {
    "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "'", "parenleft": "(", "parenright": ")", "asterisk": "*",
    "plus": "+", "comma": ",", "hyphen": "-", "period": ".", "slash": "/", "colon": ":",
    "semicolon": ";", "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~",
}
_ASCII_NAMEN |= {n: chr(ord("A") + i) for i, n in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
_ASCII_NAMEN |= {n.lower(): chr(ord("a") + i) for i, n in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
_ASCII_NAMEN |= {n: str(i) for i, n in enumerate(
    "zero one two three four five six seven eight nine".split())}


def _glyph_zu_zeichen(name: str) -> str | None:
    """Adobe-Glyphenname -> Zeichen. uniXXXX, uXXXXXX, dann die Namenstabellen."""
    if name in _ASCII_NAMEN:
        return _ASCII_NAMEN[name]
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4})", name) or re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except (ValueError, OverflowError):
            return None
    return _GLYPHNAMEN.get(name)


_BASIS_KODIERUNG = {"/WinAnsiEncoding": "cp1252", "/MacRomanEncoding": "mac_roman",
                    "/StandardEncoding": "latin-1", "/PDFDocEncoding": "latin-1"}


def _einfache_kodierung(doc: pymupdf.Document, xref: int) -> dict[int, str] | None:
    """Code (0-255) -> Zeichen. Standardkodierung als Basis, dann /Differences darüber. None,
    wenn keine Kodierung angegeben ist (dann ist die interne Schriftkodierung nicht erschließbar)."""
    typ, enc = doc.xref_get_key(xref, "Encoding")
    basis = _BASIS_KODIERUNG.get(enc if typ == "name" else "")
    diffs = ""
    if typ == "xref" or enc.startswith("<<"):
        unter = doc.xref_object(_ref(enc), compressed=True) if typ == "xref" else enc
        b = re.search(r"/BaseEncoding\s*(/\w+)", unter)
        basis = _BASIS_KODIERUNG.get(b.group(1) if b else "", basis or "cp1252")
        d = re.search(r"/Differences\s*\[(.*?)\]", unter, re.S)
        diffs = d.group(1) if d else ""
    if basis is None:
        return None
    tabelle: dict[int, str] = {}
    for code in range(256):
        try:
            z = bytes([code]).decode(basis)
            if z.isprintable() or z == "\xa0":
                tabelle[code] = z
        except (ValueError, UnicodeError):
            pass
    code = 0
    for tok in re.findall(r"\d+|/[^\s/\[\]]+", diffs):
        if tok[0] == "/":
            if (z := _glyph_zu_zeichen(tok[1:])) is not None:
                tabelle[code] = z
            else:
                tabelle.pop(code, None)
            code += 1
        else:
            code = int(tok)
    return tabelle


def _einfache_schrift(doc: pymupdf.Document, xref: int) -> dict | None:
    """Einfache Schrift (TrueType, Type1 oder Type1C) mit erschließbarer Kodierung. So bettet
    Word normalen Text ein (TrueType/WinAnsi), InDesign Hausschriften (Type1C, oft /Differences)."""
    if doc.xref_get_key(xref, "Subtype")[1] not in ("/TrueType", "/Type1", "/MMType1"):
        return None
    kodierung = _einfache_kodierung(doc, xref)
    if kodierung is None:
        return None
    try:
        erster = int(doc.xref_get_key(xref, "FirstChar")[1])
        breiten = _objekt(doc, xref, "Widths").strip("[] \n").split()
    except ValueError:
        return None
    return {"xref": xref, "unicode": _ref(doc.xref_get_key(xref, "ToUnicode")[1]),
            "breiten": {erster + i: w for i, w in enumerate(breiten)}, "kodierung": kodierung}


def _schriftname(doc: pymupdf.Document, xref: int) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", doc.xref_get_key(xref, "BaseFont")[1].lstrip("/").replace("#20", " "))


_TOKEN = re.compile(rb"\((?:[^()\\]|\\.|\((?:[^()\\]|\\.)*\))*\)|<<|>>|<[0-9A-Fa-f\s]*>|/[^\s/\[\]()<>{}%]+"
                    rb"|[^\s/\[\]()<>{}%]+|[\[\]]")


def _schreibe_text_um(roh: bytes, alias: str, neu_alias: str, kodiere=None) -> bytes | None:
    """Stellt Text der Schrift `alias` auf `neu_alias` um; `kodiere(bytes, im_array)` liefert
    für jeden Hex-String den Ersatz (inklusive Klammern und ggf. Kerning-Zahlen). None, wenn das
    nicht eindeutig geht (z. B. Literal-String in dieser Schrift)."""
    teile, pos, schrift, vorige, im_text, tiefe = [], 0, None, [], False, 0
    for m in _TOKEN.finditer(roh):
        tok = m.group(0)
        if tok in (b"[", b"]"):
            tiefe += 1 if tok == b"[" else -1
        if tok in (b"BT", b"ET"):
            im_text = tok == b"BT"
        elif not im_text:  # Strings außerhalb von BT…ET sind keine Textausgabe (z. B. /Lang(de-DE))
            pass
        elif tok == b"Tf" and len(vorige) >= 2:
            schrift = vorige[-2][0][1:].decode("latin-1")
            if schrift == alias:
                s, e = vorige[-2][1]
                teile += [roh[pos:s], b"/" + neu_alias.encode()]
                pos = e
        elif schrift == alias and tok.startswith(b"("):
            return None
        elif schrift == alias and kodiere and tok.startswith(b"<") and tok != b"<<":
            neu = kodiere(bytes.fromhex(tok[1:-1].decode()), tiefe > 0)
            if neu is None:
                return None
            teile += [roh[pos:m.start()], neu]
            pos = m.end()
        vorige = (vorige + [(tok, m.span())])[-3:]
    return b"".join(teile) + roh[pos:]


class _Sicherung:
    """Merkt sich Objekte vor einer Änderung, um sie bei Abweichung zurückzusetzen."""

    def __init__(self, doc: pymupdf.Document):
        self.doc, self.alt = doc, {}

    def merke(self, *xrefs: int | None) -> None:
        for x in xrefs:
            if x and x not in self.alt:
                strom = self.doc.xref_stream(x) if self.doc.xref_is_stream(x) else None
                self.alt[x] = (self.doc.xref_object(x, compressed=True), strom)

    def zurueck(self) -> None:
        for x, (objekt, strom) in self.alt.items():
            self.doc.update_object(x, objekt)
            if strom is not None:
                self.doc.update_stream(x, strom)


def _verschmelze_cid(doc, seite, alias, kopie: dict, original: dict, ziel: str, s: _Sicherung) -> bool:
    """Gleiche CID-Schrift: Original bekommt das (vollständigere) Schriftprogramm, die Breiten und
    Unicode-Zuordnung der Kopie dazu; der Text zeigt danach aufs Original. Nur TrueType: Bei CFF
    hängen die CIDs am charset des Programms, ein Tausch würde den Originaltext umnummerieren."""
    if original["prog"] != "truetype" or kopie["prog"] != "truetype":
        return False
    s.merke(original["xref"], original["nachfahre"], original["unicode"], original["datei"])
    w = _lies_w(_objekt(doc, kopie["nachfahre"], "W"))
    w.update(_lies_w(_objekt(doc, original["nachfahre"], "W")))
    doc.xref_set_key(original["nachfahre"], "W", "[" + " ".join(f"{c}[{w[c]}]" for c in sorted(w)) + "]")
    karte = _lies_tounicode(doc.xref_stream(kopie["unicode"]))
    karte.update(_lies_tounicode(doc.xref_stream(original["unicode"])))
    doc.update_stream(original["unicode"], _schreibe_tounicode(karte))
    programm = doc.xref_stream(kopie["datei"])
    doc.update_stream(original["datei"], programm)
    doc.xref_set_key(original["datei"], "Length1", str(len(programm)))
    # Breiten des Originals haben Vorrang, der eingefügte Text bekommt Kerning-Ausgleich
    return _kodiere_um(doc, seite, alias, kopie, original["xref"], ziel)


# Zeichen, die sich eine Glyphe teilen und je nach Erzeuger unterschiedlich in ToUnicode stehen
_GLEICHE_GLYPHE = {"\xa0": " ", " ": "\xa0", "\xad": "-", "-": "\xad", "\u2011": "-"}


def _glyphen_mit_umriss(programm: bytes) -> set[int] | None:
    """Glyph-IDs eines TrueType-Programms, die wirklich Umrisse haben (loca-Tabelle). Subsets
    behalten oft alle Glyph-Slots und ToUnicode-Einträge, leeren aber die Umrisse."""
    import struct
    try:
        anzahl = struct.unpack(">H", programm[4:6])[0]
        tabellen = {programm[12 + 16 * i:16 + 16 * i]: struct.unpack(">II", programm[20 + 16 * i:28 + 16 * i])
                    for i in range(anzahl)}
        kopf, _ = tabellen[b"head"]
        lang = struct.unpack(">h", programm[kopf + 50:kopf + 52])[0] == 1
        start, laenge = tabellen[b"loca"]
        roh = programm[start:start + laenge]
        werte = struct.unpack(f">{laenge // 4}I", roh) if lang else [w * 2 for w in struct.unpack(f">{laenge // 2}H", roh)]
    except (KeyError, struct.error, IndexError):
        return None
    return {gid for gid in range(len(werte) - 1) if werte[gid + 1] > werte[gid]}


def _cff_index(daten: bytes, pos: int) -> tuple[list[bytes], int]:
    """Liest einen CFF-INDEX ab `pos`, liefert die Einträge und die Position dahinter."""
    anzahl = int.from_bytes(daten[pos:pos + 2], "big")
    if anzahl == 0:
        return [], pos + 2
    groesse = daten[pos + 2]
    start = pos + 3
    offsets = [int.from_bytes(daten[start + i * groesse:start + (i + 1) * groesse], "big") for i in range(anzahl + 1)]
    basis = start + (anzahl + 1) * groesse - 1
    return [daten[basis + offsets[i]:basis + offsets[i + 1]] for i in range(anzahl)], basis + offsets[-1]


def _cff_dict(roh: bytes) -> dict[int, list[float]]:
    """Top DICT: Operator -> Operanden (zweibytige Operatoren als 1200 + n)."""
    ergebnis, operanden, i = {}, [], 0
    while i < len(roh):
        b = roh[i]
        if b <= 21:
            op = 1200 + roh[i + 1] if b == 12 else b
            i += 2 if b == 12 else 1
            ergebnis[op], operanden = operanden, []
        elif b == 28:
            operanden.append(int.from_bytes(roh[i + 1:i + 3], "big", signed=True)); i += 3
        elif b == 29:
            operanden.append(int.from_bytes(roh[i + 1:i + 5], "big", signed=True)); i += 5
        elif b == 30:  # Gleitkommazahl, Halbbytes bis 0xf
            i += 1
            while i < len(roh) and (roh[i] & 0x0F) != 0x0F and (roh[i] >> 4) != 0x0F:
                i += 1
            i += 1
            operanden.append(0.0)
        elif b <= 246:
            operanden.append(b - 139); i += 1
        elif b <= 250:
            operanden.append((b - 247) * 256 + roh[i + 1] + 108); i += 2
        elif b <= 254:
            operanden.append(-(b - 251) * 256 - roh[i + 1] - 108); i += 2
        else:
            i += 1
    return ergebnis


def _cff_glyphen_mit_umriss(programm: bytes) -> set[int] | None:
    """CIDs eines CFF-Programms (auch in OpenType verpackt), deren Glyphe mehr als ein bloßes
    `endchar` ist. Subsets ersetzen entfernte Glyphen durch genau dieses eine Byte."""
    import struct
    try:
        if programm[:4] == b"OTTO":
            anzahl = struct.unpack(">H", programm[4:6])[0]
            for i in range(anzahl):
                eintrag = programm[12 + 16 * i:28 + 16 * i]
                if eintrag[:4] == b"CFF ":
                    start, laenge = struct.unpack(">II", eintrag[8:16])
                    programm = programm[start:start + laenge]
                    break
            else:
                return None
        _namen, pos = _cff_index(programm, programm[2])
        tops, pos = _cff_index(programm, pos)
        top = _cff_dict(tops[0])
        zeichen, _ = _cff_index(programm, int(top[17][0]))
        gids = {gid for gid, z in enumerate(zeichen) if len(z) > 1}
        if 1230 not in top:  # nicht CID-keyed: CID = GID
            return gids
        # CID-keyed: charset bildet GID -> CID ab (GID 0 ist .notdef)
        cids, pos, gid = [0], int(top.get(15, [0])[0]), 1
        if pos == 0:
            return gids
        art, pos = programm[pos], pos + 1
        while gid < len(zeichen):
            if art == 0:
                cids.append(int.from_bytes(programm[pos:pos + 2], "big")); pos += 2; gid += 1
            else:
                erster = int.from_bytes(programm[pos:pos + 2], "big")
                weitere = programm[pos + 2] if art == 1 else int.from_bytes(programm[pos + 2:pos + 4], "big")
                pos += 3 if art == 1 else 4
                cids += range(erster, erster + weitere + 1)
                gid += weitere + 1
        return {cids[g] for g in gids if g < len(cids)}
    except (KeyError, IndexError, struct.error, ValueError):
        return None


def _codes_im_original(doc: pymupdf.Document, xref: int) -> dict[str, tuple[bytes, float]] | None:
    """Zeichen -> Code der Originalschrift, nur für Glyphen, die ihr Subset wirklich enthält:
    bei CID-Schriften alles aus der ToUnicode-Tabelle, bei einfachen WinAnsi-Schriften alles mit
    Breite > 0 (Word und Co. schreiben Breiten nur für genutzte Zeichen)."""
    if (cid := _cid_schrift(doc, xref)):
        programm = doc.xref_stream(cid["datei"])
        umrisse = {"cff": _cff_glyphen_mit_umriss, "truetype": _glyphen_mit_umriss}.get(cid["prog"], lambda _p: None)(programm)
        breiten = _lies_w(_objekt(doc, cid["nachfahre"], "W"))
        standard = float(doc.xref_get_key(cid["nachfahre"], "DW")[1]) if doc.xref_get_key(cid["nachfahre"], "DW")[0] != "null" else 1000.0
        tabelle: dict[str, tuple[bytes, float]] = {}
        for code, ziel in sorted(_lies_tounicode(doc.xref_stream(cid["unicode"])).items()):
            try:
                zeichen = bytes.fromhex(ziel).decode("utf-16-be")
                # umrisse None = Format nicht prüfbar (Type1 oder Parser-Fehler): alles aufnehmen,
                # das Render-Netz fängt unsichtbare Glyphen ab.
                if umrisse is not None and code not in umrisse and not zeichen.isspace():
                    continue
                tabelle.setdefault(zeichen, (code.to_bytes(2, "big"), float(breiten.get(code, standard))))
            except (ValueError, UnicodeError, OverflowError):
                pass
        return tabelle
    if (einfach := _einfache_schrift(doc, xref)):
        tabelle = {}
        for code, breite in einfach["breiten"].items():
            try:
                zeichen = einfach["kodierung"].get(code)
                if zeichen is not None and float(breite) > 0:
                    tabelle.setdefault(zeichen, (bytes([code]), float(breite)))
            except ValueError:
                pass
        return tabelle
    return None


def _kodiere_um(doc, seite, alias, kopie: dict, original: int, ziel: str) -> bool:
    """Eingefügten Text auf die Glyphen umschreiben, die schon in der Originalschrift stecken.
    Das Original bleibt dabei unverändert. Fehlt ein Zeichen im Subset: False."""
    tabelle = _codes_im_original(doc, original)
    if not tabelle:
        return False
    unicode = _lies_tounicode(doc.xref_stream(kopie["unicode"]))
    w_kopie = _lies_w(_objekt(doc, kopie["nachfahre"], "W"))

    def kodiere(gids: bytes, im_array: bool) -> bytes | None:
        """Gleiche Laufweite wie vorher: Weicht die Breite im Original (z. B. gerundet) ab,
        gleicht eine Kerning-Zahl im TJ-Array das je Zeichen aus."""
        teile: list[bytes] = []
        for i in range(0, len(gids) - 1, 2):
            gid = int.from_bytes(gids[i:i + 2], "big")
            try:
                zeichen = bytes.fromhex(unicode[gid]).decode("utf-16-be")
                code, breite = tabelle.get(zeichen) or tabelle[_GLEICHE_GLYPHE[zeichen]]
                soll = float(w_kopie[gid])
            except (KeyError, ValueError, UnicodeError):
                return None
            teile.append(b"<" + code.hex().upper().encode() + b">")
            if abs(breite - soll) > 1e-3:
                if not im_array:
                    return None
                teile.append(f"{breite - soll:.3f}".rstrip("0").rstrip(".").encode())
        return b"".join(teile)

    roh = _schreibe_text_um(seite.read_contents(), alias, ziel, kodiere)
    if roh is None:
        return False
    doc.update_stream(seite.get_contents()[0], roh)
    return True


def _zeichen(seite: pymupdf.Page) -> list[tuple[str, float, float, float]]:
    return [(_GLEICHE_GLYPHE.get(c["c"], c["c"]) if c["c"] in "\xa0\xad\u2011" else c["c"],
             c["origin"][0], c["origin"][1], sp["size"])
            for b in seite.get_text("rawdict")["blocks"] for l in b.get("lines", [])
            for sp in l["spans"] for c in sp["chars"]]


def _glyphen_gleich(vorher: pymupdf.Page, nachher: pymupdf.Page) -> bool:
    """Jedes sichtbare Zeichen hat nachher ungefähr so viel Tinte wie vorher. Fängt Glyphen, die
    im Subset fehlen: Der Text wäre noch extrahierbar, aber unsichtbar. Gezählt wird nur in
    Pixelzeilen, die sich überhaupt verändert haben."""
    f = 2  # 144 dpi
    a, b = (s.get_pixmap(dpi=72 * f, colorspace=pymupdf.csGRAY) for s in (vorher, nachher))
    if a.samples == b.samples:
        return True
    if (a.width, a.height) != (b.width, b.height):
        return False
    st, sa, sb = a.stride, a.samples, b.samples
    anders = [sa[y * st:(y + 1) * st] != sb[y * st:(y + 1) * st] for y in range(a.height)]
    for blk in nachher.get_text("rawdict")["blocks"]:
        for zeile in blk.get("lines", []):
            for span in zeile["spans"]:
                for c in span["chars"]:
                    if not c["c"].strip():
                        continue
                    x0, y0, x1, y1 = c["bbox"]
                    y0, y1 = max(0, int(y0 * f)), min(a.height, int(y1 * f) + 1)
                    if not any(anders[y0:y1]):
                        continue
                    x0, x1 = max(0, int(x0 * f)), min(a.width, int(x1 * f) + 1)
                    tinte = [sum(255 - v for y in range(y0, y1) for v in s[y * st + x0:y * st + x1]) for s in (sa, sb)]
                    if abs(tinte[0] - tinte[1]) > 0.35 * max(tinte) + 255 * 4:
                        return False
    return True


def _text_gleich(a: list, b: list, toleranz: float = 0.1) -> bool:
    """Gleiche Zeichen an gleicher Stelle in gleicher Größe (Schriftwechsel ändert nur Glyphen-
    Rasterung im Subpixelbereich, das ist kein Pixelvergleich mehr)."""
    def sortiert(z):
        return sorted(z, key=lambda e: (round(e[2]), e[1], e[0]))
    return len(a) == len(b) and all(
        x[0] == y[0] and abs(x[1] - y[1]) < toleranz and abs(x[2] - y[2]) < toleranz and abs(x[3] - y[3]) < 0.01
        for x, y in zip(sortiert(a), sortiert(b)))


def _grund(doc, seite, alias, kopie: dict | None, kandidaten: list[int]) -> str:
    """Warum eine eingefügte Schrift nicht mit dem Original zusammengeht, in Worten."""
    if not kopie:
        return "weil ihr Schriftformat für das Zusammenführen noch nicht unterstützt wird"
    if not kandidaten:
        return "weil das PDF keine passende Originalschrift enthält"
    benutzt: set[str] = set()
    if kopie:
        unicode = _lies_tounicode(doc.xref_stream(kopie["unicode"]))

        def sammle(gids: bytes, _im_array: bool) -> bytes:
            for i in range(0, len(gids) - 1, 2):
                if (u := unicode.get(int.from_bytes(gids[i:i + 2], "big"))):
                    benutzt.update(bytes.fromhex(u).decode("utf-16-be", "replace"))
            return b""

        _schreibe_text_um(seite.read_contents(), alias, alias, sammle)
    vorhanden = set().union(*((_codes_im_original(doc, ox) or {}).keys() for ox in kandidaten))
    fehlend = sorted(z for z in benutzt if z not in vorhanden and _GLEICHE_GLYPHE.get(z) not in vorhanden)
    if fehlend:
        return f"weil „{''.join(fehlend)}“ in der eingebetteten Originalschrift fehlt"
    if not any(_codes_im_original(doc, ox) for ox in kandidaten):
        return "weil das Schriftformat des Originals für das Zusammenführen noch nicht unterstützt wird"
    return "weil sie sich nicht verlustfrei mit der Originalschrift zusammenführen lässt"


def vereinheitliche_schriften(doc: pymupdf.Document, herkunft: dict[str, set[str]] | None = None
                              ) -> tuple[list[str], set[int]]:
    """Hängt eingefügten Text an die gleichnamige Originalschrift und entfernt die Kopie.

    1. Umkodieren auf Glyphen des Originals (Original bleibt unverändert, geprüft über Zeichen
       und Positionen).
    2. Sonst bei gleicher CID-Schrift das Schriftprogramm übernehmen (pixelgenau geprüft).
    Jeder Versuch wird bei Abweichung zurückgesetzt. Bleibt eine Kopie übrig, bekommt sie einen
    neutralen Namen. Rückgabe: Hinweise und Seiten, deren Text umkodiert wurde."""
    from .kern import normiert

    nutzung: dict[int, set[int]] = {}
    originale: list[tuple[str, int]] = []
    for seite in doc:
        for alias, xref in _seitenschriften(doc, seite).items():
            nutzung.setdefault(xref, set()).add(seite.number)
            if not EIGENER_ALIAS.fullmatch(alias) and xref not in {x for _, x in originale}:
                originale.append((normiert(_schriftname(doc, xref)), xref))
    ausgang = pymupdf.open("pdf", doc.tobytes())  # Vergleichsstand, frisch geladen
    text = {s.number: _zeichen(s) for s in ausgang}
    hinweise, umkodiert = [], set()
    for seite in doc:
        for alias, xref in list(_seitenschriften(doc, seite).items()):
            if not EIGENER_ALIAS.fullmatch(alias):
                continue
            kopie, name = _cid_schrift(doc, xref), _schriftname(doc, xref)
            # gleicher Name zuerst, dann die Originalschrift, die der eingefügte Text ersetzt
            ersetzt = {normiert(n) for n in (herkunft or {}).get(alias, ())}
            kandidaten = [ox for n, ox in originale if kopie and n == normiert(name)] \
                + [ox for n, ox in originale if kopie and n != normiert(name) and n in ersetzt]
            erledigt = False
            for weg in ("umkodieren", "programm"):
                for ox in kandidaten:
                    schriften = _seitenschriften(doc, seite)
                    ziel = next((a for a, x in schriften.items() if x == ox), None) or _freier_alias(set(schriften))
                    s = _Sicherung(doc)
                    s.merke(seite.xref, seite.get_contents()[0], _ref(doc.xref_get_key(seite.xref, "Resources/Font")[1]))
                    betroffen = sorted(nutzung[ox] | {seite.number})
                    if weg == "programm":  # frisch geladen: MuPDF cacht Schriftprogramme je Objekt
                        frisch = pymupdf.open("pdf", doc.tobytes())
                        stand = {i: frisch[i].get_pixmap(dpi=72).samples for i in betroffen}
                    if weg == "umkodieren":
                        ok = _kodiere_um(doc, seite, alias, kopie, ox, ziel)
                    else:
                        cid = _cid_schrift(doc, ox)
                        ok = bool(cid) and _verschmelze_cid(doc, seite, alias, kopie, cid, ziel, s)
                    if ok:
                        _setze_ressource(doc, seite, "Font", ziel, ox)
                        _setze_ressource(doc, seite, "Font", alias, None)
                        frisch = pymupdf.open("pdf", doc.tobytes())
                        ok = _text_gleich(_zeichen(frisch[seite.number]), text[seite.number]) \
                            and _glyphen_gleich(ausgang[seite.number], frisch[seite.number])
                        if weg == "programm":  # neues Schriftprogramm: jede Seite damit muss pixelgleich bleiben
                            ok = ok and all(frisch[i].get_pixmap(dpi=72).samples == stand[i] for i in betroffen)
                    if ok:
                        nutzung[ox].add(seite.number)
                        umkodiert.add(seite.number)
                        erledigt = True
                        break
                    s.zurueck()
                if erledigt:
                    break
            if not erledigt:
                schriften = _seitenschriften(doc, seite)
                ziel = _freier_alias(set(schriften))
                _setze_ressource(doc, seite, "Font", ziel, xref)
                _setze_ressource(doc, seite, "Font", alias, None)
                _benenne_um(seite, alias, ziel)
                # Nur melden, wenn wirklich zwei Fassungen derselben Schrift im PDF stehen. Hat der
                # neue Text das Original ganz verdrängt (ganze Zeile neu gesetzt), gibt es nur eine.
                if any(n == normiert(name) or n in ersetzt for n, _ in originale):
                    hinweise.append(f"S. {seite.number + 1}: {name} bleibt zusätzlich im PDF, "
                                    + _grund(doc, seite, ziel, kopie, kandidaten))
    return hinweise, umkodiert


# ---------------------------------------------------------------- Seiteninhalt und Metadaten

_LEER = re.compile(rb"(?<![^\s])q\s+(?:(?:[-\d.]+\s+)+(?:rg|RG|g|G|k|K|w)\s+)*Q(?:\s+|$)")


def entferne_leere_zustaende(seite: pymupdf.Page) -> None:
    xref = seite.get_contents()[0]
    roh = alt = seite.parent.xref_stream(xref)
    while (neu := _LEER.sub(b"", roh)) != roh:
        roh = neu
    if roh != alt:
        seite.parent.update_stream(xref, re.sub(rb"\n{2,}", b"\n", roh).strip() + b"\n")


METADATEN = {"title": "Title", "author": "Author", "subject": "Subject", "keywords": "Keywords"}


def lies_metadaten(doc: pymupdf.Document) -> dict[str, str]:
    """Die inhaltlichen Angaben, die in der Oberfläche bearbeitet werden können."""
    alt = doc.metadata or {}
    return {k: (alt.get(k) or "").strip() for k in METADATEN}


def _pdf_text(wert: str) -> str:
    if wert.isascii():
        return "(" + wert.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    return "<FEFF" + wert.encode("utf-16-be").hex().upper() + ">"


def setze_metadaten(doc: pymupdf.Document, metadaten: dict[str, str] | None = None) -> None:
    """Nur Titel, Autor, Thema und Stichwörter bleiben (aus `metadaten`, sonst wie bisher). Kein
    Erstellungs- oder Änderungsdatum, kein Erzeuger, keine XMP-Historie, neue einheitliche ID."""
    werte = lies_metadaten(doc) | {k: v.strip() for k, v in (metadaten or {}).items() if k in METADATEN}
    info = "<<" + "".join(f"/{METADATEN[k]}{_pdf_text(v)}" for k, v in werte.items() if v) + ">>"
    typ, ref = doc.xref_get_key(-1, "Info")
    if typ == "xref":
        doc.update_object(_ref(ref), info)
    else:
        neu = doc.get_new_xref()
        doc.update_object(neu, info)
        doc.xref_set_key(-1, "Info", f"{neu} 0 R")
    doc.del_xml_metadata()
    for xref in [doc.pdf_catalog()] + [s.xref for s in doc]:
        for key in ("PieceInfo", "LastModified", "Metadata", "Info"):  # /Info gehört in den Trailer, nicht hierher
            if doc.xref_get_key(xref, key)[0] != "null":
                doc.xref_set_key(xref, key, "null")
    kennung = secrets.token_hex(16).upper()
    doc.xref_set_key(-1, "ID", f"[<{kennung}><{kennung}>]")


# ---------------------------------------------------------------- Ablauf


def _neutraler_kopf(daten: bytes, version: str) -> bytes:
    """MuPDF schreibt `% Written by MuPDF <Version>`, eine eigene Binärmarke und immer PDF 1.7.
    Ersetzt wird byte-genau mit gleicher Länge, damit die Offsets der xref-Tabelle stimmen."""
    m = re.match(rb"%PDF-(\d\.\d)\n(%[^\n]*\n)(% Written by MuPDF[^\n]*\n)?", daten)
    if not m:
        return daten
    kopf = b"%PDF-" + (version.encode() if re.fullmatch(r"1\.[4-7]", version) else m.group(1)) + b"\n"
    marke = m.group(2)
    if len(marke) == 6:
        marke = b"%\xe2\xe3\xcf\xd3\n"  # übliche Binärmarke (Acrobat, Word, LibreOffice)
    kommentar = b"\n" * len(m.group(3) or b"")
    return kopf + marke + kommentar + daten[m.end():]


def _bilder(doc: pymupdf.Document) -> list[bytes]:
    return [s.get_pixmap(dpi=72).samples for s in doc]


def speichere(doc: pymupdf.Document, ausgabe: Path, sauber: bool = True,
              metadaten: dict[str, str] | None = None, version: str = "",
              herkunft: dict[str, set[str]] | None = None) -> list[str]:
    """Speichert das bearbeitete Dokument, mit `sauber` ohne Bearbeitungsspuren. `metadaten`
    überschreibt Titel, Autor, Thema und Stichwörter, `version` (z. B. "1.6") bleibt im Kopf
    erhalten. `herkunft` sagt je eingefügter Schrift, welche
    Originalschrift sie ersetzt (wichtig, wenn eine Ersatzschrift wie Liberation statt Arial kam)."""
    if not sauber:
        if metadaten:
            doc.set_metadata((doc.metadata or {}) | {k: v.strip() for k, v in metadaten.items() if k in METADATEN})
        doc.subset_fonts()  # sonst landet z. B. die komplette Arial (ca. 750 KB) im PDF
        doc.save(ausgabe, garbage=3, deflate=True)
        return []
    sicherung = doc.tobytes()
    vorher = _bilder(doc)
    doc.bake(annots=True, widgets=False)  # Links und Formularfelder bleiben
    for seite in doc:
        seite.clean_contents()  # ein Stream je Seite, einheitliche Schreibweise
    text = [_zeichen(s) for s in pymupdf.open("pdf", doc.tobytes())]  # neu geladen: inkl. eingebrannter Annotationen
    hinweise, umkodiert = vereinheitliche_schriften(doc, herkunft or {})
    _neutrale_xobjekte(doc)
    for seite in doc:
        entferne_leere_zustaende(seite)
    doc.subset_fonts()
    setze_metadaten(doc, metadaten)
    # Genau die geprüften Bytes schreiben: Ein zweites Speichern mit garbage=4 nummeriert die
    # Objekte im Speicher erneut um, dabei zeigte /Metadata einmal auf einen Seiteninhalt.
    daten = _neutraler_kopf(doc.tobytes(garbage=4, deflate=True, no_new_id=1), version)
    pruefung, original = pymupdf.open("pdf", daten), pymupdf.open("pdf", sicherung)
    if not all(_text_gleich(_zeichen(s), text[s.number]) and _glyphen_gleich(original[s.number], s)
               if s.number in umkodiert else s.get_pixmap(dpi=72).samples == vorher[s.number]
               for s in pruefung):
        speichere(pymupdf.open("pdf", sicherung), ausgabe, sauber=False, metadaten=metadaten)
        return ["WARNUNG: Bereinigung hätte das Druckbild verändert, deshalb ohne Bereinigung gespeichert"]
    Path(ausgabe).write_bytes(daten)
    return hinweise
