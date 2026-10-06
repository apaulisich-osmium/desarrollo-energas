#!/usr/bin/env python3
"""
Extractor ENARGAS - Proyección Semanal de la Demanda del Sistema de Transporte.

Fuente: https://www.enargas.gob.ar/secciones/transporte-y-distribucion/dod-proyeccion-semanal.php

Flujo:
  1. Lee la página y detecta los botones de descarga (archivos PS_AAAAMMDD.pdf).
  2. Descarga cada PDF (queda cacheado en ./data/pdfs).
  3. Parsea el PDF por coordenadas: cada número se asigna a la columna (día) más cercana.
     - Días anteriores a la fecha del archivo: columna "Programado" + columna "Real".
     - Días desde la fecha del archivo en adelante: "Proyección".
  4. Acumula: cada corrida agrega solo los archivos nuevos a ./data/proyeccion_semanal.csv
     (formato largo, una fila por dato), así la historia se conserva aunque ENARGAS
     retire los PDFs viejos de la página.
  5. Consolida la serie desde --desde (default 01/10/2026): por día y concepto, el último
     valor Real informado y la última proyección publicada -> ./data/serie_acumulada.csv
  6. Genera el dashboard ./data/proyeccion_semanal.html y, opcionalmente, vuelca a Google Sheets.

Uso:
  python enargas_proyeccion_semanal.py                 # descarga, parsea, CSV + HTML
  python enargas_proyeccion_semanal.py --sheets        # además escribe en Google Sheets
  python enargas_proyeccion_semanal.py --pdf archivo.pdf   # parsea un PDF local
  python enargas_proyeccion_semanal.py --desde 2026-09-28  # cambia el inicio de la serie
"""
import argparse
import csv
import json
import re
import sys
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path

import pdfplumber
import requests

BASE = "https://www.enargas.gob.ar/secciones/transporte-y-distribucion/"
PAGE_URL = BASE + "dod-proyeccion-semanal.php"
PDF_URL = BASE + "descarga.php?tipo=&path=proyeccion-semanal&file={file}"

SPREADSHEET_ID = "1-h5ulfg4xmuG6MxiT2iT2qBc3Axii0tI-HQeDt3tu4s"
WORKSHEET_GID = 1194975652

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PDF_DIR = DATA_DIR / "pdfs"
CSV_PATH = DATA_DIR / "proyeccion_semanal.csv"
SERIE_PATH = DATA_DIR / "serie_acumulada.csv"
HTML_PATH = DATA_DIR / "proyeccion_semanal.html"

# inicio de la serie acumulada (los archivos previos quedan en el CSV crudo)
DESDE_DEFAULT = "2026-10-01"
SERIE_SHEET = "Serie consolidada"

HEADERS = {"User-Agent": "Mozilla/5.0 (extractor ENARGAS proyeccion semanal)"}

MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}

COLUMNS = ["archivo", "fecha_archivo", "emision", "semana_proyectada", "fecha_dato",
           "tipo", "seccion", "subgrupo", "concepto", "unidad", "valor", "limites"]

NUM_RE = re.compile(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$")
DAY_RE = re.compile(r"^(lun|mar|mié|mie|jue|vie|sáb|sab|dom)$", re.I)

# Encabezados que abren una nueva sección (se compara contra el inicio del rótulo).
SECTION_STARTS = [
    ("Temperatura Media Estimada", "Temperatura"),
    ("DEMANDA TOTAL", "Demanda total"),
    ("A- DENTRO DE SISTEMAS DE TRANSPORTE", "A- Dentro de sistemas de transporte"),
    ("B- FUERA DE SISTEMAS DE TRANSPORTE", "B- Fuera de sistemas de transporte"),
    ("INYECCIONES (TGN + TGS + ENARSA)", "Inyecciones (TGN + TGS + ENARSA)"),
    ("BOLIVIA", "Otras inyecciones"),
    ("STOCK (TGN+TGS)", "Stock (linepack)"),
]


# --------------------------------------------------------------------------- web
def listar_archivos():
    """Devuelve [(archivo, fecha)] de los botones de la página, más reciente primero."""
    r = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    files = OrderedDict()
    for f in re.findall(r'DecargarPDF\([^)]*"(PS_\d{8}\.pdf)"\)', r.text):
        files[f] = datetime.strptime(f[3:11], "%Y%m%d").date()
    return list(files.items())


def descargar(archivo):
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    dest = PDF_DIR / archivo
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    r = requests.get(PDF_URL.format(file=archivo), headers=HEADERS, timeout=60)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"{archivo}: la respuesta no es un PDF")
    dest.write_bytes(r.content)
    return dest


# --------------------------------------------------------------------------- parser
def to_num(s):
    return float(s.replace(".", "").replace(",", "."))


def agrupar_lineas(words, tol=3.0):
    """Agrupa palabras en líneas según su coordenada vertical."""
    lines = []
    for w in sorted(words, key=lambda w: w["top"]):
        if lines and abs(w["top"] - lines[-1]["top"]) <= tol:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
    return lines


def fecha_con_anio(dia, mes, ref):
    """Arma la fecha usando el año de referencia, corrigiendo el cruce de año."""
    d = date(ref.year, mes, dia)
    if (d - ref).days > 180:
        d = date(ref.year - 1, mes, dia)
    elif (ref - d).days > 180:
        d = date(ref.year + 1, mes, dia)
    return d


def parse_pdf(path):
    archivo = Path(path).name
    m = re.search(r"PS_(\d{8})", archivo)
    fecha_archivo = datetime.strptime(m.group(1), "%Y%m%d").date() if m else None

    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(extra_attrs=["fontname"])
    lines = agrupar_lineas(words)

    # --- encabezado de columnas: línea con los días ("vie 02 - oct ...") + "REAL"
    header = next(ln for ln in lines
                  if sum(1 for w in ln["words"] if DAY_RE.match(w["text"])) >= 3)
    hw = header["words"]
    ref = fecha_archivo or date.today()
    cols = []  # {x, fecha, tipo}
    i = 0
    while i < len(hw):
        w = hw[i]
        if DAY_RE.match(w["text"]) and i + 3 < len(hw):
            dia, mes = int(hw[i + 1]["text"]), MESES[hw[i + 3]["text"][:3].lower()]
            x0, x1 = w["x0"], hw[i + 3]["x1"]
            cols.append({"x": (x0 + x1) / 2, "fecha": fecha_con_anio(dia, mes, ref),
                         "tipo": "Proyección"})
            i += 4
        elif w["text"].upper() == "REAL" and cols:
            cols[-1]["tipo"] = "Programado"
            cols.append({"x": (w["x0"] + w["x1"]) / 2, "fecha": cols[-1]["fecha"],
                         "tipo": "Real"})
            i += 1
        else:
            i += 1
    if not cols:
        raise RuntimeError(f"{archivo}: no se encontró el encabezado de días")
    x_datos = min(c["x"] for c in cols) - 25  # todo lo que está a la izquierda es rótulo

    texto = " ".join(w["text"] for w in words)
    emision = re.search(r"FECHA:\s*(\w+\s+\d{1,2}\s*-\s*[a-z]+?)\s*(\d{1,2})\s*hs", texto)
    emision = f"{emision.group(1)} {emision.group(2)} hs" if emision else ""
    semana = re.search(r"PROYECCION SEMANA:\s*(.+?)\s+(?:REAL|vie|lun|mar|mié|jue|sáb|dom)\b", texto)
    semana = semana.group(1).strip() if semana else ""

    rows = []
    seccion, padre, ultimo_label, x_padre = "", "", "", 0
    for ln in lines:
        if ln is header or ln["top"] < header["top"] - 5:
            continue
        label_w = [w for w in ln["words"] if w["x1"] < x_datos]
        # rótulo vertical "Saldos" (sale como "odlaS", "dlaS" o letras sueltas)
        if label_w and (label_w[0]["text"] in "Saldos" or label_w[0]["text"] in "sodlaS"):
            label_w = label_w[1:]
        num_w = [w for w in ln["words"] if w["x0"] >= x_datos and NUM_RE.match(w["text"])]
        label = " ".join(w["text"] for w in label_w)

        limites = ""
        lm = re.search(r"(Min|MIN):?\s*(\d+)\s*-\s*(Max|MAX):?\s*(\d+)", label)
        if lm:
            limites = f"{lm.group(2)}-{lm.group(4)}"
            label = label[:lm.start()].strip()
        unidad = "°C" if "°C" in label else "MMm³"
        label = re.sub(r"\s*(MMm³|°C)\s*", " ", label).strip()

        if not label and num_w:
            label = ultimo_label  # números en una línea partida (p.ej. INYECCIÓN BUQUE ESCOBAR)
        if not label or not num_w:
            if label:
                ultimo_label = label
            continue
        ultimo_label = label

        for start, nombre in SECTION_STARTS:
            if label.upper().startswith(start.upper()):
                seccion, padre = nombre, ""
                break
        if label.startswith("DELTA"):
            seccion = "Stock (linepack)"
        if label.startswith("BUQUE REGASIFICADOR"):
            seccion, padre = "Saldos", ""

        # jerarquía: un rótulo bastante más indentado que el último rótulo de nivel superior
        # (Sur, Neuba I, GPFM, TGS/TGN de tramos finales...) cuelga de él
        indent = label_w[0]["x0"] if label_w else 0
        es_sub = bool(padre) and indent > x_padre + 40
        if not es_sub:
            es_padre = ((seccion.startswith("Inyecciones") and label in ("TGS", "TGN", "ENARSA"))
                        or (seccion == "Stock (linepack)" and label.startswith("TRAMOS FINALES")))
            padre, x_padre = (label, indent) if es_padre else ("", 0)
        subgrupo = padre if es_sub else ""

        for w in num_w:
            cx = (w["x0"] + w["x1"]) / 2
            col = min(cols, key=lambda c: abs(c["x"] - cx))
            rows.append({
                "archivo": archivo,
                "fecha_archivo": fecha_archivo.isoformat() if fecha_archivo else "",
                "emision": emision,
                "semana_proyectada": semana,
                "fecha_dato": col["fecha"].isoformat(),
                "tipo": col["tipo"],
                "seccion": seccion,
                "subgrupo": subgrupo,
                "concepto": label,
                "unidad": unidad,
                "valor": to_num(w["text"]),
                "limites": limites,
            })
    return rows


# --------------------------------------------------------------------------- salidas
def leer_csv():
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["valor"] = float(r["valor"])
    return rows


def guardar_csv(rows):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)




def consolidar(rows, desde):
    """Serie acumulada: un valor Real y uno Proyectado por (concepto, día), desde `desde`.

    Si varios archivos informan el mismo día, gana el más reciente (para el Real, el último
    dato informado; para el Proyectado, la última proyección/programación publicada).
    """
    if not rows:
        return [], []
    ultimo = max(r["archivo"] for r in rows)
    ult = [r for r in rows if r["archivo"] == ultimo]
    fmax = max(r["fecha_dato"] for r in ult)
    # el orden de los conceptos sigue al PDF: la última columna del último archivo los tiene todos
    orden = OrderedDict()
    for r in ult:
        if r["fecha_dato"] == fmax:
            orden[(r["seccion"], r["subgrupo"], r["concepto"])] = None
    meta = {}
    for r in sorted(rows, key=lambda r: r["archivo"]):
        k = (r["seccion"], r["subgrupo"], r["concepto"])
        orden.setdefault(k, None)
        meta[k] = {"unidad": r["unidad"], "limites": r["limites"]}

    serie = {}
    for r in sorted(rows, key=lambda r: r["archivo"]):
        if r["fecha_dato"] < desde:
            continue
        d = serie.setdefault((r["seccion"], r["subgrupo"], r["concepto"], r["fecha_dato"]), {})
        if r["tipo"] == "Real":
            d["real"], d["archivo_real"] = r["valor"], r["archivo"]
        else:
            d["proyectado"], d["archivo_proyectado"] = r["valor"], r["archivo"]

    conceptos = [{"id": "|".join(k), "seccion": k[0], "subgrupo": k[1], "concepto": k[2], **meta[k]}
                 for k in orden]
    filas = []
    for (sec, sub, con, fecha), d in sorted(serie.items(), key=lambda kv: kv[0][3]):
        real, proy = d.get("real"), d.get("proyectado")
        filas.append({"fecha": fecha, "seccion": sec, "subgrupo": sub, "concepto": con,
                      "unidad": meta[(sec, sub, con)]["unidad"],
                      "real": real, "proyectado": proy,
                      "desvio": round(real - proy, 2) if real is not None and proy is not None else None,
                      "archivo_real": d.get("archivo_real", ""),
                      "archivo_proyectado": d.get("archivo_proyectado", "")})
    return conceptos, filas


SERIE_COLUMNS = ["fecha", "seccion", "subgrupo", "concepto", "unidad", "real", "proyectado",
                 "desvio", "archivo_real", "archivo_proyectado"]


def guardar_serie(filas):
    with SERIE_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SERIE_COLUMNS)
        w.writeheader()
        w.writerows(filas)


def generar_html(rows, desde):
    """Dashboard estático con la serie acumulada y la vista de cada archivo original."""
    conceptos, filas = consolidar(rows, desde)
    serie = {}
    for f in filas:
        cid = "|".join((f["seccion"], f["subgrupo"], f["concepto"]))
        serie.setdefault(cid, {})[f["fecha"]] = [f["real"], f["proyectado"],
                                                f["archivo_real"], f["archivo_proyectado"]]

    archivos = OrderedDict()
    for archivo in sorted({r["archivo"] for r in rows}, reverse=True):
        rs = [r for r in rows if r["archivo"] == archivo]
        orden = {"Programado": 0, "Real": 1, "Proyección": 2}
        colkeys = sorted(OrderedDict.fromkeys((r["fecha_dato"], r["tipo"]) for r in rs),
                         key=lambda k: (k[0], orden[k[1]]))
        fs = OrderedDict()
        for r in rs:
            f = fs.setdefault("|".join((r["seccion"], r["subgrupo"], r["concepto"])), {"v": {}})
            f["v"][f"{r['fecha_dato']}|{r['tipo']}"] = r["valor"]
        archivos[archivo] = {"emision": rs[0]["emision"], "semana": rs[0]["semana_proyectada"],
                             "cols": [list(c) for c in colkeys],
                             "filas": {k: v["v"] for k, v in fs.items()}}

    data = {"desde": desde, "generado": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "fuente": PAGE_URL, "fechas": sorted({f["fecha"] for f in filas}),
            "conceptos": conceptos, "serie": serie, "archivos": archivos}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    HTML_PATH.write_text(HTML_TEMPLATE.replace("__DATA__", payload), encoding="utf-8")
    return filas


def volcar_a_sheets(rows, filas_serie, credenciales):
    """Agrega a la pestaña indicada los datos de archivos nuevos y reescribe 'Serie consolidada'."""
    import gspread

    gc = gspread.service_account(filename=credenciales)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.get_worksheet_by_id(WORKSHEET_GID)
    existentes = ws.get_all_values()
    if not existentes or existentes[0][:len(COLUMNS)] != COLUMNS:
        if existentes and any(any(c for c in fila) for fila in existentes):
            raise RuntimeError("La pestaña destino tiene datos con otro encabezado; "
                               "no se sobrescribe. Usá una pestaña vacía.")
        ws.update([COLUMNS], "A1")
        existentes = [COLUMNS]
    cargados = {fila[0] for fila in existentes[1:] if fila}
    nuevas = [[r[c] for c in COLUMNS] for r in rows if r["archivo"] not in cargados]
    if nuevas:
        ws.append_rows(nuevas, value_input_option="USER_ENTERED")

    try:
        ws_serie = sh.worksheet(SERIE_SHEET)
    except gspread.WorksheetNotFound:
        ws_serie = sh.add_worksheet(SERIE_SHEET, rows=len(filas_serie) + 10, cols=len(SERIE_COLUMNS))
    ws_serie.clear()
    ws_serie.update([SERIE_COLUMNS] + [["" if f[c] is None else f[c] for c in SERIE_COLUMNS]
                                       for f in filas_serie], "A1", value_input_option="USER_ENTERED")
    return len(nuevas), len({r["archivo"] for r in rows} - cargados)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", nargs="*", help="Parsear PDFs locales en lugar de descargar")
    ap.add_argument("--desde", default=DESDE_DEFAULT,
                    help=f"Inicio de la serie acumulada, AAAA-MM-DD (default: {DESDE_DEFAULT})")
    ap.add_argument("--sheets", action="store_true", help="Volcar a Google Sheets")
    ap.add_argument("--credenciales", default=str(ROOT / "credentials.json"),
                    help="JSON de la cuenta de servicio de Google (default: credentials.json)")
    args = ap.parse_args()

    todas = leer_csv()
    ya = {r["archivo"] for r in todas}

    if args.pdf:
        fuentes = [Path(p) for p in args.pdf]
    else:
        archivos = listar_archivos()
        print(f"{len(archivos)} archivos publicados: {', '.join(a for a, _ in archivos)}")
        fuentes = [descargar(a) for a, _ in archivos]

    for p in fuentes:
        if p.name in ya:
            print(f"  {p.name}: ya procesado")
            continue
        rows = parse_pdf(p)
        todas.extend(rows)
        print(f"  {p.name}: {len(rows)} datos extraídos")

    todas.sort(key=lambda r: (r["archivo"], r["fecha_dato"]))
    guardar_csv(todas)
    filas_serie = generar_html(todas, args.desde)
    guardar_serie(filas_serie)
    print(f"Acumulado: {len({r['archivo'] for r in todas})} archivos, serie desde {args.desde}")
    print(f"CSV crudo:  {CSV_PATH}\nCSV serie:  {SERIE_PATH}\nDashboard:  {HTML_PATH}")

    if args.sheets:
        n, a = volcar_a_sheets(todas, filas_serie, args.credenciales)
        print(f"Google Sheets: {n} filas nuevas de {a} archivo(s); '{SERIE_SHEET}' actualizada")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard Proyección Semanal</title>
<style>
:root{color-scheme:light;
--bg:#f4f4f2;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#77756f;--line:#e4e3de;--grid:#ecebe7;
--accent:#2a78d6;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--realbg:#e8f1fc;--zone:#f1f0ec;--sec:#efeee9;
--good:#1a7f37;--bad:#c4302b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--bg:#111110;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#97968d;--line:#2e2e2b;--grid:#262624;
--accent:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--realbg:#17263a;--zone:#21211f;--sec:#242422;
--good:#4fbf6b;--bad:#ef6b66}}
:root[data-theme="dark"]{color-scheme:dark;
--bg:#111110;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#97968d;--line:#2e2e2b;--grid:#262624;
--accent:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--realbg:#17263a;--zone:#21211f;--sec:#242422;
--good:#4fbf6b;--bad:#ef6b66}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:24px 16px 40px}
h1{font-size:22px;margin:0 0 2px;letter-spacing:-.01em}
h2{font-size:15px;margin:0}
.sub{color:var(--ink2);margin:0 0 16px}
a{color:var(--accent)}
.filters{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;
  padding:10px 0;margin-bottom:12px;background:var(--bg);border-bottom:1px solid var(--line)}
.filters label{color:var(--ink2);font-size:13px;display:flex;gap:6px;align-items:center}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--surface)}
.seg button{font:inherit;font-size:13px;border:0;background:none;color:var(--ink2);padding:6px 12px;cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed="true"]{background:var(--accent);color:#fff}
select,input[type=date]{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;
  background:var(--surface);color:var(--ink)}
button.btn{font:inherit;font-size:13px;padding:5px 10px;border:1px solid var(--line);border-radius:6px;
  background:var(--surface);color:var(--ink);cursor:pointer}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .t{color:var(--ink2);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.kpi .v{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums;margin:2px 0}
.kpi .v small{font-size:13px;font-weight:400;color:var(--ink2)}
.kpi .d{font-size:12px;color:var(--ink2);font-variant-numeric:tabular-nums}
.up{color:var(--good)}.down{color:var(--bad)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:16px}
.card-h{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;margin-bottom:10px}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:3px 6px 3px 10px;font-size:13px}
.chip .sw{width:14px;height:0;border-top:2px solid}
.chip button{border:0;background:none;color:var(--muted);cursor:pointer;font-size:15px;line-height:1;padding:0 4px}
.legend{display:flex;gap:16px;color:var(--ink2);font-size:12px;margin-bottom:4px}
.legend svg{vertical-align:middle;margin-right:4px}
.note{color:var(--muted);font-size:12px}
#chart{position:relative;width:100%}
#chart svg{display:block;width:100%}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:8px 10px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.12);min-width:180px;display:none;z-index:3}
.tip .h{color:var(--ink2);margin-bottom:4px}
.tip .row{display:flex;align-items:center;gap:8px;justify-content:space-between}
.tip .row span:first-child{display:flex;align-items:center;gap:6px;color:var(--ink2)}
.tip strong{font-variant-numeric:tabular-nums;color:var(--ink)}
.scroll{overflow-x:auto;max-height:70vh;overflow-y:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th,td{padding:4px 8px;border-bottom:1px solid var(--grid);white-space:nowrap;text-align:right}
thead th{position:sticky;top:0;background:var(--surface);font-weight:600;z-index:1}
thead tr:nth-child(2) th{top:26px;font-weight:400;color:var(--ink2);font-size:12px}
th:first-child,td:first-child{text-align:left;position:sticky;left:0;background:var(--surface);z-index:2}
td.r,th.r{background:var(--realbg)}
tr.sec td{background:var(--sec);font-weight:600}
td.sub1{padding-left:24px}
td.na{color:var(--muted)}
.tabs{display:flex;gap:4px;margin-bottom:12px}
@media (max-width:640px){.kpi .v{font-size:22px}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Proyección Semanal de la Demanda del Sistema de Transporte</h1>
  <p class="sub" id="sub"></p>

  <div class="filters">
    <label>Mostrar
      <span class="seg" role="group" aria-label="Tipo de dato" id="vista">
        <button type="button" data-v="ambos" aria-pressed="true">Real y Proyectado</button>
        <button type="button" data-v="real" aria-pressed="false">Real</button>
        <button type="button" data-v="proy" aria-pressed="false">Proyectado</button>
      </span>
    </label>
    <label>Desde <input type="date" id="f-desde"></label>
    <label>Hasta <input type="date" id="f-hasta"></label>
  </div>

  <div class="kpis" id="kpis"></div>

  <section class="card">
    <div class="card-h">
      <h2 id="chart-title">Evolución</h2>
      <div class="tools">
        <select id="c-add" aria-label="Agregar concepto"></select>
        <span class="note">hasta 3 conceptos de la misma unidad</span>
      </div>
    </div>
    <div class="chips" id="chips"></div>
    <div class="legend" id="legend"></div>
    <div id="chart"><div class="tip" id="tip"></div></div>
  </section>

  <section class="card">
    <div class="card-h">
      <h2>Datos acumulados</h2>
      <div class="tools">
        <label class="note">Sección <select id="t-sec"></select></label>
        <button class="btn" id="dl">Descargar CSV</button>
      </div>
    </div>
    <div class="scroll"><table id="tbl"></table></div>
  </section>

  <section class="card">
    <div class="card-h">
      <h2>Archivo original</h2>
      <div class="tools"><select id="a-sel" aria-label="Archivo"></select><span class="note" id="a-meta"></span></div>
    </div>
    <div class="scroll"><table id="a-tbl"></table></div>
  </section>

  <p class="note" id="foot"></p>
</div>
<script>
const DATA = __DATA__;
const $ = s => document.querySelector(s);
const DIAS = ["dom","lun","mar","mié","jue","vie","sáb"];
const SLOTS = ["--s1","--s2","--s3"];
const NF = new Intl.NumberFormat("es-AR",{minimumFractionDigits:1,maximumFractionDigits:2});
const fmt = v => v == null ? "—" : NF.format(v);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const dfmt = f => `${DIAS[new Date(f+"T12:00:00").getDay()]} ${f.slice(8)}/${f.slice(5,7)}`;
const C = Object.fromEntries(DATA.conceptos.map(c => [c.id, c]));
const nombre = c => c.subgrupo ? `${c.subgrupo} · ${c.concepto}` : c.concepto;
const val = (id, f, k) => { const s = DATA.serie[id] && DATA.serie[id][f]; return s ? s[k] : null; };  // k: 0 real, 1 proy
const store = { get(k){ try { return JSON.parse(localStorage.getItem("ps_"+k)); } catch(e){ return null; } },
                set(k,v){ try { localStorage.setItem("ps_"+k, JSON.stringify(v)); } catch(e){} } };

const findId = (sec, con) => (DATA.conceptos.find(c => c.seccion === sec && c.concepto === con && !c.subgrupo) || {}).id;
const ID_DEM = findId("Demanda total","DEMANDA TOTAL");
let vista = store.get("vista") || "ambos";
let elegidos = (store.get("elegidos") || [ID_DEM]).filter(id => C[id]);
if (!elegidos.length && DATA.conceptos.length) elegidos = [DATA.conceptos[0].id];
let desde = DATA.fechas[0] || "", hasta = DATA.fechas[DATA.fechas.length-1] || "";

const fechas = () => DATA.fechas.filter(f => f >= desde && f <= hasta);
const ultimoReal = id => [...DATA.fechas].reverse().find(f => val(id, f, 0) != null);

// ---------------------------------------------------------------- encabezado y filtros
const nArch = Object.keys(DATA.archivos).length;
$("#sub").innerHTML = `Operación gasoductos TGN + TGS · serie acumulada desde ${esc(DATA.desde.split("-").reverse().join("/"))} · ${nArch} archivos procesados · Fuente: <a href="${esc(DATA.fuente)}">ENARGAS</a>`;
$("#foot").textContent = `Volúmenes en MMm³/día salvo indicación. Real: último dato informado. Proyectado: última proyección publicada para ese día. Generado ${DATA.generado}.`;
["#f-desde","#f-hasta"].forEach(s => { $(s).min = DATA.fechas[0]; $(s).max = DATA.fechas[DATA.fechas.length-1]; });
$("#f-desde").value = desde; $("#f-hasta").value = hasta;
$("#f-desde").onchange = e => { desde = e.target.value || DATA.fechas[0]; render(); };
$("#f-hasta").onchange = e => { hasta = e.target.value || DATA.fechas[DATA.fechas.length-1]; render(); };
document.querySelectorAll("#vista button").forEach(b => b.onclick = () => { vista = b.dataset.v; store.set("vista", vista); render(); });

// selector de conceptos agrupado por sección
(function(){
  const sel = $("#c-add"); sel.innerHTML = `<option value="">+ Agregar concepto…</option>`;
  const secs = {}; DATA.conceptos.forEach(c => (secs[c.seccion] = secs[c.seccion] || []).push(c));
  Object.entries(secs).forEach(([s, cs]) => {
    const g = document.createElement("optgroup"); g.label = s;
    cs.forEach(c => g.appendChild(new Option(nombre(c) + (c.unidad !== "MMm³" ? ` (${c.unidad})` : ""), c.id)));
    sel.appendChild(g);
  });
  sel.onchange = () => {
    const id = sel.value; sel.value = ""; if (!id || elegidos.includes(id)) return;
    if (elegidos.length && C[elegidos[0]].unidad !== C[id].unidad) elegidos = [];   // un solo eje: misma unidad
    elegidos = [...elegidos, id].slice(-3); store.set("elegidos", elegidos); render();
  };
  const ts = $("#t-sec"); ts.add(new Option("Todas", ""));
  Object.keys(secs).forEach(s => ts.add(new Option(s, s)));
  ts.onchange = renderTabla;
})();

// ---------------------------------------------------------------- KPIs
function renderKpis(){
  const defs = [["Demanda total", ID_DEM],
                ["Inyecciones", findId("Inyecciones (TGN + TGS + ENARSA)","INYECCIONES (TGN + TGS + ENARSA)")],
                ["Stock linepack", findId("Stock (linepack)","STOCK (TGN+TGS)")],
                ["Temperatura Bs.As.", findId("Temperatura","Temperatura Media Estimada Anillo Bs.As.")]];
  const fs = fechas();
  $("#kpis").innerHTML = defs.filter(d => d[1]).map(([t, id]) => {
    const u = C[id].unidad;
    const fr = [...fs].reverse().find(f => val(id, f, 0) != null);
    const fp = [...fs].reverse().find(f => val(id, f, 1) != null);
    if (vista === "proy" || !fr) {   // sin dato real en el rango (p.ej. stock): se muestra lo proyectado
      if (!fp) return `<div class="kpi"><div class="t">${esc(t)}</div><div class="v">—</div></div>`;
      return `<div class="kpi"><div class="t">${esc(t)} · proyectado</div><div class="v">${fmt(val(id,fp,1))} <small>${esc(u)}</small></div><div class="d">${dfmt(fp)} · ${vista === "proy" ? "última fecha proyectada" : "no informa real"}</div></div>`;
    }
    if (!fr) return `<div class="kpi"><div class="t">${esc(t)}</div><div class="v">—</div><div class="d">sin datos reales en el rango</div></div>`;
    const r = val(id, fr, 0), p = val(id, fr, 1);
    let d = `${dfmt(fr)} · último real`;
    if (vista === "ambos" && p != null) {
      const dv = r - p, pct = p ? dv / Math.abs(p) * 100 : 0;
      d += `<br>Proyectado ${fmt(p)} · desvío <span class="${dv >= 0 ? "up" : "down"}">${dv >= 0 ? "▲ +" : "▼ "}${fmt(dv)} (${NF.format(pct)}%)</span>`;
    }
    return `<div class="kpi"><div class="t">${esc(t)} · real</div><div class="v">${fmt(r)} <small>${esc(u)}</small></div><div class="d">${d}</div></div>`;
  }).join("");
}

// ---------------------------------------------------------------- gráfico
function niceTicks(lo, hi, n){
  if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1,2,2.5,5,10].map(m => m * mag).find(s => s >= raw);
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, t = [];
  for (let v = a; v <= b + step / 2; v += step) t.push(+v.toFixed(6));
  return t;
}
const lineKey = (color, dashed) => `<svg width="18" height="8"><line x1="1" y1="4" x2="17" y2="4" stroke="${color}" stroke-width="2" ${dashed ? 'stroke-dasharray="4 3"' : ""}/></svg>`;

function renderChart(){
  const box = $("#chart"), tip = $("#tip");
  box.querySelectorAll("svg").forEach(s => s.remove()); tip.style.display = "none";
  $("#chips").innerHTML = elegidos.map((id, i) => `<span class="chip"><span class="sw" style="border-color:var(${SLOTS[i]})"></span>${esc(nombre(C[id]))}<button type="button" aria-label="Quitar" data-id="${esc(id)}">×</button></span>`).join("");
  $("#chips").querySelectorAll("button").forEach(b => b.onclick = () => { elegidos = elegidos.filter(x => x !== b.dataset.id); store.set("elegidos", elegidos); render(); });
  $("#chart-title").textContent = elegidos.length ? `Evolución (${C[elegidos[0]].unidad})` : "Evolución";
  const leg = [];
  if (vista !== "proy") leg.push(lineKey(css("--ink2"), false) + "Real");
  if (vista !== "real") leg.push(lineKey(css("--ink2"), true) + "Proyectado");
  $("#legend").innerHTML = leg.map(l => `<span>${l}</span>`).join("") + `<span>${'<svg width="14" height="10"><rect width="14" height="10" fill="' + css("--zone") + '" stroke="' + css("--line") + '"/></svg>'}Solo proyección</span>`;

  const fs = fechas();
  if (!elegidos.length || !fs.length) { box.insertAdjacentHTML("afterbegin", `<svg height="60"><text x="8" y="30" fill="${css("--muted")}">Elegí un concepto para graficar.</text></svg>`); return; }
  const series = [];
  elegidos.forEach((id, i) => {
    if (vista !== "proy") series.push({id, i, k: 0, label: `${nombre(C[id])} · real`});
    if (vista !== "real") series.push({id, i, k: 1, label: `${nombre(C[id])} · proyectado`});
  });
  const W = Math.max(320, box.clientWidth), H = W < 600 ? 260 : 340, m = {t: 14, r: 18, b: 30, l: 56};
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const vals = series.flatMap(s => fs.map(f => val(s.id, f, s.k))).filter(v => v != null);
  if (!vals.length) { box.insertAdjacentHTML("afterbegin", `<svg height="60"><text x="8" y="30" fill="${css("--muted")}">Sin datos para este rango.</text></svg>`); return; }
  const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.08 || 1;
  const ticks = niceTicks(lo - pad, hi + pad, 5), y0 = ticks[0], y1 = ticks[ticks.length-1];
  const x = i => m.l + (fs.length === 1 ? pw / 2 : i * pw / (fs.length - 1));
  const y = v => m.t + ph - (v - y0) / (y1 - y0) * ph;
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, a, p) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); (p || svg).appendChild(e); return e; };
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("height", H);
  svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Evolución de " + elegidos.map(id => nombre(C[id])).join(", "));
  box.insertBefore(svg, tip);

  // zona solo-proyección: después del último día con dato real de cualquier concepto elegido
  const lastReal = Math.max(...elegidos.map(id => fs.findIndex(f => f === ultimoReal(id))));
  if (lastReal >= 0 && lastReal < fs.length - 1)
    el("rect", {x: x(lastReal) + (x(lastReal+1) - x(lastReal)) / 2, y: m.t, width: m.l + pw - (x(lastReal) + (x(lastReal+1) - x(lastReal)) / 2), height: ph, fill: css("--zone")});
  else if (lastReal < 0) el("rect", {x: m.l, y: m.t, width: pw, height: ph, fill: css("--zone")});

  ticks.forEach(t => {
    el("line", {x1: m.l, x2: m.l + pw, y1: y(t), y2: y(t), stroke: css("--grid"), "stroke-width": 1});
    const tx = el("text", {x: m.l - 8, y: y(t) + 4, "text-anchor": "end", "font-size": 11, fill: css("--muted")}); tx.textContent = NF.format(t);
  });
  const step = Math.max(1, Math.ceil(fs.length / Math.max(1, Math.floor(pw / 64))));
  fs.forEach((f, i) => { if (i % step) return;
    const tx = el("text", {x: x(i), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: css("--muted")}); tx.textContent = f.slice(8) + "/" + f.slice(5,7); });

  const surface = css("--surface");
  series.forEach(s => {
    const color = css(SLOTS[s.i]); let d = "", pen = false;
    fs.forEach((f, i) => { const v = val(s.id, f, s.k); if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + x(i) + " " + y(v); pen = true; });
    el("path", {d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", ...(s.k ? {"stroke-dasharray": "5 4"} : {})});
    if (fs.length <= 45) fs.forEach((f, i) => { const v = val(s.id, f, s.k); if (v == null) return;
      el("circle", {cx: x(i), cy: y(v), r: 4, fill: s.k ? surface : color, stroke: s.k ? color : surface, "stroke-width": 2}); });
  });

  // capa de hover: crosshair que se ajusta al día más cercano + tooltip con todas las series
  const cross = el("line", {y1: m.t, y2: m.t + ph, stroke: css("--ink2"), "stroke-width": 1, opacity: 0});
  const hit = el("rect", {x: m.l - 10, y: m.t, width: pw + 20, height: ph, fill: "transparent", tabindex: 0});
  let cur = -1;
  const show = i => {
    cur = i; const f = fs[i]; cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", .5);
    tip.replaceChildren();
    const h = document.createElement("div"); h.className = "h"; h.textContent = dfmt(f); tip.appendChild(h);
    series.forEach(s => {
      const row = document.createElement("div"); row.className = "row";
      const l = document.createElement("span"); l.innerHTML = lineKey(css(SLOTS[s.i]), s.k === 1); l.appendChild(document.createTextNode(s.label));
      const v = document.createElement("strong"); v.textContent = fmt(val(s.id, f, s.k));
      row.append(l, v); tip.appendChild(row);
    });
    if (vista === "ambos") elegidos.forEach(id => { const r = val(id, f, 0), p = val(id, f, 1); if (r == null || p == null) return;
      const row = document.createElement("div"); row.className = "row note"; row.textContent = `Desvío ${nombre(C[id])}: ${r - p >= 0 ? "+" : ""}${fmt(r - p)}`; tip.appendChild(row); });
    tip.style.display = "block";
    const tw = tip.offsetWidth, left = x(i) / W * box.clientWidth;
    tip.style.left = Math.min(Math.max(0, left + 12 + tw > box.clientWidth ? left - tw - 12 : left + 12), box.clientWidth - tw) + "px";
    tip.style.top = "8px";
  };
  const hide = () => { cross.setAttribute("opacity", 0); tip.style.display = "none"; cur = -1; };
  hit.addEventListener("pointermove", e => { const r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * W / r.width;
    show(fs.length === 1 ? 0 : Math.max(0, Math.min(fs.length - 1, Math.round((px - m.l) / pw * (fs.length - 1))))); });
  hit.addEventListener("pointerleave", hide); hit.addEventListener("blur", hide);
  hit.addEventListener("focus", () => show(fs.length - 1));
  hit.addEventListener("keydown", e => { if (e.key === "ArrowLeft") show(Math.max(0, cur - 1)); if (e.key === "ArrowRight") show(Math.min(fs.length - 1, cur + 1)); });
}

// ---------------------------------------------------------------- tabla acumulada
const SEC_TITULOS = new Set(DATA.conceptos.filter(c => !c.subgrupo && c.concepto === c.concepto.toUpperCase() && /^(DEMANDA TOTAL|A- |B- |INYECCIONES|STOCK)/.test(c.concepto)).map(c => c.id));
function columnas(){ const ks = vista === "ambos" ? [0,1] : vista === "real" ? [0] : [1]; return fechas().flatMap(f => ks.map(k => [f, k])); }
function renderTabla(){
  const sec = $("#t-sec").value, cols = columnas(), ks = vista === "ambos" ? 2 : 1;
  let h = "<thead><tr><th rowspan='2'>Concepto</th>" + fechas().map(f => `<th colspan="${ks}">${dfmt(f)}</th>`).join("") + "</tr><tr>";
  h += cols.map(([f, k]) => `<th class="${k ? "" : "r"}">${k ? "Proy." : "Real"}</th>`).join("") + "</tr></thead><tbody>";
  DATA.conceptos.filter(c => !sec || c.seccion === sec).forEach(c => {
    h += `<tr class="${SEC_TITULOS.has(c.id) ? "sec" : ""}"><td class="${c.subgrupo ? "sub1" : ""}">${esc(c.concepto)}${c.unidad !== "MMm³" ? " (" + esc(c.unidad) + ")" : ""}</td>`;
    h += cols.map(([f, k]) => { const v = val(c.id, f, k); return `<td class="${k ? "" : "r"}${v == null ? " na" : ""}">${fmt(v)}</td>`; }).join("") + "</tr>";
  });
  $("#tbl").innerHTML = h + "</tbody>";
}
$("#dl").onclick = () => {
  const sec = $("#t-sec").value, cols = columnas(), q = s => `"${String(s).replace(/"/g, '""')}"`;
  const lines = [["seccion","subgrupo","concepto","unidad", ...cols.map(([f, k]) => `${f} ${k ? "proyectado" : "real"}`)].map(q).join(",")];
  DATA.conceptos.filter(c => !sec || c.seccion === sec).forEach(c =>
    lines.push([c.seccion, c.subgrupo, c.concepto, c.unidad].map(q).concat(cols.map(([f, k]) => { const v = val(c.id, f, k); return v == null ? "" : v; })).join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + lines.join("\n")], {type: "text/csv"}));
  a.download = `proyeccion_semanal_${vista}_${desde}_${hasta}.csv`; a.click(); URL.revokeObjectURL(a.href);
};

// ---------------------------------------------------------------- archivo original
(function(){
  const sel = $("#a-sel"); Object.keys(DATA.archivos).forEach(a => sel.add(new Option(a, a)));
  sel.onchange = renderArchivo;
})();
function renderArchivo(){
  const a = DATA.archivos[$("#a-sel").value]; if (!a) return;
  $("#a-meta").textContent = `Emisión: ${a.emision} · Semana: ${a.semana}`;
  const tipo = t => t === "Real" ? "real" : "proy";
  const cols = a.cols.filter(([f, t]) => vista === "ambos" || tipo(t) === vista);
  let h = "<thead><tr><th>Concepto</th>" + cols.map(([f, t]) => `<th class="${t === "Real" ? "r" : ""}">${dfmt(f)}<br><span class="note">${t}</span></th>`).join("") + "</tr></thead><tbody>";
  Object.entries(a.filas).forEach(([id, v]) => {
    const c = C[id] || {concepto: id.split("|")[2], subgrupo: id.split("|")[1], unidad: "MMm³"};
    h += `<tr class="${SEC_TITULOS.has(id) ? "sec" : ""}"><td class="${c.subgrupo ? "sub1" : ""}">${esc(c.concepto)}</td>` +
         cols.map(([f, t]) => `<td class="${t === "Real" ? "r" : ""}">${fmt(v[f + "|" + t])}</td>`).join("") + "</tr>";
  });
  $("#a-tbl").innerHTML = h + "</tbody>";
}

function render(){
  document.querySelectorAll("#vista button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v === vista));
  renderKpis(); renderChart(); renderTabla(); renderArchivo();
}
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(renderChart, 150); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderChart);
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
