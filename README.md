# Extractor ENARGAS – Proyección Semanal de la Demanda del Sistema de Transporte

Descarga los PDFs publicados en
[ENARGAS – Proyección Semanal](https://www.enargas.gob.ar/secciones/transporte-y-distribucion/dod-proyeccion-semanal.php)
(`PS_AAAAMMDD.pdf`), extrae sus datos, los acumula y genera un dashboard HTML.

## Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso

```bash
.venv/bin/python enargas_proyeccion_semanal.py                  # descarga, acumula, genera CSV + dashboard
.venv/bin/python enargas_proyeccion_semanal.py --desde 2026-09-28  # cambia el inicio de la serie (default 2026-10-01)
.venv/bin/python enargas_proyeccion_semanal.py --sheets         # además vuelca a Google Sheets
.venv/bin/python enargas_proyeccion_semanal.py --pdf PS_20261005.pdf  # parsea un PDF local
```

ENARGAS solo publica los últimos 5 archivos: conviene correrlo a diario para no perder días.

## Salidas (`data/`)

| Archivo | Contenido |
|---|---|
| `proyeccion_semanal.csv` | Datos crudos acumulados, una fila por dato (archivo, fecha, tipo Programado/Real/Proyección, sección, concepto, valor). Cada corrida agrega solo archivos nuevos. |
| `serie_acumulada.csv` | Serie consolidada desde `--desde`: por día y concepto, último valor Real y última proyección publicada, con el desvío. |
| `proyeccion_semanal.html` | Dashboard: selector Real / Proyectado / Real y Proyectado, rango de fechas, indicadores, gráfico de hasta 3 conceptos, tabla acumulada con descarga CSV y vista de cada archivo original. |
| `pdfs/` | PDFs descargados (caché). |

## Google Sheets

1. Crear una cuenta de servicio en Google Cloud con la API de Google Sheets habilitada y guardar su clave como `credentials.json` en la raíz (está en `.gitignore`).
2. Compartir la planilla con el email de la cuenta de servicio (Editor).
3. Correr con `--sheets`: agrega los datos de archivos nuevos a la pestaña configurada y reescribe la pestaña `Serie consolidada`.

## Nota sobre el stock

En los PDFs, el stock (linepack) de días pasados no trae columna REAL, por lo que se registra como Proyectado.
