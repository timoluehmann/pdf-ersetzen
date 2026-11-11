"""Lokale Oberfläche für pdf-ersetzen: kleiner HTTP-Server (nur Standardbibliothek + PyMuPDF).

Start: `pdf-ersetzen-ui` oder `uv run pdf-ersetzen-ui`. Läuft ausschließlich auf 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pymupdf

from .kern import Einfuegung, Regel, Schriften, bearbeite, norm, stil_an, zeilen_der_seite

STATISCH = Path(__file__).parent / "ui"
MAX_GROESSE = 100 * 1024 * 1024
DPI = 144


class Sitzung:
    def __init__(self, name: str, daten: bytes):
        self.ordner = Path(tempfile.mkdtemp(prefix="pdf-ersetzen-"))
        self.name = name
        self.original = self.ordner / "original.pdf"
        self.original.write_bytes(daten)
        self.ergebnis: Path | None = None
        self.version = 0
        self.sperre = threading.Lock()

    def aufraeumen(self):
        shutil.rmtree(self.ordner, ignore_errors=True)


SITZUNGEN: dict[str, Sitzung] = {}


def seiten_info(pfad: Path) -> list[dict]:
    with pymupdf.open(pfad) as doc:
        return [{"breite": s.rect.width, "hoehe": s.rect.height} for s in doc]


def woerter(pfad: Path, nr: int) -> dict:
    """Wörter mit Zeichenbereich je Zeile. Der Client setzt daraus den exakten Suchtext zusammen."""
    with pymupdf.open(pfad) as doc:
        seite = doc[nr - 1]
        zeilen, liste, linien = [], [], []
        for li, zeile in enumerate(zeilen_der_seite(seite)):
            text = norm("".join(z.c for z in zeile)).replace("\xad", "-")
            zeilen.append(text)
            sichtbar = [z for z in zeile if z.c.strip()]
            if sichtbar:
                linien.append({"z": li, "y": sichtbar[0].origin.y, "x0": sichtbar[0].bbox.x0, "x1": sichtbar[-1].bbox.x1})
            for m in re.finditer(r"\S+", text):
                r = pymupdf.Rect(zeile[m.start()].bbox)
                for z in zeile[m.start() + 1 : m.end()]:
                    r |= z.bbox
                liste.append({"z": li, "a": m.start(), "b": m.end(), "r": [r.x0, r.y0, r.x1, r.y1]})
        return {"zeilen": zeilen, "woerter": liste, "linien": linien}


def render(pfad: Path, nr: int) -> bytes:
    with pymupdf.open(pfad) as doc:
        return doc[nr - 1].get_pixmap(dpi=DPI).tobytes("png")


def stil(s: Sitzung, nr: int, x: float, y: float) -> dict | None:
    with pymupdf.open(s.original) as doc:
        return stil_an(doc[nr - 1], x, y, Schriften(doc))


def vorschau(s: Sitzung, daten: dict) -> dict:
    regeln = [
        Regel(r["alt"], r.get("neu", ""), r.get("ausrichtung", "auto"),
              [(int(st[0]), float(st[1]), float(st[2])) for st in r.get("stellen") or []])
        for r in daten.get("regeln", [])
        if r.get("alt")
    ]
    einfuegungen = [
        Einfuegung(int(e["seite"]), float(e["x"]), float(e["y"]), e.get("text", ""), e["stil"],
                   float(e.get("skalierung", 1.0)), e.get("ausrichtung", "links"))
        for e in daten.get("einfuegungen", [])
    ]
    with s.sperre:
        if not regeln and not any(e.text.strip() for e in einfuegungen):
            s.ergebnis = None
            s.version += 1
            return {"version": s.version, "regeln": [], "einfuegungen": [{} for _ in einfuegungen], "korrekturen": {}}
        ziel = s.ordner / f"ergebnis_{s.version + 1}.pdf"
        ergebnisse, _, korrekturen = bearbeite(s.original, ziel, regeln, bool(daten.get("teilstring")),
                                               einfuegungen=einfuegungen)
        if s.ergebnis and s.ergebnis.exists():
            s.ergebnis.unlink()
        s.ergebnis = ziel
        s.version += 1
    proregel = []
    for r in regeln:
        treffer = [e for e in ergebnisse if e.fund.regel is r]
        proregel.append({
            "treffer": [
                {
                    "seite": e.fund.seite,
                    "rect": list(e.fund.neu_rect or e.fund.rect),
                    "ausrichtung": e.fund.ausrichtung,
                    "schrift": e.schrift,
                    "stauchung": round(e.stauchung, 3),
                    "notizen": e.fund.notizen,
                }
                for e in treffer
            ]
        })
    eingefuegt = [
        {"rect": list(e.rect) if e.rect else None, "schrift": e.schrift, "notizen": e.notizen}
        for e in einfuegungen
    ]
    return {"version": s.version, "regeln": proregel, "einfuegungen": eingefuegt, "korrekturen": korrekturen}


class Handler(BaseHTTPRequestHandler):
    server_version = "pdf-ersetzen"

    def log_message(self, *_):  # ruhig bleiben
        pass

    # -------------------------------------------------- Antworten

    def senden(self, status: int, inhalt: bytes, typ: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(inhalt)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(inhalt)

    def json(self, daten, status: int = 200):
        self.senden(status, json.dumps(daten, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def fehler(self, status: int, meldung: str):
        self.json({"fehler": meldung}, status)

    def sitzung(self, q: dict) -> Sitzung | None:
        s = SITZUNGEN.get(q.get("id", [""])[0])
        if not s:
            self.fehler(HTTPStatus.NOT_FOUND, "Sitzung abgelaufen. Bitte PDF neu öffnen.")
        return s

    def seitennr(self, q: dict, s: Sitzung) -> int | None:
        try:
            nr = int(q.get("nr", ["1"])[0])
        except ValueError:
            nr = 0
        with pymupdf.open(s.original) as doc:
            if 1 <= nr <= doc.page_count:
                return nr
        self.fehler(HTTPStatus.BAD_REQUEST, "Ungültige Seite")
        return None

    # -------------------------------------------------- Routen

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            return self.senden(200, (STATISCH / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/seite":
            s = self.sitzung(q)
            if not s or not (nr := self.seitennr(q, s)):
                return
            stand = q.get("stand", ["original"])[0]
            pfad = s.ergebnis if stand == "neu" and s.ergebnis else s.original
            return self.senden(200, render(pfad, nr), "image/png")
        if url.path == "/api/woerter":
            s = self.sitzung(q)
            if not s or not (nr := self.seitennr(q, s)):
                return
            return self.json(woerter(s.original, nr))
        if url.path == "/api/stil":
            s = self.sitzung(q)
            if not s or not (nr := self.seitennr(q, s)):
                return
            try:
                x, y = float(q["x"][0]), float(q["y"][0])
            except (KeyError, ValueError):
                return self.fehler(HTTPStatus.BAD_REQUEST, "x und y fehlen")
            ergebnis = stil(s, nr, x, y)
            if not ergebnis:
                return self.fehler(HTTPStatus.NOT_FOUND, "An dieser Stelle steht kein Text.")
            return self.json(ergebnis)
        if url.path == "/api/download":
            s = self.sitzung(q)
            if not s:
                return
            if not s.ergebnis:
                return self.fehler(HTTPStatus.CONFLICT, "Noch keine Änderung")
            name = Path(s.name).stem + "_bearbeitet.pdf"
            return self.senden(200, s.ergebnis.read_bytes(), "application/pdf", {
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"
            })
        self.fehler(HTTPStatus.NOT_FOUND, "Nicht gefunden")

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge > MAX_GROESSE:
            return self.fehler(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Datei größer als 100 MB")
        rumpf = self.rfile.read(laenge)

        if url.path == "/api/oeffnen":
            name = q.get("name", ["dokument.pdf"])[0]
            if not rumpf.startswith(b"%PDF"):
                return self.fehler(HTTPStatus.BAD_REQUEST, "Das ist keine PDF-Datei.")
            try:
                with pymupdf.open("pdf", rumpf) as doc:
                    if doc.needs_pass:
                        return self.fehler(HTTPStatus.BAD_REQUEST, "Das PDF ist passwortgeschützt.")
                    mit_text = any(s.get_text("text").strip() for s in doc)
            except Exception:
                return self.fehler(HTTPStatus.BAD_REQUEST, "Das PDF lässt sich nicht lesen.")
            for alt in list(SITZUNGEN.values()):  # Einzelplatz: alte Sitzungen verwerfen
                alt.aufraeumen()
            SITZUNGEN.clear()
            sid = secrets.token_urlsafe(12)
            s = SITZUNGEN[sid] = Sitzung(name, rumpf)
            return self.json({"id": sid, "name": name, "seiten": seiten_info(s.original), "mitText": mit_text})

        if url.path == "/api/vorschau":
            s = self.sitzung(q)
            if not s:
                return
            try:
                daten = json.loads(rumpf or b"{}")
                return self.json(vorschau(s, daten))
            except Exception as e:  # Fehler sichtbar machen statt still scheitern
                return self.fehler(HTTPStatus.INTERNAL_SERVER_ERROR, f"Ersetzen fehlgeschlagen: {e}")

        self.fehler(HTTPStatus.NOT_FOUND, "Nicht gefunden")


def freier_port(wunsch: int) -> int:
    for port in [wunsch, *range(wunsch + 1, wunsch + 20)]:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("Kein freier Port gefunden")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pdf-ersetzen-ui")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--kein-browser", action="store_true")
    a = p.parse_args(argv)
    port = freier_port(a.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"pdf-ersetzen läuft auf {url}  (Beenden mit Ctrl+C)", flush=True)
    if not a.kein_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in SITZUNGEN.values():
            s.aufraeumen()
    return 0


if __name__ == "__main__":
    sys.exit(main())
