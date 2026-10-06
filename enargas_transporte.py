"""
Extracción de los Partes Diarios Operativos de Transporte de ENARGAS.

Fuente: https://www.enargas.gob.ar/secciones/transporte-y-distribucion/dod-partes-dist-trans.php
(columna "Transporte" del listado de partes diarios).

Uso:
    python enargas_transporte.py 2026-09-01 2026-10-01
    python enargas_transporte.py 2026-09-01 2026-10-01 --sheets   # además sube a Google Sheets

Google Sheets (OAuth con cuenta personal): poner el JSON del cliente OAuth "App de escritorio"
como credenciales.json en esta carpeta. La primera ejecución abre el navegador para autorizar
y guarda token_google.json para las siguientes.

Genera:
    data/pdf_transporte/AAAAMMDD.pdf    (PDFs descargados, se reutilizan si ya existen)
    salida/transporte_diario.csv        (una fila por día)
    salida/transporte_diario.xlsx
"""
import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

import pymupdf
import requests

BASE = "https://www.enargas.gob.ar/secciones/transporte-y-distribucion/"
URL_LISTADO = BASE + "partes-diarios-listado.php"
URL_DESCARGA = BASE + "descarga.php?tipo=transporte&path=partes-diarios/transporte&file={}.pdf"
HEADERS = {"User-Agent": "Mozilla/5.0"}

RAIZ = Path(__file__).resolve().parent
DIR_PDF = RAIZ / "data" / "pdf_transporte"
DIR_SALIDA = RAIZ / "salida"

SHEET_ID = "1-h5ulfg4xmuG6MxiT2iT2qBc3Axii0tI-HQeDt3tu4s"
SHEET_TAB = "Pablo"

# Orden y nombre de las columnas de salida (unidades: MMm3/d a 9300 kcal/m3, line pack en MMSm3, °C)
COLUMNAS = [
    "fecha",
    # Inyección por cuenca
    "iny_cuenca_norte", "iny_cuenca_neuquina", "iny_cuenca_austral",
    # Inyección por gasoducto
    "iny_gto_norte", "iny_gto_centro_oeste", "iny_gto_neuba_I_y_II", "iny_gto_san_martin",
    "iny_total",
    "iny_no_asignada",  # total - suma de gasoductos (p.ej. Peak Shaving, que no figura en ningún gasoducto)
    # Componentes informados en las notas (a)-(e)
    "nota_a_bolivia_norandino", "nota_b_gpm", "nota_d_gnl_escobar_gasandes", "nota_e_peak_shaving",
    # Capacidades
    "cap_nom_norte", "cap_nom_centro_oeste", "cap_nom_neuba_II", "cap_nom_neuba_I", "cap_nom_san_martin",
    "cap_fut_norte", "cap_fut_centro_oeste", "cap_fut_neuba_II", "cap_fut_neuba_I", "cap_fut_san_martin",
    # Egresos por nodo/tramo final
    "egr_sanjeronimo_a_santa_fe", "egr_sanjeronimo_troncal", "egr_sanjeronimo_paralelo",
    "egr_cerri_las_heras", "egr_cerri_rodriguez", "egr_cerri_gutierrez",
    "egr_total_bs_as",
    # Line pack
    "lp_tgn_actual", "lp_tgn_dif", "lp_tgs_actual", "lp_tgs_dif", "lp_total_actual", "lp_total_dif",
    # Pronóstico térmico Capital Federal y alrededores
    "temp_max", "temp_min",
    "pron_d1_fecha", "pron_d1_min_est", "pron_d1_max_est",
    "pron_d2_fecha", "pron_d2_min_est", "pron_d2_max_est",
    "pron_d3_fecha", "pron_d3_min_est", "pron_d3_max_est",
]

NUM = re.compile(r"^-?\d+(\.\d+)?$")


# ---------------------------------------------------------------- descarga

def listar_disponibles(desde: dt.date, hasta: dt.date) -> list[str]:
    """Devuelve los AAAAMMDD con parte de transporte publicado en el rango."""
    r = requests.post(URL_LISTADO, headers=HEADERS, timeout=60,
                      data={"fecha_desde": desde.strftime("%Y%m%d"), "fecha_hasta": hasta.strftime("%Y%m%d")})
    r.raise_for_status()
    return sorted(set(re.findall(r"DecargarPDF\('transporte','partes-diarios/transporte','(\d{8})\.pdf'\)", r.text)))


def descargar(fecha: str) -> Path:
    destino = DIR_PDF / f"{fecha}.pdf"
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    r = requests.get(URL_DESCARGA.format(fecha), headers=HEADERS, timeout=60)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"{fecha}: la descarga no es un PDF")
    destino.write_bytes(r.content)
    return destino


# ---------------------------------------------------------------- parseo

class Pagina:
    def __init__(self, page):
        # (x0, y0, x1, texto)
        self.words = [(w[0], w[1], w[2], w[4]) for w in page.get_text("words")]

    def y_de(self, *etiqueta, x_max=None, y_min=0):
        """Coordenada y de una etiqueta formada por palabras consecutivas."""
        for i, w in enumerate(self.words):
            if w[1] < y_min or (x_max is not None and w[0] > x_max):
                continue
            seq = self.words[i:i + len(etiqueta)]
            if [s[3] for s in seq] == list(etiqueta):
                return w[1]
        raise ValueError(f"No se encontró la etiqueta {' '.join(etiqueta)!r}")

    def num(self, x0, x1, y0, y1):
        """Único número cuyo centro x está en [x0,x1] y su y en [y0,y1]."""
        c = [w for w in self.words if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= w[1] <= y1 and NUM.match(w[3])]
        if len(c) != 1:
            raise ValueError(f"Se esperaba 1 número en x[{x0},{x1}] y[{y0:.0f},{y1:.0f}], hay {[w[3] for w in c]}")
        return float(c[0][3])

    def num_fila(self, x0, x1, y, tol=4):
        return self.num(x0, x1, y - tol, y + tol)


def _nota(texto, patron):
    m = re.search(patron + r"\s*\((-?[\d.]+)\)", texto)
    return float(m.group(1)) if m else None


# Glifos (fuente bitmap fija) de las etiquetas del eje Y de los gráficos de pronóstico.
GLIFOS = {
    ".##../#..#./#..##/#..##/#..##/#..#./.##..": "0",
    "###../..#../..#../..#../..#../..#../#####": "1",
    "####/...#/...#/..##/.##./##../####": "2",
    "...##./..###./..###./.#.##./.#.##./######/...##.": "4",
    "####/#.../#.../####/...#/...#/####": "5",
    ".###./##.../#..../####./##.##/##.##/.###.": "6",
    "####./#..#./#..#./####./#..#./#..##/####.": "8",
    "###.....##./..#....###./..#....###./..#...#.##./..#...#.##./..#..######/#####...##.": "14",
    "./././././#/#": ".",
}


def _leer_eje(pix, oscuro, ax):
    """Devuelve [(y_marca, valor)] de las etiquetas legibles del eje Y."""
    filas = [y for y in range(pix.height) if any(oscuro(x, y) for x in range(ax - 25, ax - 3))]
    bandas = []
    for y in filas:
        if bandas and y - bandas[-1][-1] <= 1:
            bandas[-1].append(y)
        else:
            bandas.append([y])
    marcas = [y for y in range(pix.height) if all(oscuro(ax + d, y) for d in range(1, 6))]
    etiquetas = []
    for b in bandas:
        cols = [x for x in range(ax - 25, ax - 3) if any(oscuro(x, y) for y in b)]
        segs = []
        for x in cols:
            if segs and x - segs[-1][-1] <= 1:
                segs[-1].append(x)
            else:
                segs.append([x])
        texto = ""
        for s in segs:
            clave = "/".join("".join("#" if oscuro(x, y) else "." for x in s) for y in b)
            if clave not in GLIFOS:
                texto = None
                break
            texto += GLIFOS[clave]
        if not texto or not marcas:
            continue
        centro = (b[0] + b[-1]) / 2
        y_marca = min(marcas, key=lambda m: abs(m - centro))
        if abs(y_marca - centro) <= 3:
            etiquetas.append((y_marca, float(texto)))
    return etiquetas


def pronostico_grafico(doc, page):
    """
    Estima mín/máx de los 3 gráficos de pronóstico (imágenes rasterizadas):
    calibra la escala con las etiquetas del eje Y y mide el tope de cada barra.
    Resolución ~0.05-0.2 °C por píxel según la escala. Devuelve None si no hay barras.
    """
    imgs = sorted(((page.get_image_rects(i[0])[0], i[0]) for i in page.get_images(full=True)),
                  key=lambda t: t[0].x0)
    graficos = [xref for rect, xref in imgs if 80 < rect.width < 110 and rect.height > 150]
    res = []
    for xref in graficos[:3]:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.n - pix.alpha > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        px = lambda x, y: pix.pixel(x, y)[:3]
        oscuro = lambda x, y: max(px(x, y)) < 110
        negro = lambda x, y: max(px(x, y)) < 60
        amarillo = lambda x, y: px(x, y)[0] > 200 and px(x, y)[1] > 200 and px(x, y)[2] < 80

        ejes = [x for x in range(10, 60) if all(oscuro(x, y) for y in range(60, 190))]
        etiquetas = _leer_eje(pix, oscuro, ejes[0]) if ejes else []
        if len(etiquetas) < 2:
            res.append((None, None))
            continue
        (y_a, v_a), (y_b, v_b) = etiquetas[0], etiquetas[-1]
        valor = lambda y: round(v_a + (y - y_a) * (v_b - v_a) / (y_b - y_a), 1)
        # base de las barras: fila del eje X (la etiqueta más baja)
        y_base = max(e[0] for e in etiquetas)

        def tope(es_color):
            xs = [x for x in range(ejes[0] + 2, pix.width) if es_color(x, y_base - 2)]
            if len(xs) < 5:  # sin barra (gráfico vacío): solo bordes de 1-2 px
                return None
            x = xs[len(xs) // 2]
            y = y_base - 2
            while y > 0 and es_color(x, y - 1):
                y -= 1
            # las barras amarillas tienen un borde superior negro de 1 px (las negras ya lo incluyen)
            if y > 0 and negro(x, y - 1) and not es_color(x, y - 1):
                y -= 1
            return valor(y)

        res.append((tope(negro), tope(amarillo)))
    return res


def parsear(pdf: Path) -> dict:
    doc = pymupdf.open(pdf)
    page = doc[0]
    P = Pagina(page)
    texto = page.get_text()
    d = {}

    m = re.search(r"06hs\.\s*del\s*\w+\s*(\d\d/\d\d/\d\d)\s*a las", texto)
    if not m:
        raise ValueError(f"{pdf.name}: no se encontró el período")
    fecha = dt.datetime.strptime(m.group(1), "%d/%m/%y").date()
    if fecha.strftime("%Y%m%d") != pdf.stem:
        raise ValueError(f"{pdf.name}: el período del PDF es {fecha}")
    d["fecha"] = fecha.isoformat()

    # Filas de la tabla principal (ancladas por etiqueta del gasoducto)
    y_norte = P.y_de("Norte", "(a)")
    y_co = P.y_de("Centro", "Oeste")
    y_n2 = P.y_de("Neuba", "II", "(b)")
    y_n1 = P.y_de("Neuba", "I", "(c)")
    y_sm = P.y_de("San", "Mart�n") if any(w[3] == "Mart�n" for w in P.words) else P.y_de("San", "Martín")
    y_total = P.y_de("Inyecci�n", "Total") if any(w[3] == "Inyecci�n" for w in P.words) else P.y_de("Inyección", "Total")

    # Inyección por cuenca (valor debajo del nombre)
    d["iny_cuenca_norte"] = P.num(100, 150, y_norte + 5, y_norte + 18)
    y_neuq = P.y_de("Neuquina")
    d["iny_cuenca_neuquina"] = P.num(100, 150, y_neuq + 5, y_neuq + 18)
    y_aus = P.y_de("Austral")
    d["iny_cuenca_austral"] = P.num(100, 150, y_aus + 5, y_aus + 18)

    # Inyección por gasoducto (x≈230-270); Neuba I y II comparten un valor entre ambas filas
    X_INY, X_NOM, X_FUT = (230, 275), (290, 335), (350, 395)
    d["iny_gto_norte"] = P.num_fila(*X_INY, y_norte)
    d["iny_gto_centro_oeste"] = P.num_fila(*X_INY, y_co + 5, tol=8)
    d["iny_gto_neuba_I_y_II"] = P.num(*X_INY, y_n2 - 2, y_n1 + 2)
    d["iny_gto_san_martin"] = P.num_fila(*X_INY, y_sm)
    d["iny_total"] = P.num_fila(*X_INY, y_total)

    d["nota_a_bolivia_norandino"] = _nota(texto, r"Bolivia y Norandino")
    d["nota_b_gpm"] = _nota(texto, r"GPM")
    d["nota_d_gnl_escobar_gasandes"] = _nota(texto, r"Escobar y Gasandes")
    d["nota_e_peak_shaving"] = _nota(texto, r"Peak Shaving")

    for pref, xr in (("cap_nom", X_NOM), ("cap_fut", X_FUT)):
        d[f"{pref}_norte"] = P.num_fila(*xr, y_norte)
        d[f"{pref}_centro_oeste"] = P.num_fila(*xr, y_co + 5, tol=8)
        d[f"{pref}_neuba_II"] = P.num_fila(*xr, y_n2 + 1, tol=5)
        d[f"{pref}_neuba_I"] = P.num_fila(*xr, y_n1)
        d[f"{pref}_san_martin"] = P.num_fila(*xr, y_sm)

    # Egresos por nodo/tramo (x valores ≈455-490)
    X_EGR = (455, 492)
    d["egr_sanjeronimo_a_santa_fe"] = P.num_fila(*X_EGR, P.y_de("A", "Santa", "Fe"))
    d["egr_sanjeronimo_troncal"] = P.num_fila(*X_EGR, P.y_de("Troncal"))
    d["egr_sanjeronimo_paralelo"] = P.num_fila(*X_EGR, P.y_de("Paralelo"))
    d["egr_cerri_las_heras"] = P.num_fila(*X_EGR, P.y_de("Las", "Heras"))
    d["egr_cerri_rodriguez"] = P.num_fila(*X_EGR, P.y_de("Rodriguez", x_max=500, y_min=y_n2))
    d["egr_cerri_gutierrez"] = P.num_fila(*X_EGR, P.y_de("Gutierrez", x_max=500))
    d["egr_total_bs_as"] = P.num(495, 540, y_norte, y_total - 10)

    # Line pack: TGN (bloque superior), TGS (bloque inferior), total (fila de totales)
    X_LP, X_DIF = (540, 582), (582, 612)
    y_medio = (y_co + y_n2) / 2
    d["lp_tgn_actual"] = P.num(*X_LP, y_norte, y_medio)
    d["lp_tgn_dif"] = P.num(*X_DIF, y_norte, y_medio)
    d["lp_tgs_actual"] = P.num(*X_LP, y_medio, y_total - 10)
    d["lp_tgs_dif"] = P.num(*X_DIF, y_medio, y_total - 10)
    d["lp_total_actual"] = P.num_fila(*X_LP, y_total, tol=6)
    d["lp_total_dif"] = P.num_fila(*X_DIF, y_total, tol=6)

    # Pronóstico: recuadro Máxima/Mínima (texto exacto)
    mx = re.search(r"M.xima:\s*(-?[\d.]+)", texto)
    mn = re.search(r"M.nima:\s*(-?[\d.]+)", texto)
    d["temp_max"] = float(mx.group(1)) if mx else None
    d["temp_min"] = float(mn.group(1)) if mn else None

    # Pronóstico 3 días (estimado desde los gráficos)
    for i, (tmin, tmax) in enumerate(pronostico_grafico(doc, page), start=1):
        d[f"pron_d{i}_fecha"] = (fecha + dt.timedelta(days=i)).isoformat()
        d[f"pron_d{i}_min_est"] = tmin
        d[f"pron_d{i}_max_est"] = tmax

    # Controles de consistencia
    avisos = []
    suma_cuenca = d["iny_cuenca_norte"] + d["iny_cuenca_neuquina"] + d["iny_cuenca_austral"]
    suma_gto = d["iny_gto_norte"] + d["iny_gto_centro_oeste"] + d["iny_gto_neuba_I_y_II"] + d["iny_gto_san_martin"]
    if abs(suma_cuenca - suma_gto) > 0.25:
        avisos.append(f"suma cuencas {suma_cuenca:.1f} != suma gasoductos {suma_gto:.1f}")
    d["iny_no_asignada"] = round(d["iny_total"] - suma_gto, 1)
    if abs(d["lp_tgn_actual"] + d["lp_tgs_actual"] - d["lp_total_actual"]) > 0.25:
        avisos.append("line pack TGN+TGS != total")
    if abs(d["lp_tgn_dif"] + d["lp_tgs_dif"] - d["lp_total_dif"]) > 0.25:
        avisos.append("dif line pack TGN+TGS != total")
    d["_avisos"] = avisos
    return d


# ---------------------------------------------------------------- salida

def guardar(filas: list[dict]):
    DIR_SALIDA.mkdir(exist_ok=True)
    with open(DIR_SALIDA / "transporte_diario.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = SHEET_TAB
        ws.append(COLUMNAS)
        for r in filas:
            ws.append([r.get(c) for c in COLUMNAS])
        ws.freeze_panes = "B2"
        wb.save(DIR_SALIDA / "transporte_diario.xlsx")
    except ImportError:
        pass


def conectar_google(credenciales: Path):
    """Acepta un cliente OAuth de escritorio (clave 'installed') o una cuenta de servicio."""
    import json
    import gspread
    info = json.loads(credenciales.read_text(encoding="utf-8"))
    if info.get("type") == "service_account":
        return gspread.service_account(filename=str(credenciales))
    # OAuth: la primera vez abre el navegador para autorizar; el token queda guardado al lado
    return gspread.oauth(credentials_filename=str(credenciales),
                         authorized_user_filename=str(credenciales.with_name("token_google.json")))


def subir_a_sheets(filas: list[dict], credenciales: Path):
    """Combina con lo que ya hay en la hoja (por fecha) y reescribe la pestaña."""
    ws = conectar_google(credenciales).open_by_key(SHEET_ID).worksheet(SHEET_TAB)
    existentes = {}
    valores = ws.get_all_values()
    if valores and valores[0] == COLUMNAS:
        for v in valores[1:]:
            existentes[v[0]] = dict(zip(COLUMNAS, v))
    elif any(c for fila in valores for c in fila):
        raise RuntimeError(f"La hoja '{SHEET_TAB}' tiene contenido que no generó este script; no se sobrescribe.")
    for r in filas:
        existentes[r["fecha"]] = r
    salida = [COLUMNAS] + [[("" if existentes[k].get(c) is None else existentes[k].get(c)) for c in COLUMNAS]
                           for k in sorted(existentes)]
    ws.clear()
    ws.update(salida, "A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1, cols=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("desde", type=dt.date.fromisoformat)
    ap.add_argument("hasta", type=dt.date.fromisoformat)
    ap.add_argument("--sheets", action="store_true", help="subir el resultado a Google Sheets")
    ap.add_argument("--credenciales", type=Path, default=RAIZ / "credenciales.json",
                    help="JSON del cliente OAuth de escritorio (o de una cuenta de servicio) de Google")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    DIR_PDF.mkdir(parents=True, exist_ok=True)
    fechas = listar_disponibles(a.desde, a.hasta)
    print(f"{len(fechas)} partes de transporte disponibles entre {a.desde} y {a.hasta}")

    filas = []
    for f in fechas:
        try:
            r = parsear(descargar(f))
        except Exception as e:
            print(f"  ERROR {f}: {e}", file=sys.stderr)
            continue
        if r["_avisos"]:
            print(f"  AVISO {f}: {'; '.join(r['_avisos'])}")
        filas.append(r)

    guardar(filas)
    print(f"{len(filas)} días guardados en {DIR_SALIDA}")
    if a.sheets:
        subir_a_sheets(filas, a.credenciales)
        print(f"Hoja '{SHEET_TAB}' actualizada")


if __name__ == "__main__":
    main()
