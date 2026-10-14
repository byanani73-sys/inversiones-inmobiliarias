"""
backfill_telefonos.py — Rellena nombre_publicante y telefono_publicante en
propiedades activas que aún no tienen teléfono registrado.

Para cada propiedad con telefono_publicante IS NULL (y activa=1), visita la
URL con un browser Playwright+stealth fresco, hace click en "Ver teléfono",
completa el formulario de contacto si aparece (datos ficticios rotativos),
y guarda el resultado en SQLite y Google Sheets inmediatamente.

Filtros aplicados:
  - activa = 1
  - telefono_publicante IS NULL OR telefono_publicante = ''
  - estado en Sheet NO sea uno de los estados de contacto ya gestionados

Uso:
  python scrapers/backfill_telefonos.py
  python scrapers/backfill_telefonos.py --limite 20
  python scrapers/backfill_telefonos.py --dry-run
  python scrapers/backfill_telefonos.py --sin-sheets
"""

import argparse
import asyncio
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

LOG_FILE = Path.home() / "Library" / "Logs" / "backfill_telefonos.log"
DEBUG_SCREENSHOT = str(ROOT / "logs" / "debug_telefono.png")

from scrapers.scraper_diario import (
    DB_PATH,
    connect_sheets, cargar_sheet_state, col_letter,
    fetch_telefono,
)

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
DELAY_MIN = 8    # segundos mínimos entre visitas
DELAY_MAX = 20   # segundos máximos entre visitas

# Estados que indican que la propiedad ya fue contactada — no procesar
ESTADOS_EXCLUIDOS = {
    "Llamado/no atendió",
    "Habló-no interesado",
    "Habló-interesado",
    "Negociando/volver a llamar",
    "Captado",
    "Publicación desactivada",
}


# ---------------------------------------------------------------------------
# Tee: escribe a stdout Y a un archivo de log simultáneamente
# ---------------------------------------------------------------------------
class _Tee:
    """Wraps sys.stdout para duplicar salida a un archivo."""
    def __init__(self, filepath: Path):
        self._file = open(filepath, "a", encoding="utf-8", buffering=1)
        self._stdout = sys.__stdout__

    def write(self, data):
        self._stdout.write(data)
        self._file.write(data)

    def flush(self):
        self._stdout.flush()
        self._file.flush()

    def close(self):
        self._file.close()

    # Para compatibilidad con librerías que inspeccionan sys.stdout
    @property
    def encoding(self):
        return self._stdout.encoding

    @property
    def errors(self):
        return self._stdout.errors

    def isatty(self):
        return self._stdout.isatty()

    def fileno(self):
        return self._stdout.fileno()


def instalar_tee(filepath: Path) -> _Tee:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tee = _Tee(filepath)
    sys.stdout = tee
    sys.stderr = tee
    return tee


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def cargar_sin_telefono(limite: int | None) -> list[dict]:
    """Carga propiedades activas donde telefono_publicante IS NULL o vacío."""
    conn = sqlite3.connect(DB_PATH)

    # Asegurar que las columnas existen (por si la DB es vieja)
    existentes = {row[1] for row in conn.execute("PRAGMA table_info(propiedades)")}
    for col, tipo in [("nombre_publicante", "TEXT"), ("telefono_publicante", "TEXT")]:
        if col not in existentes:
            conn.execute(f"ALTER TABLE propiedades ADD COLUMN {col} {tipo}")
            conn.commit()
            print(f"  [DB] Columna creada: {col}")

    rows = conn.execute(
        """SELECT id_zonaprop, url, barrio_simple, precio_por_m2_tasable, fecha_primera_vez
           FROM propiedades
           WHERE activa = 1
             AND (telefono_publicante IS NULL OR telefono_publicante = '')
             AND url IS NOT NULL
           ORDER BY fecha_primera_vez DESC"""
    ).fetchall()
    conn.close()

    props = []
    for id_zp, url, barrio, pm2, fecha in rows:
        props.append({
            "id":     id_zp,
            "url":    url,
            "barrio": barrio or "?",
            "pm2":    pm2 or 0,
            "fecha":  (fecha or "")[:10],
        })

    if limite:
        props = props[:limite]
    return props


def filtrar_por_estado(props: list[dict], sh: dict) -> tuple[list[dict], int]:
    """
    Elimina de la lista aquellas propiedades cuyo estado en el Sheet
    pertenece a ESTADOS_EXCLUIDOS.
    Devuelve (lista_filtrada, n_excluidos).
    """
    if not sh or not sh.get("all_values"):
        return props, 0

    all_values = sh["all_values"]
    h_idx      = sh["h_idx"]
    id_col     = h_idx.get("id_zonaprop", 0)
    estado_col = h_idx.get("estado")

    if estado_col is None:
        return props, 0

    # Construir mapa id → estado desde el Sheet
    id_estado: dict[str, str] = {}
    for r_num, row in enumerate(all_values, 1):
        if r_num == 1 or not row or len(row) <= id_col:
            continue
        id_zp = row[id_col]
        if not id_zp:
            continue
        estado = row[estado_col] if len(row) > estado_col else ""
        id_estado[id_zp] = estado.strip()

    filtradas = []
    excluidos = 0
    for p in props:
        estado = id_estado.get(p["id"], "")
        if estado in ESTADOS_EXCLUIDOS:
            excluidos += 1
        else:
            filtradas.append(p)

    return filtradas, excluidos


def guardar_en_db(id_zp: str, nombre: str | None, telefono: str | None) -> bool:
    """
    Guarda nombre y teléfono de una sola propiedad en SQLite de inmediato.
    Devuelve True si se escribió algo.
    """
    nombre   = nombre   or None
    telefono = telefono or None
    if not nombre and not telefono:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """UPDATE propiedades
           SET nombre_publicante = ?, telefono_publicante = ?
           WHERE id_zonaprop = ?""",
        (nombre, telefono, id_zp),
    )
    conn.commit()
    conn.close()
    return True


# ---------------------------------------------------------------------------
# Google Sheets — actualización inmediata de una sola fila
# ---------------------------------------------------------------------------
def actualizar_fila_sheets(
    sh: dict,
    id_zp: str,
    nombre: str | None,
    telefono: str | None,
) -> bool:
    """
    Actualiza nombre_publicante y telefono_publicante para un solo ID en el Sheet.
    Devuelve True si se pudo escribir.
    """
    if not sh:
        return False

    ws        = sh["ws"]
    h_idx     = sh["h_idx"]
    id_to_row = sh["id_to_row"]

    nombre_i   = h_idx.get("nombre_publicante")
    telefono_i = h_idx.get("telefono_publicante")

    if nombre_i is None and telefono_i is None:
        return False

    row_num = id_to_row.get(id_zp)
    if not row_num:
        return False

    updates = []
    if nombre_i is not None:
        updates.append({
            "range":  f"{col_letter(nombre_i)}{row_num}",
            "values": [[nombre or ""]],
        })
    if telefono_i is not None:
        updates.append({
            "range":  f"{col_letter(telefono_i)}{row_num}",
            "values": [[telefono or ""]],
        })

    if updates:
        ws.batch_update(updates)
    return True


# ---------------------------------------------------------------------------
# Google Sheets sync — batch al final (para recuperar_desde_log)
# ---------------------------------------------------------------------------
def sincronizar_telefonos_sheets(
    sh: dict,
    resultados: list[dict],
    dry_run: bool = False,
) -> None:
    """Batch-update nombre_publicante y telefono_publicante en el Sheet."""
    ws        = sh["ws"]
    h_idx     = sh["h_idx"]
    id_to_row = sh["id_to_row"]

    nombre_i   = h_idx.get("nombre_publicante")
    telefono_i = h_idx.get("telefono_publicante")

    if nombre_i is None and telefono_i is None:
        print("  [!] Sheet: columnas nombre_publicante / telefono_publicante no encontradas — omitiendo")
        return

    updates  = []
    sin_fila = []

    for r in resultados:
        nombre   = r.get("nombre")   or ""
        telefono = r.get("telefono") or ""
        if not nombre and not telefono:
            continue
        row_num = id_to_row.get(r["id"])
        if not row_num:
            sin_fila.append(r["id"])
            continue
        if nombre_i is not None:
            updates.append({
                "range":  f"{col_letter(nombre_i)}{row_num}",
                "values": [[nombre]],
            })
        if telefono_i is not None:
            updates.append({
                "range":  f"{col_letter(telefono_i)}{row_num}",
                "values": [[telefono]],
            })

    if sin_fila:
        print(f"  [!] {len(sin_fila)} IDs no encontrados en Sheet (se ignoran)")

    if not updates:
        print("  Sheet: sin datos que escribir")
        return

    if dry_run:
        print(f"  [dry-run] {len(updates)} celdas a actualizar en Sheet")
        return

    BATCH = 500
    for i in range(0, len(updates), BATCH):
        ws.batch_update(updates[i:i + BATCH])
        time.sleep(1)

    n_props = len([r for r in resultados if r.get("nombre") or r.get("telefono")])
    print(f"  ✓ {n_props} propiedades actualizadas en Sheet")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(limite: int | None, sin_sheets: bool, dry_run: bool) -> None:
    tee = instalar_tee(LOG_FILE)
    t0  = time.time()
    hoy = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 65}")
    print(f"BACKFILL TELÉFONOS — {hoy}")
    if dry_run:
        print("  [DRY-RUN: no se escribirá nada]")
    print(f"  Log: {LOG_FILE}")
    print(f"{'=' * 65}\n")

    # -----------------------------------------------------------------------
    # 1. Conectar Sheet
    # -----------------------------------------------------------------------
    sh = None
    if not sin_sheets:
        print("[1/4] Conectando al Sheet...")
        try:
            ws = connect_sheets()
            sh = cargar_sheet_state(ws)
            print(f"  {len(sh['id_to_row'])} IDs cargados del Sheet")
        except Exception as e:
            print(f"  [!] Error conectando al Sheet: {e}")
            print("  Continuando sin sync al Sheet")
    else:
        print("[1/4] Sin-sheets: se omite conexión al Sheet")

    # -----------------------------------------------------------------------
    # 2. Cargar propiedades sin teléfono de SQLite
    # -----------------------------------------------------------------------
    print("\n[2/4] Cargando propiedades sin teléfono de SQLite...")
    propiedades = cargar_sin_telefono(limite)
    print(f"  {len(propiedades)} candidatas en SQLite (activa=1, sin teléfono)")

    # Filtrar por estado del Sheet
    if sh:
        propiedades, n_excl = filtrar_por_estado(propiedades, sh)
        print(f"  {n_excl} excluidas por estado (ya contactadas)")
        print(f"  {len(propiedades)} propiedades a procesar")
    else:
        print(f"  (sin Sheet: no se filtra por estado)")
        print(f"  {len(propiedades)} propiedades a procesar")

    total = len(propiedades)
    if total == 0:
        print("\nNada que procesar.")
        tee.close()
        return

    # -----------------------------------------------------------------------
    # 3. Visitar cada URL y extraer teléfono
    # -----------------------------------------------------------------------
    print(f"\n[3/4] Extrayendo teléfonos "
          f"(delay {DELAY_MIN}–{DELAY_MAX}s aleatorio)...\n")

    n_telefono = 0   # con teléfono capturado
    n_nombre   = 0   # con nombre (aunque sin tel)
    n_sin_nada = 0   # sin ningún dato
    n_db       = 0   # guardados en SQLite
    n_sheet    = 0   # guardados en Sheet
    errores    = 0

    HEADLESS_WARN_AFTER = 3

    iterable = tqdm(propiedades, unit="prop", ncols=80) if TQDM_AVAILABLE else propiedades

    async with async_playwright() as pw:
        for idx, prop in enumerate(iterable):
            id_zp  = prop["id"]
            url    = prop["url"]
            barrio = prop["barrio"]
            pm2    = prop["pm2"]
            pos    = idx + 1

            try:
                nombre, telefono = await fetch_telefono(
                    pw, url,
                    debug_path=DEBUG_SCREENSHOT,
                    headless=False,
                )
            except Exception as exc:
                errores += 1
                msg = f"  [{pos:>4}/{total}] ✗ ERROR | {barrio} | {exc}"
                if TQDM_AVAILABLE:
                    tqdm.write(msg)
                else:
                    print(msg)
                if idx < total - 1:
                    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                continue

            # Contadores
            if telefono:
                n_telefono += 1
            if nombre:
                n_nombre += 1
            if not telefono and not nombre:
                n_sin_nada += 1

            # Guardar inmediatamente
            if not dry_run and (nombre or telefono):
                if guardar_en_db(id_zp, nombre, telefono):
                    n_db += 1

                if sh:
                    try:
                        if actualizar_fila_sheets(sh, id_zp, nombre, telefono):
                            n_sheet += 1
                    except Exception as e:
                        print(f"\n  [!] Sheet error para {id_zp}: {e}")

            # Aviso si las primeras N no dan teléfono
            if pos == HEADLESS_WARN_AFTER:
                if n_telefono == 0:
                    print(
                        f"\n  ⚠️  AVISO: {HEADLESS_WARN_AFTER} propiedades procesadas y ningún "
                        f"teléfono obtenido. ZonaProp puede estar bloqueando el browser.\n"
                        f"  Continuando de todas formas.\n"
                    )

            # Log de la fila
            pm2_str = f"USD {pm2:,.0f}/m²" if pm2 else "sin m²"
            tel_str = f"📞 {telefono}" if telefono else "sin tel"
            nom_str = nombre or "-"

            if not TQDM_AVAILABLE:
                print(f"  [{pos:>4}/{total}] {barrio:<20} | {pm2_str} | {tel_str} | {nom_str}")
            else:
                if telefono or nombre:
                    tqdm.write(f"  [{pos:>4}/{total}] ✓ {barrio} | {tel_str} | {nom_str}")
                else:
                    tqdm.write(f"  [{pos:>4}/{total}] ✗ sin datos | {barrio}")

            if idx < total - 1:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                await asyncio.sleep(delay)

    # -----------------------------------------------------------------------
    # 4. Resumen
    # -----------------------------------------------------------------------
    elapsed = time.time() - t0
    mins    = int(elapsed // 60)
    segs    = int(elapsed % 60)

    print(f"\n{'=' * 65}")
    print("RESUMEN")
    print(f"{'=' * 65}")
    print(f"  Propiedades procesadas:  {total}")
    print(f"  Con teléfono:            {n_telefono}")
    print(f"  Con nombre (sin tel):    {n_nombre - n_telefono if n_nombre > n_telefono else 0}")
    print(f"  Sin ningún dato:         {n_sin_nada}")
    print(f"  Errores:                 {errores}")
    print(f"  Guardados en SQLite:     {n_db}")
    print(f"  Guardados en Sheet:      {n_sheet}")
    print(f"  Tiempo total:            {mins}m {segs}s")
    print(f"{'=' * 65}\n")

    tee.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill de teléfonos de publicantes en ZonaProp"
    )
    parser.add_argument(
        "--limite", type=int, default=None,
        help="Procesar solo las primeras N propiedades",
    )
    parser.add_argument(
        "--sin-sheets", action="store_true",
        help="No sincronizar con Google Sheets",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="No escribir nada (ni SQLite ni Sheet)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(
        limite=args.limite,
        sin_sheets=args.sin_sheets,
        dry_run=args.dry_run,
    ))
