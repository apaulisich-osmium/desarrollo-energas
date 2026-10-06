# Desarrollo ENARGAS

Extractores de datos operativos publicados por ENARGAS.

| Módulo | Script | Fuente | Salidas |
|---|---|---|---|
| [Proyección semanal de demanda](#extractor-enargas--proyección-semanal-de-la-demanda-del-sistema-de-transporte) | `enargas_proyeccion_semanal.py` | Proyección Semanal (`PS_AAAAMMDD.pdf`) | `data/` |
| [Partes diarios de transporte](#partes-diarios-de-transporte-de-gas--enargas) | `enargas_transporte.py` | Partes Diarios, columna Transporte | `data/pdf_transporte/`, `salida/` |

Instalación común: `pip install -r requirements.txt`.

**Pantalla principal:** abrir [`index.html`](index.html) en el navegador. Tiene un botón para cada módulo:
`data/proyeccion_semanal.html` (Proyección Semanal) y `salida/transporte_diario.html` (Partes de Transporte).
Cada dashboard se regenera al correr su script.

Ambos módulos escriben en la misma planilla de Google Sheets, en pestañas distintas.

---

## Extractor ENARGAS – Proyección Semanal de la Demanda del Sistema de Transporte

Descarga los PDFs publicados en
[ENARGAS – Proyección Semanal](https://www.enargas.gob.ar/secciones/transporte-y-distribucion/dod-proyeccion-semanal.php)
(`PS_AAAAMMDD.pdf`), extrae sus datos, los acumula y genera un dashboard HTML.

### Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Uso

```bash
.venv/bin/python enargas_proyeccion_semanal.py                  # descarga, acumula, genera CSV + dashboard
.venv/bin/python enargas_proyeccion_semanal.py --desde 2026-09-28  # cambia el inicio de la serie (default 2026-10-01)
.venv/bin/python enargas_proyeccion_semanal.py --sheets         # además vuelca a Google Sheets
.venv/bin/python enargas_proyeccion_semanal.py --pdf PS_20261005.pdf  # parsea un PDF local
```

ENARGAS solo publica los últimos 5 archivos: conviene correrlo a diario para no perder días.

### Salidas (`data/`)

| Archivo | Contenido |
|---|---|
| `proyeccion_semanal.csv` | Datos crudos acumulados, una fila por dato (archivo, fecha, tipo Programado/Real/Proyección, sección, concepto, valor). Cada corrida agrega solo archivos nuevos. |
| `serie_acumulada.csv` | Serie consolidada desde `--desde`: por día y concepto, último valor Real y última proyección publicada, con el desvío. |
| `proyeccion_semanal.html` | Dashboard: selector Real / Proyectado / Real y Proyectado, rango de fechas, indicadores, gráfico de hasta 3 conceptos, tabla acumulada con descarga CSV y vista de cada archivo original. |
| `pdfs/` | PDFs descargados (caché). |

### Google Sheets

1. Crear una cuenta de servicio en Google Cloud con la API de Google Sheets habilitada y guardar su clave como `credentials.json` en la raíz (está en `.gitignore`).
2. Compartir la planilla con el email de la cuenta de servicio (Editor).
3. Correr con `--sheets`: agrega los datos de archivos nuevos a la pestaña configurada y reescribe la pestaña `Serie consolidada`.

### Nota sobre el stock

En los PDFs, el stock (linepack) de días pasados no trae columna REAL, por lo que se registra como Proyectado.

---

## Partes diarios de transporte de gas – ENARGAS

Extrae a formato tabla los **Partes Diarios Operativos de Transporte** publicados por ENARGAS
([Partes de Distribución y Transporte](https://www.enargas.gob.ar/secciones/transporte-y-distribucion/dod-partes-dist-trans.php), columna *Transporte*).

### Uso

```bash
pip install -r requirements.txt
python enargas_transporte.py 2026-09-01 2026-10-01            # descarga, parsea y genera salida/
python enargas_transporte.py 2026-09-01 2026-10-01 --sheets   # además actualiza la hoja "Pablo" de Google Sheets
```

- Los PDFs se guardan en `data/pdf_transporte/AAAAMMDD.pdf` y no se vuelven a descargar.
- La tabla resultante queda en `salida/transporte_diario.csv` y `salida/transporte_diario.xlsx` (una fila por día operativo). Cada corrida se suma a lo ya acumulado.
- Dashboard: `salida/transporte_diario.html` (plantilla en `plantillas/dashboard_transporte.html`).

### Columnas

Volúmenes en MMm³/d a 9300 kcal/m³; line pack en MMSm³; temperaturas en °C.

| Grupo | Columnas |
|---|---|
| Inyección por cuenca | `iny_cuenca_norte`, `iny_cuenca_neuquina`, `iny_cuenca_austral` |
| Inyección por gasoducto | `iny_gto_norte`, `iny_gto_centro_oeste`, `iny_gto_neuba_I_y_II` (el PDF da un valor conjunto), `iny_gto_san_martin`, `iny_total` |
| Inyección no asignada | `iny_no_asignada` = total − suma de gasoductos (p. ej. Peak Shaving) |
| Notas (a)–(e) del parte | `nota_a_bolivia_norandino`, `nota_b_gpm`, `nota_d_gnl_escobar_gasandes`, `nota_e_peak_shaving` |
| Capacidades | `cap_nom_*`, `cap_fut_*` por gasoducto |
| Egresos por nodo/tramo | `egr_sanjeronimo_a_santa_fe`, `egr_sanjeronimo_troncal`, `egr_sanjeronimo_paralelo`, `egr_cerri_las_heras`, `egr_cerri_rodriguez`, `egr_cerri_gutierrez`, `egr_total_bs_as` |
| Line pack | `lp_tgn_actual`, `lp_tgn_dif`, `lp_tgs_actual`, `lp_tgs_dif`, `lp_total_actual`, `lp_total_dif` |
| Pronóstico Capital Federal y alrededores | `temp_max`, `temp_min` (recuadro del parte) y `pron_d{1,2,3}_fecha/min_est/max_est` |

Los valores `pron_d*_est` se **estiman** midiendo las barras de los gráficos (imágenes sin números en el PDF),
con un error aproximado de ±0.1–0.2 °C. Quedan vacíos cuando el parte publica los gráficos sin datos.

### Controles

Por cada día se verifica que la suma por cuenca coincida con la suma por gasoducto y que
TGN + TGS coincida con el line pack total; las diferencias se informan como `AVISO` en consola.

### Google Sheets (OAuth)

1. En Google Cloud Console habilitar **Google Sheets API** y **Google Drive API**.
2. Crear un ID de cliente OAuth de tipo **App de escritorio** y guardar el JSON como `credenciales.json` en esta carpeta.
3. La primera ejecución con `--sheets` abre el navegador para autorizar y guarda `token_google.json`.

Ambos archivos están en `.gitignore`: **no subirlos al repositorio**.

### Próximos pasos

- Incorporar los partes de importación (GNL Escobar, Norandino, GasAndes) para separar importación de producción local.
- Extender el rango histórico.
