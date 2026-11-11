import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pymupdf
import pytest

from pdf_ersetzen import oberflaeche
from test_ersetzen import rechnung


@pytest.fixture(scope="module")
def basis():
    server = ThreadingHTTPServer(("127.0.0.1", 0), oberflaeche.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def anfrage(url, daten=None, typ="application/json"):
    req = urllib.request.Request(url, data=daten, method="POST" if daten is not None else "GET")
    if daten is not None:
        req.add_header("Content-Type", typ)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.fixture
def sitzung(basis, tmp_path):
    pdf = rechnung(tmp_path / "rechnung.pdf").read_bytes()
    status, _, rumpf = anfrage(f"{basis}/api/oeffnen?name=Rechnung%20April.pdf", pdf, "application/pdf")
    assert status == 200
    return json.loads(rumpf)


def test_startseite(basis):
    status, kopf, rumpf = anfrage(f"{basis}/")
    assert status == 200 and "text/html" in kopf["Content-Type"] and b"PDF ersetzen" in rumpf


def test_oeffnen_liefert_seiten(sitzung):
    assert sitzung["name"] == "Rechnung April.pdf"
    assert sitzung["mitText"] and len(sitzung["seiten"]) == 1
    assert sitzung["seiten"][0]["breite"] == pytest.approx(595, abs=1)


def test_woerter_ergeben_exakten_suchtext(basis, sitzung):
    _, _, rumpf = anfrage(f"{basis}/api/woerter?id={sitzung['id']}&nr=1")
    daten = json.loads(rumpf)
    w = next(w for w in daten["woerter"] if daten["zeilen"][w["z"]][w["a"]:w["b"]] == "86,40")
    assert len(w["r"]) == 4
    zeile = next(z for z in daten["zeilen"] if z.startswith("Muster"))
    assert zeile.startswith("Muster GmbH, Hauptstraße")  # normalisierte Leerzeichen


def test_vorschau_und_download(basis, sitzung):
    sid = sitzung["id"]
    regeln = {"regeln": [{"alt": "86,40 €", "neu": "112,80 €"}, {"alt": "gibt es nicht", "neu": "x"}]}
    status, _, rumpf = anfrage(f"{basis}/api/vorschau?id={sid}", json.dumps(regeln).encode())
    assert status == 200
    daten = json.loads(rumpf)
    assert len(daten["regeln"][0]["treffer"]) == 1 and daten["regeln"][1]["treffer"] == []
    assert daten["regeln"][0]["treffer"][0]["ausrichtung"] == "rechts"

    status, kopf, png = anfrage(f"{basis}/api/seite?id={sid}&nr=1&stand=neu")
    assert status == 200 and png.startswith(b"\x89PNG")

    status, kopf, pdf = anfrage(f"{basis}/api/download?id={sid}")
    assert status == 200 and "Rechnung%20April_bearbeitet.pdf" in kopf["Content-Disposition"]
    assert "112,80" in pymupdf.open("pdf", pdf)[0].get_text()


def test_leere_regeln_setzen_zurueck(basis, sitzung):
    sid = sitzung["id"]
    anfrage(f"{basis}/api/vorschau?id={sid}", json.dumps({"regeln": [{"alt": "Summe", "neu": "Total"}]}).encode())
    anfrage(f"{basis}/api/vorschau?id={sid}", json.dumps({"regeln": []}).encode())
    status, _, _ = anfrage(f"{basis}/api/download?id={sid}")
    assert status == 409


def test_fehlerfaelle(basis, sitzung):
    assert anfrage(f"{basis}/api/oeffnen?name=x.pdf", b"kein pdf", "application/pdf")[0] == 400
    assert anfrage(f"{basis}/api/woerter?id=falsch&nr=1")[0] == 404
    assert anfrage(f"{basis}/api/seite?id={sitzung['id']}&nr=99")[0] == 400
    status, _, rumpf = anfrage(f"{basis}/api/seite?id=falsch&nr=1")
    assert status == 404 and "neu öffnen" in json.loads(rumpf)["fehler"]


def test_vorschau_mit_stelle(basis, tmp_path):
    from test_ersetzen import ARIAL
    doc = pymupdf.open()
    for _ in range(2):
        s = doc.new_page()
        s.insert_font("ar", fontfile=ARIAL)
        s.insert_text((70, 100), "Summe 50,00 €", fontname="ar", fontsize=10)
    _, _, rumpf = anfrage(f"{basis}/api/oeffnen?name=z.pdf", doc.tobytes(), "application/pdf")
    sid = json.loads(rumpf)["id"]
    r = pymupdf.open("pdf", doc.tobytes())[0].search_for("50,00")[0]
    regel = {"alt": "50,00 €", "neu": "60,00 €", "stellen": [[1, r.x0 + 1, (r.y0 + r.y1) / 2]]}
    _, _, rumpf = anfrage(f"{basis}/api/vorschau?id={sid}", json.dumps({"regeln": [regel]}).encode())
    treffer = json.loads(rumpf)["regeln"][0]["treffer"]
    assert [t["seite"] for t in treffer] == [1]


def test_stil_und_einfuegen(basis, sitzung):
    sid = sitzung["id"]
    _, _, rumpf = anfrage(f"{basis}/api/woerter?id={sid}&nr=1")
    daten = json.loads(rumpf)
    assert all({"y", "x0", "x1"} <= set(l) for l in daten["linien"])
    w = next(w for w in daten["woerter"] if daten["zeilen"][w["z"]][w["a"]:w["b"]] == "Reisekosten")
    x, y = (w["r"][0] + w["r"][2]) / 2, (w["r"][1] + w["r"][3]) / 2
    status, _, rumpf = anfrage(f"{basis}/api/stil?id={sid}&nr=1&x={x}&y={y}")
    assert status == 200
    vorlage = json.loads(rumpf)
    assert vorlage["stil"]["size"] == 10

    e = {"seite": 1, "x": 70, "y": 300, "text": "Neue Position", **vorlage}
    _, _, rumpf = anfrage(f"{basis}/api/vorschau?id={sid}", json.dumps({"regeln": [], "einfuegungen": [e]}).encode())
    erg = json.loads(rumpf)["einfuegungen"][0]
    assert erg["rect"][0] == pytest.approx(70, abs=0.1) and erg["schrift"]
    _, _, pdf = anfrage(f"{basis}/api/download?id={sid}")
    assert "Neue Position" in oberflaeche.norm(pymupdf.open("pdf", pdf)[0].get_text())

    assert anfrage(f"{basis}/api/stil?id={sid}&nr=1&x=5&y=5")[0] == 404  # leere Ecke
