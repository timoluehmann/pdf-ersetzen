"""Saubere Ausgabe: keine Bearbeitungsspuren, gleiche Optik, gleicher Text."""
import subprocess
import re
from pathlib import Path

import pymupdf
import pytest

from pdf_ersetzen import Regel, bearbeite
from pdf_ersetzen import bereinigen
from test_ersetzen import ARIAL, rechnung, text


def schriften(pdf: Path) -> list[str]:
    return sorted(name for s in pymupdf.open(pdf) for _x, _e, _t, name, *_ in s.get_fonts())


def aliase(pdf: Path) -> set[str]:
    return {alias for s in pymupdf.open(pdf) for *_, alias, _enc in s.get_fonts()}


@pytest.fixture
def vorbearbeitet(tmp_path):
    """Rechnung, die schon zweimal mit anderen Werkzeugen nachbearbeitet wurde:
    Revisionen, Metadaten-Historie, eine FreeText-Annotation und ein Link."""
    pfad = rechnung(tmp_path / "rechnung.pdf")
    doc = pymupdf.open(pfad)
    doc.set_metadata({"producer": "Microsoft Word", "creator": "Microsoft Word",
                      "creationDate": "D:20261103091500+01'00'", "modDate": "D:20261103091500+01'00'"})
    doc[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(70, 110, 200, 122),
                        "uri": "https://example.com"})
    doc.saveIncr()
    doc = pymupdf.open(pfad)
    doc[0].add_freetext_annot(pymupdf.Rect(70, 300, 300, 320), "Zahlbar bis 30.11.2026", fontsize=10)
    doc.set_metadata({**doc.metadata, "producer": "Smallpdf", "modDate": "D:20261110143000+01'00'"})
    doc.saveIncr()
    return pfad


def test_saubere_ausgabe_hat_keine_bearbeitungsspuren(vorbearbeitet, tmp_path):
    aus = tmp_path / "aus.pdf"
    hinweise: list[str] = []
    bearbeite(vorbearbeitet, aus, [Regel("86,40 €", "112,80 €"), Regel("2.336,40 €", "2.362,80 €")],
              hinweise=hinweise)
    roh = aus.read_bytes()
    doc = pymupdf.open(aus)
    assert hinweise == []
    assert roh.count(b"%%EOF") == 1 and doc.xref_get_key(-1, "Prev")[0] == "null"
    assert b"MuPDF" not in roh and not doc.is_repaired  # kein Bibliotheksname, xref noch gültig
    assert roh.startswith(vorbearbeitet.read_bytes()[:9])  # PDF-Version wie im Original
    teile = re.findall(r"<([0-9A-F]+)>", doc.xref_get_key(-1, "ID")[1])
    assert len(teile) == 2 and teile[0] == teile[1]
    meta = doc.metadata
    assert not meta["producer"] and not meta["creator"]
    assert not meta["creationDate"] and not meta["modDate"]
    assert "Date" not in doc.xref_object(int(doc.xref_get_key(-1, "Info")[1].split()[0]))  # auch kein /…Date null
    assert doc.get_xml_metadata() == ""
    seite = doc[0]
    assert len(seite.get_contents()) == 1
    assert list(seite.annots()) == []
    assert len(seite.get_links()) == 1
    assert not any(name.startswith("Annot") for _x, name, *_ in seite.get_xobjects())
    assert not any(a.startswith("PE") for a in aliase(aus))
    # keine Schriftkopie dazu (Helvetica stammt aus der eingebrannten Annotation)
    assert [n for n in schriften(aus) if n != "Helvetica"] == schriften(vorbearbeitet)
    assert "112,80 €" in text(aus) and "2.362,80 €" in text(aus) and "86,40" not in text(aus)
    assert "Zahlbar bis 30.11.2026" in seite.get_text()


def test_gleiche_optik_wie_ohne_bereinigung(vorbearbeitet, tmp_path):
    """„F“ kommt im Original-Subset nicht vor, steht aber in dessen ToUnicode-Tabelle. Früher
    wurde darauf umkodiert und das F war unsichtbar, obwohl der Text stimmte."""
    sauber, roh = tmp_path / "sauber.pdf", tmp_path / "roh.pdf"
    regeln = [Regel("Reisekosten", "Fahrtkosten")]
    bearbeite(vorbearbeitet, roh, regeln, sauber=False)
    bearbeite(vorbearbeitet, sauber, regeln)
    a, b = (pymupdf.open(p)[0] for p in (roh, sauber))
    assert bereinigen._text_gleich(bereinigen._zeichen(a), bereinigen._zeichen(b), toleranz=0.02)
    assert bereinigen._glyphen_gleich(a, b)
    assert a.get_pixmap(dpi=72).samples == b.get_pixmap(dpi=72).samples


def test_ohne_bereinigung_bleibt_wie_bisher(vorbearbeitet, tmp_path):
    aus = tmp_path / "aus.pdf"
    bearbeite(vorbearbeitet, aus, [Regel("86,40 €", "112,80 €")], sauber=False)
    assert any(a.startswith("PE") for a in aliase(aus))
    assert pymupdf.open(aus).metadata["producer"] == "Smallpdf"


def word_artig(pfad: Path, zeilen: list[str]) -> Path:
    """PDF wie aus Word: einfache TrueType-Schrift mit WinAnsi-Kodierung, ganzzahlig gerundete
    Breiten nur für genutzte Zeichen, Text als Literal-Strings."""
    font = pymupdf.Font(fontfile=ARIAL)
    genutzt = {c for z in zeilen for c in z.encode("cp1252")}
    breiten = [round(font.glyph_advance(ord(bytes([c]).decode("cp1252"))) * 1000) if c in genutzt else 0
               for c in range(32, 256)]
    doc = pymupdf.open()
    seite = doc.new_page()
    datei = doc.get_new_xref()
    doc.update_object(datei, "<<>>")
    doc.update_stream(datei, Path(ARIAL).read_bytes())
    deskriptor = doc.get_new_xref()
    doc.update_object(deskriptor, f"<</Type/FontDescriptor/FontName/BCDEEE+ArialMT/Flags 32/FontBBox[-665 -325 2000 1006]"
                                  f"/ItalicAngle 0/Ascent 905/Descent -212/CapHeight 716/StemV 80/FontFile2 {datei} 0 R>>")
    schrift = doc.get_new_xref()
    doc.update_object(schrift, f"<</Type/Font/Subtype/TrueType/BaseFont/BCDEEE+ArialMT/FirstChar 32/LastChar 255"
                               f"/Widths[{' '.join(map(str, breiten))}]/Encoding/WinAnsiEncoding/FontDescriptor {deskriptor} 0 R>>")
    doc.xref_set_key(seite.xref, "Resources", f"<</Font<</F1 {schrift} 0 R>>>>")
    inhalt = b""
    for i, z in enumerate(zeilen):
        lit = z.encode("cp1252").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        inhalt += b"BT /F1 10 Tf 70 %d Td (" % (720 - 20 * i) + lit + b") Tj ET\n"
    strom = doc.get_new_xref()
    doc.update_object(strom, "<<>>")
    doc.update_stream(strom, inhalt)
    doc.xref_set_key(seite.xref, "Contents", f"{strom} 0 R")
    doc.save(pfad)
    return pfad


def test_word_schrift_wird_wiederverwendet(tmp_path):
    pdf = word_artig(tmp_path / "word.pdf", ["Rechnung 2026-0412", "Reisekosten 86,40 €", "Summe 1.250,00 €"])
    sauber, roh = tmp_path / "sauber.pdf", tmp_path / "roh.pdf"
    regeln = [Regel("86,40 €", "68,04 €")]
    hinweise: list[str] = []
    bearbeite(pdf, roh, regeln, sauber=False)
    bearbeite(pdf, sauber, regeln, hinweise=hinweise)
    assert hinweise == []
    assert schriften(sauber) == ["BCDEEE+ArialMT"]  # nur die Originalschrift
    assert "Reisekosten 68,04 €" in text(sauber)
    # gleiche Zeichen an gleicher Stelle wie die unbereinigte Fassung (Breiten-Rundung ausgeglichen)
    z_roh, z_sauber = (bereinigen._zeichen(pymupdf.open(p)[0]) for p in (roh, sauber))
    assert bereinigen._text_gleich(z_roh, z_sauber, toleranz=0.02)


def test_fehlendes_zeichen_wird_gemeldet(tmp_path):
    pdf = word_artig(tmp_path / "word.pdf", ["Reisekosten 86,40 €"])
    aus = tmp_path / "aus.pdf"
    hinweise: list[str] = []
    bearbeite(pdf, aus, [Regel("86,40 €", "97,40 €")], hinweise=hinweise)  # 9 und 7 kommen im Original nicht vor
    assert len(hinweise) == 1 and "„79“" in hinweise[0]
    assert "97,40 €" in text(aus)
    assert not any(a.startswith("PE") for a in aliase(aus))  # auch die Kopie trägt keinen Werkzeugnamen


def test_abweichung_faellt_auf_unbereinigt_zurueck(vorbearbeitet, tmp_path, monkeypatch):
    def kaputt(seite):  # simuliert eine Bereinigung, die Inhalt zerstört
        seite.parent.update_stream(seite.get_contents()[0], b"")
    monkeypatch.setattr(bereinigen, "entferne_leere_zustaende", kaputt)
    aus = tmp_path / "aus.pdf"
    hinweise: list[str] = []
    bearbeite(vorbearbeitet, aus, [Regel("86,40 €", "112,80 €")], hinweise=hinweise)
    assert hinweise and hinweise[0].startswith("WARNUNG")
    assert "112,80 €" in text(aus)


def test_dokumentangaben_ueberschreiben(vorbearbeitet, tmp_path):
    aus = tmp_path / "aus.pdf"
    bearbeite(vorbearbeitet, aus, [Regel("86,40 €", "112,80 €")],
              metadaten={"title": "Rechnung (April)", "author": "Jürgen Köhler", "keywords": ""})
    meta = pymupdf.open(aus).metadata
    assert meta["title"] == "Rechnung (April)" and meta["author"] == "Jürgen Köhler"
    assert not meta["keywords"] and not meta["creationDate"]


@pytest.fixture
def cff_pdf(tmp_path):
    """OpenType-Schrift mit CFF-Umrissen (wie Hausschriften aus InDesign). Trade Gothic ist
    lizenzpflichtig und nur lokal installiert, in der CI werden diese Tests übersprungen."""
    from test_ersetzen import _pdf_mit_postscript_namen, _trade_gothic

    schriften = _trade_gothic("Trade Gothic LT Std Bold No. 2.otf")
    if not schriften:
        pytest.skip("keine CFF-Testschrift (Trade Gothic LT Std) installiert")
    return _pdf_mit_postscript_namen(tmp_path / "cff.pdf", schriften)


def test_cff_schrift_wird_wiederverwendet(cff_pdf, tmp_path):
    sauber, roh = tmp_path / "sauber.pdf", tmp_path / "roh.pdf"
    regeln = [Regel("100,00", "110,00")]  # alle Ziffern stecken schon im Original-Subset
    hinweise: list[str] = []
    bearbeite(cff_pdf, roh, regeln, sauber=False)
    bearbeite(cff_pdf, sauber, regeln, hinweise=hinweise)
    assert hinweise == []
    assert len(schriften(sauber)) == 1
    assert "Summe 110,00" in text(sauber)
    a, b = (pymupdf.open(p)[0] for p in (roh, sauber))
    assert bereinigen._text_gleich(bereinigen._zeichen(a), bereinigen._zeichen(b), toleranz=0.02)
    assert bereinigen._glyphen_gleich(a, b)


def test_cff_fehlende_zeichen_werden_genannt(cff_pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    hinweise: list[str] = []
    bearbeite(cff_pdf, aus, [Regel("100,00", "987,45")], hinweise=hinweise)
    assert len(hinweise) == 1 and "„45789“" in hinweise[0]
    assert "Summe 987,45" in text(aus)


def test_ganz_verdraengtes_original_wird_nicht_gemeldet(cff_pdf, tmp_path):
    """Wird die ganze Zeile neu gesetzt, bleibt nur eine Schrift übrig: keine Doppelung, kein Hinweis."""
    aus = tmp_path / "aus.pdf"
    hinweise: list[str] = []
    bearbeite(cff_pdf, aus, [Regel("Summe", "Muse")], hinweise=hinweise)
    assert hinweise == [] and len(schriften(aus)) == 1


def test_cff_glyphen_erkennung():
    """Subset aus geleerten Glyphen (nur `endchar`) und echten Umrissen."""
    from test_ersetzen import _trade_gothic

    schriften_ = _trade_gothic("Trade Gothic LT Std Bold No. 2.otf")
    if not schriften_:
        pytest.skip("keine CFF-Testschrift installiert")
    voll = bereinigen._cff_glyphen_mit_umriss(schriften_[0].read_bytes())
    assert voll and len(voll) > 200  # vollständige Schrift: praktisch alle Glyphen mit Umriss
    assert bereinigen._cff_glyphen_mit_umriss(b"kein CFF") is None


# ---- Einfache CFF-Schriften (Type 1C), wie InDesign Hausschriften einbettet ----

def _cff_aus_otf(otf: Path) -> bytes:
    import struct
    d = otf.read_bytes()
    for i in range(struct.unpack(">H", d[4:6])[0]):
        tag = d[12 + 16 * i:16 + 16 * i]
        off, ln = struct.unpack(">II", d[20 + 16 * i:28 + 16 * i])
        if tag == b"CFF ":
            return d[off:off + ln]
    raise AssertionError("keine CFF-Tabelle in der OTF")


def _type1c_pdf(pfad: Path, otf: Path, zeilen: list[str], differences: bool = False) -> Path:
    """PDF mit einfacher Type-1C-Schrift (eine Byte-Kodierung, CFF-Umrisse), wie InDesign sie
    erzeugt: WinAnsi oder ein /Differences-Array mit Adobe-Glyphennamen."""
    cff = _cff_aus_otf(otf)
    font = pymupdf.Font(fontfile=str(otf))
    doc = pymupdf.open()
    s = doc.new_page()
    datei = doc.get_new_xref()
    doc.update_object(datei, "<</Subtype/Type1C>>")
    doc.update_stream(datei, cff)
    desk = doc.get_new_xref()
    doc.update_object(desk, f"<</Type/FontDescriptor/FontName/ABCDEF+TradeGothicLTStd-Bd2/Flags 4"
                            f"/FontBBox[-167 -229 1000 977]/ItalicAngle 0/Ascent 722/Descent -278"
                            f"/StemV 80/FontFile3 {datei} 0 R>>")
    genutzt = {c for z in zeilen for c in z.encode("cp1252")}
    breiten = [round(font.glyph_advance(ord(bytes([c]).decode("cp1252"))) * 1000)
               if 32 <= c < 256 and c in genutzt else 0 for c in range(32, 256)]
    if differences:
        namen = {ord(" "): "space", ord(","): "comma"}
        namen |= {ord(c): c for c in "SumeRiskotn"}
        namen |= {ord(str(i)): n for i, n in enumerate("zero one two three four five six seven eight nine".split())}
        gebraucht = {c for z in zeilen for c in map(ord, z)} & set(namen)
        diff = "[ " + " ".join(f"{c} /{namen[c]}" for c in sorted(gebraucht)) + " ]"
        enc = doc.get_new_xref()
        doc.update_object(enc, f"<</Type/Encoding/Differences {diff}>>")
        encref = f"{enc} 0 R"
    else:
        encref = "/WinAnsiEncoding"
    schrift = doc.get_new_xref()
    doc.update_object(schrift, f"<</Type/Font/Subtype/Type1/BaseFont/ABCDEF+TradeGothicLTStd-Bd2"
                               f"/FirstChar 32/LastChar 255/Widths[{' '.join(map(str, breiten))}]"
                               f"/Encoding {encref}/FontDescriptor {desk} 0 R>>")
    doc.xref_set_key(s.xref, "Resources", f"<</Font<</F1 {schrift} 0 R>>>>")
    inhalt = b""
    for i, z in enumerate(zeilen):
        lit = z.encode("cp1252").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        inhalt += b"BT /F1 11 Tf 70 %d Td (" % (720 - 20 * i) + lit + b") Tj ET\n"
    strom = doc.get_new_xref()
    doc.update_object(strom, "<<>>")
    doc.update_stream(strom, inhalt)
    doc.xref_set_key(s.xref, "Contents", f"{strom} 0 R")
    doc.save(pfad)
    return pfad


@pytest.fixture
def type1c_otf():
    from test_ersetzen import _trade_gothic
    s = _trade_gothic("Trade Gothic LT Std Bold No. 2.otf")
    if not s:
        pytest.skip("keine CFF-Testschrift (Trade Gothic LT Std) installiert")
    return s[0]


@pytest.mark.parametrize("differences", [False, True], ids=["winansi", "differences"])
def test_einfache_cff_schrift_wird_wiederverwendet(type1c_otf, tmp_path, differences):
    pdf = _type1c_pdf(tmp_path / "t1c.pdf", type1c_otf, ["Summe 100,00", "Reisekosten 86,40"], differences)
    assert "Type 1C" in subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True).stdout
    sauber, roh = tmp_path / "sauber.pdf", tmp_path / "roh.pdf"
    regeln = [Regel("100,00", "110,00")]  # alle Ziffern stecken schon im Subset
    hinweise: list[str] = []
    bearbeite(pdf, roh, regeln, sauber=False)
    bearbeite(pdf, sauber, regeln, hinweise=hinweise)
    assert hinweise == []
    assert len(schriften(sauber)) == 1  # keine zweite Schriftkopie
    assert "Summe 110,00" in text(sauber)
    a, b = (pymupdf.open(p)[0] for p in (roh, sauber))
    assert bereinigen._text_gleich(bereinigen._zeichen(a), bereinigen._zeichen(b), toleranz=0.02)
    assert bereinigen._glyphen_gleich(a, b)


def test_einfache_cff_fehlende_zeichen_werden_genannt(type1c_otf, tmp_path):
    pdf = _type1c_pdf(tmp_path / "t1c.pdf", type1c_otf, ["Summe 100,00"])
    aus = tmp_path / "aus.pdf"
    hinweise: list[str] = []
    bearbeite(pdf, aus, [Regel("100,00", "987,65")], hinweise=hinweise)  # 9 8 7 6 5 fehlen im Subset
    assert len(hinweise) == 1 and "„56789“" in hinweise[0]
    assert "Summe 987,65" in text(aus)


def test_glyphname_aufloesung():
    z = bereinigen._glyph_zu_zeichen
    assert z("comma") == "," and z("zero") == "0" and z("A") == "A" and z("space") == " "
    assert z("germandbls") == "ß" and z("adieresis") == "ä" and z("Euro") == "€"
    assert z("uni20AC") == "€" and z("uni00FC") == "ü"
    assert z("gibtsnicht") is None


# ---- Echte Type-1-Schriften (PostScript, FontFile), ohne externe Werkzeuge erzeugt ----

def _t1_num(n: int) -> bytes:
    import struct
    if -107 <= n <= 107:
        return bytes([n + 139])
    if 108 <= n <= 1131:
        n -= 108
        return bytes([(n >> 8) + 247, n & 0xFF])
    if -1131 <= n <= -108:
        n = -n - 108
        return bytes([(n >> 8) + 251, n & 0xFF])
    return b"\xff" + struct.pack(">i", n)


def _t1_encrypt(data: bytes, r: int, skip: int) -> bytes:
    out = bytearray()
    for byte in bytes(skip) + data:
        c = byte ^ (r >> 8)
        out.append(c)
        r = ((c + r) * 52845 + 22719) & 0xFFFF
    return bytes(out)


# WinAnsi-Byte -> Adobe-Glyphenname für die Zeichen der Testdaten
_T1_ENC = {32: "space", 44: "comma", 46: "period"}
_T1_ENC |= {ord(c): c for c in "SummeRiskotn"}
_T1_ENC |= {48 + i: n for i, n in enumerate("zero one two three four five six seven eight nine".split())}


def _type1_font(name: str, glyphen: dict[str, tuple[int, int]]) -> tuple[bytes, int, int, int]:
    """Echte Type-1-Schrift (eexec-verschlüsselt). Jede Glyphe ein gefülltes Rechteck (Breite, Höhe)."""
    HSBW, RLINETO, RMOVETO, CLOSEPATH, ENDCHAR = 13, 5, 21, 9, 14

    def box(w, h):
        x0, x1 = 60, max(120, w - 60)
        return (_t1_num(0) + _t1_num(w) + bytes([HSBW]) + _t1_num(x0) + _t1_num(0) + bytes([RMOVETO])
                + _t1_num(0) + _t1_num(h) + bytes([RLINETO]) + _t1_num(x1 - x0) + _t1_num(0) + bytes([RLINETO])
                + _t1_num(0) + _t1_num(-h) + bytes([RLINETO]) + bytes([CLOSEPATH, ENDCHAR]))

    klar = [f"%!PS-AdobeFont-1.0: {name} 001.000", "11 dict begin",
            "/FontInfo 2 dict dup begin", f"/FullName ({name}) def", "end def",
            f"/FontName /{name} def", "/PaintType 0 def /FontType 1 def",
            "/FontMatrix [0.001 0 0 0.001 0 0] def", "/FontBBox {0 -200 1000 800} readonly def",
            "/Encoding 256 array", "0 1 255 {1 index exch /.notdef put} for"]
    for code, g in _T1_ENC.items():
        if g in glyphen:
            klar.append(f"dup {code} /{g} put")
    klar += ["readonly def", "currentdict end", "currentfile eexec"]
    klartext = ("\n".join(klar) + "\n").encode("latin-1")

    alle = {".notdef": None, **glyphen}
    priv = ["dup /Private 15 dict dup begin",
            "/RD {string currentfile exch readstring pop} executeonly def",
            "/ND {noaccess def} executeonly def", "/NP {noaccess put} executeonly def",
            "/BlueValues [] def", "/MinFeature {16 16} ND", "/password 5839 def",
            f"2 index /CharStrings {len(alle)} dict dup begin"]
    teile = [("\n".join(priv) + "\n").encode("latin-1")]
    for g, maße in alle.items():
        cs = (_t1_num(0) + _t1_num(0) + bytes([HSBW, ENDCHAR])) if maße is None else box(*maße)
        enc = _t1_encrypt(cs, 4330, 4)
        teile.append(f"/{g} {len(enc)} RD ".encode("latin-1") + enc + b" ND\n")
    teile.append(b"end end\nreadonly put\nnoaccess put\ndup/FontName get exch definefont pop\nmark currentfile closefile\n")
    eexec = _t1_encrypt(b"".join(teile), 55665, 4)
    trailer = (b"0" * 64 + b"\n") * 8 + b"cleartomark\n"
    return klartext + eexec + trailer, len(klartext), len(eexec), len(trailer)


def _type1_pdf(pfad: Path, zeilen: list[str]) -> Path:
    """PDF mit einfacher Type-1-Schrift (FontFile, PostScript), wie alte Satzsysteme sie erzeugen."""
    chars = set("".join(zeilen))
    glyphen, breiten_pt = {}, {}
    for c in sorted(chars):
        if (name := _T1_ENC.get(ord(c))):
            w = 300 if c in " ,." else 560
            glyphen[name] = (w, 500 + (ord(c) % 5) * 40)
            breiten_pt[c] = w
    ff, l1, l2, l3 = _type1_font("ABCDEF+TestGothicStd", glyphen)
    doc = pymupdf.open()
    s = doc.new_page()
    datei = doc.get_new_xref()
    doc.update_object(datei, f"<</Length1 {l1}/Length2 {l2}/Length3 {l3}>>")
    doc.update_stream(datei, ff)
    desk = doc.get_new_xref()
    doc.update_object(desk, f"<</Type/FontDescriptor/FontName/ABCDEF+TestGothicStd/Flags 4"
                            f"/FontBBox[0 -200 1000 800]/ItalicAngle 0/Ascent 800/Descent -200"
                            f"/StemV 80/FontFile {datei} 0 R>>")
    breiten = [round(breiten_pt.get(bytes([c]).decode("latin-1"), 0)) for c in range(32, 256)]
    schrift = doc.get_new_xref()
    doc.update_object(schrift, f"<</Type/Font/Subtype/Type1/BaseFont/ABCDEF+TestGothicStd/FirstChar 32"
                               f"/LastChar 255/Widths[{' '.join(map(str, breiten))}]/Encoding/WinAnsiEncoding"
                               f"/FontDescriptor {desk} 0 R>>")
    doc.xref_set_key(s.xref, "Resources", f"<</Font<</F1 {schrift} 0 R>>>>")
    inhalt = b""
    for i, z in enumerate(zeilen):
        inhalt += b"BT /F1 11 Tf 70 %d Td (" % (720 - 20 * i) + z.encode("latin-1") + b") Tj ET\n"
    strom = doc.get_new_xref()
    doc.update_object(strom, "<<>>")
    doc.update_stream(strom, inhalt)
    doc.xref_set_key(s.xref, "Contents", f"{strom} 0 R")
    doc.save(pfad)
    return pfad


def test_type1_pdf_ist_echte_type1_schrift(tmp_path):
    pdf = _type1_pdf(tmp_path / "t1.pdf", ["Summe 100,00"])
    zeile = [z for z in subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True).stdout.splitlines()
             if "TestGothic" in z][0]
    assert "Type 1 " in zeile and "Type 1C" not in zeile  # FontFile, nicht FontFile3
    assert pymupdf.open(pdf)[0].get_text().strip() == "Summe 100,00"


def test_type1_schrift_wird_wiederverwendet(tmp_path):
    pdf = _type1_pdf(tmp_path / "t1.pdf", ["Summe 100,00", "Reisekosten 86,40"])
    sauber, roh = tmp_path / "sauber.pdf", tmp_path / "roh.pdf"
    regeln = [Regel("100,00", "110,00")]  # 1 und 0 stecken schon im Subset
    hinweise: list[str] = []
    bearbeite(pdf, roh, regeln, sauber=False)
    bearbeite(pdf, sauber, regeln, hinweise=hinweise)
    assert hinweise == []
    assert len(schriften(sauber)) == 1  # keine zweite Schrift
    assert "Summe 110,00" in text(sauber)
    a, b = (pymupdf.open(p)[0] for p in (roh, sauber))
    assert bereinigen._glyphen_gleich(a, b)  # pixelgleich zur unbereinigten Fassung


def test_type1_fehlende_zeichen_bleiben_ersatzschrift(tmp_path):
    pdf = _type1_pdf(tmp_path / "t1.pdf", ["Summe 100,00"])
    aus = tmp_path / "aus.pdf"
    hinweise: list[str] = []
    bearbeite(pdf, aus, [Regel("100,00", "987,65")], hinweise=hinweise)  # 9 8 7 5 fehlen im Subset
    assert len(hinweise) == 1 and "bleibt zusätzlich" in hinweise[0]
    assert "Summe 987,65" in text(aus)


def test_cid_type1_kopie_wird_erkannt(tmp_path):
    """Die beim Einfügen entstehende Kopie ist CIDFontType0 mit FontFile (Type 1), nicht FontFile3."""
    pdf = _type1_pdf(tmp_path / "t1.pdf", ["Summe 100,00"])
    roh = tmp_path / "roh.pdf"
    bearbeite(pdf, roh, [Regel("100,00", "110,00")], sauber=False)
    doc = pymupdf.open(roh)
    kopien = [bereinigen._cid_schrift(doc, e[0]) for e in doc[0].get_fonts(full=True) if e[4].startswith("PE")]
    assert kopien and all(k and k["prog"] == "type1" for k in kopien)
