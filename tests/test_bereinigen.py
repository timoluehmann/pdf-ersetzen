"""Saubere Ausgabe: keine Bearbeitungsspuren, gleiche Optik, gleicher Text."""
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
    assert [n for n in schriften(aus) if "Arial" in n] == schriften(vorbearbeitet)  # keine Schriftkopie dazu
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
