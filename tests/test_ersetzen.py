import sys
from pathlib import Path

import pymupdf
import pytest

from pdf_ersetzen import Regel, bearbeite, lies_regeln, norm, schriftdatei

# Testschrift: Arial (macOS, Windows) oder die metrisch gleiche Liberation Sans (Linux)
_arial = schriftdatei("Arial.ttf", "LiberationSans-Regular.ttf")
_arial_fett = schriftdatei("Arial Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf")
if not (_arial and _arial_fett):
    pytest.skip("Arial oder Liberation Sans nicht installiert", allow_module_level=True)
ARIAL, ARIAL_FETT = str(_arial), str(_arial_fett)
NUR_MAC = pytest.mark.skipif(sys.platform != "darwin", reason="braucht Helvetica.ttc von macOS")
POSTEN = [
    ("Beratung Workshop", "1.250,00 €"),
    ("Reisekosten", "86,40 €"),
    ("Lizenz 100 Nutzer", "1000,00 €"),
]


def rechnung(pfad: Path) -> Path:
    """Rechnungsähnliches PDF mit subset-eingebetteter Arial, rechtsbündiger Spalte, Linien."""
    doc = pymupdf.open()
    s = doc.new_page()
    s.insert_font("ar", fontfile=ARIAL)
    s.insert_font("arb", fontfile=ARIAL_FETT)
    s.draw_rect(pymupdf.Rect(60, 60, 540, 90), color=None, fill=(0.9, 0.93, 1.0))
    s.insert_text((70, 80), "Rechnung Nr. 2026-0412", fontname="arb", fontsize=14)
    s.insert_text((70, 120), "Muster GmbH, Hauptstraße 5, 20095 Hamburg", fontname="ar", fontsize=10)
    s.insert_text((70, 135), "Datum: 03.11.2026  Kundennr.: 4711", fontname="ar", fontsize=10)
    y = 180
    font = pymupdf.Font(fontfile=ARIAL)
    for text, betrag in POSTEN:
        s.insert_text((70, y), text, fontname="ar", fontsize=10)
        s.insert_text((520 - font.text_length(betrag, 10), y), betrag, fontname="ar", fontsize=10)
        s.draw_line((60, y + 6), (540, y + 6), color=(0.6, 0.6, 0.6), width=0.5)
        y += 20
    s.insert_text((70, y + 10), "Summe", fontname="arb", fontsize=10)
    summe = "2.336,40 €"
    fb = pymupdf.Font(fontfile=ARIAL_FETT)
    s.insert_text((520 - fb.text_length(summe, 10), y + 10), summe, fontname="arb", fontsize=10,
                  color=(0.7, 0, 0))
    doc.subset_fonts()
    doc.save(pfad)
    return pfad


def zeichen(pdf: Path) -> list[tuple[str, tuple]]:
    s = pymupdf.open(pdf)[0]
    return [
        (c["c"], tuple(round(v, 1) for v in c["bbox"]))
        for b in s.get_text("rawdict")["blocks"] for l in b.get("lines", [])
        for sp in l["spans"] for c in sp["chars"]
    ]


def text(pdf: Path) -> str:
    """Sichtbarer Text Zeile für Zeile nach Koordinaten, unabhängig von der Reihenfolge im PDF."""
    zeilen: dict[int, list] = {}
    for b in pymupdf.open(pdf)[0].get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                for c in sp["chars"]:
                    zeilen.setdefault(round(c["origin"][1]), []).append((c["origin"][0], c["c"]))
    return "\n".join(
        norm("".join(c for _, c in sorted(zeilen[y]))) for y in sorted(zeilen)
    )


def bbox_von(pdf: Path, suchtext: str) -> pymupdf.Rect:
    treffer = pymupdf.open(pdf)[0].search_for(suchtext)
    assert treffer, suchtext
    return treffer[0]


@pytest.fixture
def pdf(tmp_path):
    return rechnung(tmp_path / "rechnung.pdf")


def test_ersetzt_und_laesst_rest_unveraendert(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    vorher = zeichen(pdf)
    erg, fehlend, _ = bearbeite(pdf, aus, [Regel("Muster GmbH", "Beispiel AG")])
    assert not fehlend and len(erg) == 1
    t = text(aus)
    assert "Beispiel AG, Hauptstraße 5" in t and "Muster" not in t
    # Jedes Zeichen außerhalb der Fundstelle liegt exakt an derselben Stelle
    nachher = set(zeichen(aus))
    fremd = [z for z in vorher if z[1][1] > 125 or z[1][3] < 105]  # alle anderen Zeilen
    assert all(z in nachher for z in fremd)


def test_rechtsbuendiger_betrag_behaelt_rechte_kante(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    alt = bbox_von(pdf, "86,40 €")
    erg, _, _ = bearbeite(pdf, aus, [Regel("86,40 €", "1.186,40 €")])
    assert erg[0].fund.ausrichtung == "rechts"
    neu = bbox_von(aus, "1.186,40 €")
    assert abs(neu.x1 - alt.x1) < 0.5 and abs(neu.y1 - alt.y1) < 0.5
    assert "Reisekosten" in text(aus)


def test_wortgrenze_schuetzt_zahlen(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    erg, _, _ = bearbeite(pdf, aus, [Regel("100", "250")])
    assert len(erg) == 1  # nur "Lizenz 100", nicht in "1000,00"
    assert "Lizenz 250 Nutzer" in text(aus) and "1000,00 €" in text(aus)


def test_fehlende_glyphe_nutzt_passende_schrift(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    # "Q" und "y" kommen im Original nicht vor, das Subset hat sie nicht
    erg, _, _ = bearbeite(pdf, aus, [Regel("Hamburg", "Quickborn y")])
    assert erg[0].schrift in ("system", "original")
    assert "20095 Quickborn y" in text(aus)


def test_farbe_und_fett_bleiben(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    bearbeite(pdf, aus, [Regel("2.336,40 €", "2.416,40 €")])
    for b in pymupdf.open(aus)[0].get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                if "2.416,40" in sp["text"]:
                    assert sp["color"] == 0xB30000
                    assert "Bold" in sp["font"]
                    return
    pytest.fail("neuer Betrag nicht gefunden")


def test_hintergrund_und_linien_bleiben(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    bearbeite(pdf, aus, [Regel("2026-0412", "2026-0999"), Regel("1.250,00 €", "980,00 €")])
    assert len(pymupdf.open(aus)[0].get_drawings()) == len(pymupdf.open(pdf)[0].get_drawings())


def test_zu_langer_text_wird_gestaucht_statt_zu_ueberlappen(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    erg, _, _ = bearbeite(pdf, aus, [Regel("03.11.2026", "Dienstag, 3. November 2026")])
    assert erg[0].stauchung < 1
    assert any("WARNUNG" in n for n in erg[0].fund.notizen)  # 41 % ist unlesbar und muss auffallen
    kunde = bbox_von(aus, "Kundennr.")
    zeile = [c for b in pymupdf.open(aus)[0].get_text("rawdict")["blocks"] for l in b.get("lines", [])
             for sp in l["spans"] for c in sp["chars"] if abs(c["origin"][1] - 135) < 1 and c["c"].strip()]
    vor_kunde = [c for c in zeile if c["bbox"][0] < kunde.x0 - 0.5]
    assert all(c["bbox"][2] <= kunde.x0 + 0.5 for c in vor_kunde)


def test_nicht_gefunden_wird_gemeldet(pdf, tmp_path):
    _, fehlend, _ = bearbeite(pdf, tmp_path / "aus.pdf", [Regel("gibt es nicht", "x")])
    assert [r.alt for r in fehlend] == ["gibt es nicht"]


def test_regeldatei(tmp_path):
    f = tmp_path / "regeln.txt"
    f.write_text("# Kommentar\nalt => neu\n100 => 200 | rechts\n\n", encoding="utf-8")
    r = lies_regeln([], f)
    assert [(x.alt, x.neu, x.ausrichtung) for x in r] == [("alt", "neu", "auto"), ("100", "200", "rechts")]


def test_rest_des_textlaufs_wird_mitgezogen(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    alt_ende = bbox_von(pdf, "Hamburg")
    erg, _, _ = bearbeite(pdf, aus, [Regel("Muster GmbH", "Beispiel AG")])
    assert "Beispiel AG, Hauptstraße 5, 20095 Hamburg" in text(aus)  # keine Lücke vor dem Komma
    assert bbox_von(aus, "Hamburg").x1 < alt_ende.x1  # Rest rückt nach links auf
    assert erg[0].stauchung == 1


def test_zwei_treffer_im_selben_lauf(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    erg, fehlend, _ = bearbeite(pdf, aus, [Regel("Muster GmbH", "Beispiel AG"), Regel("Hamburg", "Kiel")])
    assert not fehlend and len(erg) == 2
    assert "Beispiel AG, Hauptstraße 5, 20095 Kiel" in text(aus)


def test_nachbarzeichen_bleiben_exakt_an_ihrer_position(pdf, tmp_path):
    """Regression: MuPDF-Redaction schob den Rest der Zeile um eine Glyphenbreite nach links."""
    aus = tmp_path / "aus.pdf"
    vorher = bbox_von(pdf, "Kundennr.")
    _, _, korrekturen = bearbeite(pdf, aus, [Regel("03.11.2026", "14.11.2026")])
    nachher = bbox_von(aus, "Kundennr.")
    assert abs(nachher.x0 - vorher.x0) < 0.3 and abs(nachher.y1 - vorher.y1) < 0.3
    assert korrekturen  # die Korrektur war tatsächlich nötig


def test_ausgabe_bleibt_klein(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    bearbeite(pdf, aus, [Regel("Hamburg", "Quickborn")])
    assert aus.stat().st_size < pdf.stat().st_size * 1.5


@pytest.fixture
def helvetica_pdf(tmp_path):
    """Nicht eingebettete Standardschrift Helvetica, wie sie viele Rechnungsprogramme nutzen."""
    doc = pymupdf.open()
    s = doc.new_page()
    s.insert_text((70, 100), "Gesamt: 86,40", fontname="helv", fontsize=10)
    pfad = tmp_path / "helv.pdf"
    doc.save(pfad)
    return pfad


@NUR_MAC
def test_euro_in_helvetica_wird_korrekt_gesetzt(helvetica_pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    erg, _, _ = bearbeite(helvetica_pdf, aus, [Regel("86,40", "112,80 €")])
    assert erg[0].schrift == "system"  # echte Helvetica aus Helvetica.ttc, nicht Arial
    assert "Gesamt: 112,80 €" in text(aus)
    assert not any("WARNUNG" in n for n in erg[0].fund.notizen)


@pytest.fixture
def zweiseitig(tmp_path):
    """Gleicher Betrag dreimal: zweimal auf Seite 1, einmal als Übertrag auf Seite 2."""
    doc = pymupdf.open()
    for zeilen in (["Zwischensumme 1.336,40 €", "Summe 1.336,40 €"], ["Übertrag 1.336,40 €"]):
        s = doc.new_page()
        s.insert_font("ar", fontfile=ARIAL)
        for i, z in enumerate(zeilen):
            s.insert_text((70, 100 + 20 * i), z, fontname="ar", fontsize=10)
    pfad = tmp_path / "zwei.pdf"
    doc.save(pfad)
    return pfad


def seitentext(pdf: Path, nr: int) -> str:
    return norm(pymupdf.open(pdf)[nr - 1].get_text())


def test_stelle_aendert_nur_angeklickten_treffer(zweiseitig, tmp_path):
    aus = tmp_path / "aus.pdf"
    summe = pymupdf.open(zweiseitig)[0].search_for("1.336,40")[1]  # zweiter Treffer auf S. 1
    punkt = (1, summe.x0 + 1, (summe.y0 + summe.y1) / 2)
    erg, fehlend, _ = bearbeite(zweiseitig, aus, [Regel("1.336,40 €", "999,00 €", stellen=[punkt])])
    assert not fehlend and len(erg) == 1 and erg[0].fund.seite == 1
    assert "Zwischensumme 1.336,40 €" in text(aus) and "Summe 999,00 €" in text(aus)
    assert "1.336,40" in seitentext(aus, 2)  # Übertrag auf Seite 2 unverändert


def test_ohne_stelle_aendert_alle_vorkommen(zweiseitig, tmp_path):
    aus = tmp_path / "aus.pdf"
    erg, _, _ = bearbeite(zweiseitig, aus, [Regel("1.336,40 €", "999,00 €")])
    assert len(erg) == 3 and "999,00" in seitentext(aus, 2)


def test_stelle_hat_vorrang_vor_ueberall_regel(zweiseitig, tmp_path):
    aus = tmp_path / "aus.pdf"
    r = pymupdf.open(zweiseitig)[1].search_for("1.336,40")[0]
    erg, _, _ = bearbeite(zweiseitig, aus, [
        Regel("1.336,40 €", "1,00 €"),
        Regel("1.336,40 €", "2,00 €", stellen=[(2, r.x0 + 1, (r.y0 + r.y1) / 2)]),
    ])
    assert "2,00" in seitentext(aus, 2) and "1,00" not in seitentext(aus, 2)
    assert seitentext(aus, 1).count("1,00 €") == 2


def test_stelle_im_regelformat(tmp_path):
    r = Regel("1.336,40 €", "999,00 €", "rechts", [(1, 120.5, 98.25), (2, 70.0, 100.0)])
    zeile = r.als_text()
    assert zeile == "1.336,40 € => 999,00 € | rechts | @1:120.5:98.2 @2:70.0:100.0"
    f = tmp_path / "r.txt"
    f.write_text(zeile + "\nA => B | C\n", encoding="utf-8")
    a, b = lies_regeln([], f)
    assert (a.alt, a.neu, a.ausrichtung) == ("1.336,40 €", "999,00 €", "rechts")
    assert a.stellen == [(1, 120.5, 98.2), (2, 70.0, 100.0)]
    assert (b.neu, b.stellen) == ("B | C", [])  # unbekannter Zusatz bleibt Teil des Texts


# ---------------------------------------------------------------- Einfügen

from pdf_ersetzen import Einfuegung, Schriften, stil_an  # noqa: E402


def stil_von(pdf: Path, nr: int, suchtext: str) -> dict:
    doc = pymupdf.open(pdf)
    r = doc[nr - 1].search_for(suchtext)[0]
    return stil_an(doc[nr - 1], (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, Schriften(doc))


def span_mit(pdf: Path, nr: int, text: str) -> dict:
    for b in pymupdf.open(pdf)[nr - 1].get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                if text in norm(sp["text"]):
                    return sp
    raise AssertionError(f"{text!r} nicht gefunden")


def test_stil_der_vorlage(pdf):
    s = stil_von(pdf, 1, "2.336,40")
    assert s["stil"]["size"] == 10 and s["stil"]["color"] == 0xB30000 and "Bold" in s["stil"]["font"]
    assert 0.95 < s["skalierung"] < 1.05
    assert s["grundlinie"] == pytest.approx(250, abs=0.5)  # y der Summenzeile


def test_eingefuegter_text_sieht_aus_wie_vorlage(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    s = stil_von(pdf, 1, "Reisekosten")
    e = Einfuegung(1, 70, 300, "Zusatzleistung Schulung", s["stil"], s["skalierung"])
    bearbeite(pdf, aus, [], einfuegungen=[e])
    neu, vorlage = span_mit(aus, 1, "Zusatzleistung"), span_mit(aus, 1, "Reisekosten")
    assert neu["size"] == vorlage["size"] and neu["color"] == vorlage["color"]
    assert basis_familie(neu["font"]) == basis_familie(vorlage["font"])
    assert neu["origin"] == pytest.approx((70, 300), abs=0.2)
    assert e.rect is not None and e.schrift in ("original", "system", "standard")


def basis_familie(name: str) -> str:
    return name.split("+")[-1].lower().replace(" ", "").replace("regular", "").replace("mt", "")


def test_einfuegen_rechtsbuendig_und_auf_anderer_seite(zweiseitig, tmp_path):
    aus = tmp_path / "aus.pdf"
    s = stil_von(zweiseitig, 1, "1.336,40")
    vorlage = pymupdf.open(zweiseitig)[0].search_for("Summe 1.336,40 €")[0]
    e = Einfuegung(2, vorlage.x1, 140, "50,00 €", s["stil"], s["skalierung"], "rechts")
    erg, _, _ = bearbeite(zweiseitig, aus, [], einfuegungen=[e])
    assert erg == []  # nur eingefügt, nichts ersetzt
    r = pymupdf.open(aus)[1].search_for("50,00 €")[0]
    assert r.x1 == pytest.approx(vorlage.x1, abs=0.5)
    assert "Übertrag 1.336,40 €" in seitentext(aus, 2)


def test_einfuegen_zusammen_mit_ersetzen(pdf, tmp_path):
    aus = tmp_path / "aus.pdf"
    s = stil_von(pdf, 1, "Reisekosten")
    erg, _, _ = bearbeite(pdf, aus, [Regel("Reisekosten", "Fahrtkosten")],
                          einfuegungen=[Einfuegung(1, 70, 300, "Neue Zeile", s["stil"], s["skalierung"])])
    assert len(erg) == 1 and "Fahrtkosten" in text(aus) and "Neue Zeile" in text(aus)



@NUR_MAC
def test_schnitte_aus_schriftsammlung():
    from pdf_ersetzen import systemschrift
    schluessel, puffer = systemschrift("Helvetica-Bold")
    assert schluessel.endswith(".ttc#1")
    font = pymupdf.Font(fontbuffer=puffer)
    assert font.name == "Helvetica Bold"
    assert font.text_length("€", 10) == pytest.approx(7.44, abs=0.01)  # Arial hätte 5,56
    assert systemschrift("GibtEsNicht-Regular") is None
