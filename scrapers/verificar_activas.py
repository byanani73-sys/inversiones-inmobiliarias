"""
verificar_activas.py — Verifica si las propiedades activas siguen publicadas en ZonaProp.

Para cada propiedad con activa=1 (excepto estados terminales), visita la URL
con Playwright+stealth y detecta si fue dada de baja. Actualiza SQLite y Sheet.

Uso:
  python scrapers/verificar_activas.py
  python scrapers/verificar_activas.py --limite 50       # solo las primeras N
  python scrapers/verificar_activas.py --dias 7          # solo las ingresadas hace >7 días
  python scrapers/verificar_activas.py --sin-sheets      # no sync al Sheet
  python scrapers/verificar_activas.py --dry-run         # no escribe nada

Detección de baja:
  - HTTP 404 en la respuesta
  - URL final redirigida a la home (zonaprop.com.ar/)
  - URL final sin el slug de la propiedad original
  - Texto de página con frases de "no encontrada"
  - Ausencia del elemento de tipo/título del detalle
"""

import argparse
import asyncio
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.scraper_inicial import wait_for_cf
from scrapers.scraper_diario import (
    DB_PATH,
    LOCAL_BROWSER_ARGS, LOCAL_USER_AGENT, LOCAL_VIEWPORT,
    connect_sheets, cargar_sheet_state, col_letter,
)

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

_stealth = Stealth()

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
DELAY_ENTRE_REQUESTS = 2       # segundos entre visitas
TIMEOUT_MS           = 25_000  # timeout por navegación (ms)
CF_TIMEOUT_SECS      = 30      # tiempo max esperando pasar Cloudflare

# Estados que NO se verifican (ya resueltos)
ESTADOS_EXCLUIDOS = {
    "Captado",
    "Publicación desactivada",
    "Hablo - no interesado",
}

# Frases que ZonaProp muestra cuando una publicación fue dada de baja
NOT_FOUND_PHRASES = [
    "la publicación que buscás no existe",
    "esta publicación no está disponible",
    "esta publicación fue dada de baja",
    "publicación no encontrada",
    "no encontramos la publicación",
    "la propiedad que buscás no existe",
    "page not found",
]

ZONAPROP_HOME = "https://www.zonaprop.com.ar/"


def slug_de_url(url: str) -> str:
    """Extrae el slug final de la URL para comparar redirecciones."""
    # 'https://...zonaprop.com.ar/propiedades/veclappa-depto-12345678.html'
    # → 'veclappa-depto-12345678'
    m = re.search(r"/([^/]+)\.html", url)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Detección de baja — browser fresco por cada URL (igual que fetch_detail)
# ---------------------------------------------------------------------------
async def esta_activa(pw, url: str) -> tuple[bool | None, str]:
    """
    Abre un browser stealth nuevo, navega a url, determina si la publicación
    sigue activa y cierra el browser. Un browser fresco por request es la
    única estrategia que demostró funcionar contra Cloudflare en este proyecto.

    Devuelve:
      (True,  razón)  — sigue activa
      (False, razón)  — dada de baja / no encontrada
      (None,  razón)  — no se pudo determinar (CF, timeout, error de red)
    """
    browser = await pw.chromium.launch(headless=True, args=LOCAL_BROWSER_ARGS)
    try:
        context = await browser.new_context(
            user_agent=LOCAL_USER_AGENT,
            viewport=LOCAL_VIEWPORT,
            locale="es-AR",
        )
        page = await context.new_page()
        await _stealth.apply_stealth_async(page)

        try:
            response = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=TIMEOUT_MS)
        except Exception as e:
            return None, f"error navegación: {type(e).__name__}"

        # Esperar CF si aplica
        if not await wait_for_cf(page, timeout_secs=CF_TIMEOUT_SECS):
            return None, "Cloudflare bloqueó"

        await page.wait_for_timeout(1000)

        final_url = page.url

        # Señal 1 — HTTP 4xx
        if response and response.status >= 400:
            return False, f"HTTP {response.status}"

        # Señal 2 — redirigida a la home
        if final_url.rstrip("/") == ZONAPROP_HOME.rstrip("/"):
            return False, "redirigida a home"

        # Señal 3 — URL final no contiene el slug original
        slug_orig  = slug_de_url(url)
        slug_final = slug_de_url(final_url)
        if slug_orig and slug_final and slug_orig != slug_final:
            return False, f"URL cambió → {final_url[:70]}"

        # Señal 4 — texto de página indica "no encontrada"
        try:
            content = (await page.content()).lower()
        except Exception:
            content = ""
        for phrase in NOT_FOUND_PHRASES:
            if phrase in content:
                return False, f"texto: '{phrase}'"

        # Señal 5 — ausencia del elemento de detalle (title-type es exclusivo del detalle)
        try:
            title_el = await page.query_selector("[class*='title-type']")
            if not title_el:
                if "clasificado" not in final_url and "propiedades" not in final_url:
                    return False, "sin detalle + URL no es listing"
        except Exception:
            pass

        return True, "OK"

    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def cargar_propiedades_db(ids_validos: set | None, dias: int | None) -> list[dict]:
    """
    Carga de SQLite las propiedades activas a verificar.

    ids_validos  — si no es None, filtra solo esos IDs (viene del Sheet)
    dias         — si no es None, solo propiedades con
                   fecha_primera_vez <= hoy - N días (las más antiguas primero)
    """
    conn = sqlite3.connect(DB_PATH)

    where = "activa = 1 AND url IS NOT NULL"
    params: list = []
    if dias is not None:
        where += " AND DATE(fecha_primera_vez) <= DATE('now', ?)"
        params.append(f"-{dias} days")

    rows = conn.execute(
        f"""SELECT id_zonaprop, url, barrio_simple, precio_por_m2_tasable,
                   fecha_primera_vez
            FROM propiedades
            WHERE {where}
            ORDER BY fecha_primera_vez ASC""",
        params,
    ).fetchall()
    conn.close()

    props = []
    for id_zp, url, barrio, pm2, fecha in rows:
        if ids_validos is not None and id_zp not in ids_validos:
            continue
        props.append({
            "id":    id_zp,
            "url":   url,
            "barrio": barrio or "?",
            "pm2":   pm2 or 0,
            "fecha": (fecha or "")[:10],
        })
    return props


def marcar_inactivas_db(ids: list[str]) -> None:
    if not ids:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "UPDATE propiedades SET activa = 0 WHERE id_zonaprop = ?",
        [(id_,) for id_ in ids],
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Google Sheet sync
# ---------------------------------------------------------------------------
def sincronizar_desactivadas_sheets(
    sh: dict,
    ids_desactivadas: list[str],
    dry_run: bool = False,
) -> None:
    if not ids_desactivadas:
        print("  Sheet: nada que actualizar")
        return

    ws        = sh["ws"]
    h_idx     = sh["h_idx"]
    id_to_row = sh["id_to_row"]

    activa_i = h_idx.get("activa")
    estado_i = h_idx.get("estado")

    if activa_i is None and estado_i is None:
        print("  [!] Sheet: columnas 'activa' y 'estado' no encontradas")
        return

    updates = []
    sin_fila = []

    for id_zp in ids_desactivadas:
        row_num = id_to_row.get(id_zp)
        if not row_num:
            sin_fila.append(id_zp)
            continue
        if activa_i is not None:
            updates.append({
                "range":  f"{col_letter(activa_i)}{row_num}",
                "values": [[0]],
            })
        if estado_i is not None:
            updates.append({
                "range":  f"{col_letter(estado_i)}{row_num}",
                "values": [["Publicación desactivada"]],
            })

    if sin_fila:
        print(f"  [!] {len(sin_fila)} IDs desactivados no encontrados en Sheet (se ignoran)")

    if updates:
        if not dry_run:
            BATCH = 500
            for i in range(0, len(updates), BATCH):
                ws.batch_update(updates[i:i + BATCH])
                time.sleep(1)
            print(f"  ✓ {len(ids_desactivadas) - len(sin_fila)} filas actualizadas en Sheet "
                  f"(activa=0, estado='Publicación desactivada')")
        else:
            print(f"  [dry-run] {len(updates)} celdas a actualizar en Sheet")
    else:
        print("  Sheet: sin cambios")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(limite: int | None, dias: int | None, sin_sheets: bool, dry_run: bool) -> None:
    t0  = time.time()
    hoy = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 65}")
    print(f"VERIFICAR ACTIVAS — {hoy}")
    if dry_run:
        print("  [DRY-RUN: no se escribirá nada]")
    print(f"{'=' * 65}\n")

    # -----------------------------------------------------------------------
    # 1. Cargar estado del Sheet para filtrar por estado
    # -----------------------------------------------------------------------
    ids_validos: set | None = None
    sh = None

    if not sin_sheets:
        print("[1/4] Conectando al Sheet...")
        try:
            ws = connect_sheets()
            sh = cargar_sheet_state(ws)
            h_idx     = sh["h_idx"]
            id_to_row = sh["id_to_row"]
            all_vals  = ws.get_all_values()
            headers   = sh["headers"]
            estado_i  = h_idx.get("estado")

            # Construir set de IDs con estado no excluido
            ids_validos = set()
            for r_num, row in enumerate(all_vals, 1):
                if r_num == 1:
                    continue
                id_col = h_idx.get("id_zonaprop", 0)
                id_zp = row[id_col] if len(row) > id_col else ""
                if not id_zp:
                    continue
                estado = row[estado_i] if (estado_i is not None and len(row) > estado_i) else ""
                if estado not in ESTADOS_EXCLUIDOS:
                    ids_validos.add(id_zp)

            print(f"  {len(sh['id_to_row'])} IDs en Sheet | "
                  f"{len(ids_validos)} con estado a verificar")
        except Exception as e:
            print(f"  [!] Error conectando al Sheet: {e}")
            print("  Continuando sin filtro de estado (verificando todas las activas)")
            sh = None
    else:
        print("[1/4] Sin-sheets: verificando todas las activas sin filtro de estado")

    # -----------------------------------------------------------------------
    # 2. Cargar propiedades de SQLite
    # -----------------------------------------------------------------------
    print("\n[2/4] Cargando propiedades de SQLite...")
    propiedades = cargar_propiedades_db(ids_validos, dias)

    if limite:
        propiedades = propiedades[:limite]

    total = len(propiedades)
    filtro_dias = f" con fecha_primera_vez <= hoy - {dias}d" if dias else ""
    print(f"  {total} propiedades a verificar{filtro_dias}")

    if total == 0:
        print("\nNada que verificar.")
        return

    # -----------------------------------------------------------------------
    # 3. Verificar URLs — browser fresco por cada propiedad
    # -----------------------------------------------------------------------
    print(f"\n[3/4] Verificando {total} URLs "
          f"(browser fresco por request, delay {DELAY_ENTRE_REQUESTS}s)...\n")

    resultados   = {}   # id → True/False/None
    desactivadas = []

    # Barra de progreso (tqdm si disponible)
    if TQDM_AVAILABLE:
        iterable = tqdm(propiedades, unit="prop", ncols=80)
    else:
        iterable = propiedades

    async with async_playwright() as pw:
        for idx, prop in enumerate(iterable):
            id_zp  = prop["id"]
            url    = prop["url"]
            barrio = prop["barrio"]
            pm2    = prop["pm2"]
            fecha  = prop["fecha"]
            pos    = idx + 1

            activa, razon = await esta_activa(pw, url)
            resultados[id_zp] = activa

            pm2_str   = f"USD {pm2:,.0f}/m²" if pm2 else "sin m²"
            fecha_str = fecha or "?"

            if not TQDM_AVAILABLE:
                if activa is True:
                    print(f"  [{pos:>4}/{total}] ✓ activa      | {barrio:<20} | {pm2_str} | {fecha_str}")
                elif activa is False:
                    desactivadas.append(id_zp)
                    print(f"  [{pos:>4}/{total}] ✗ desactivada | {barrio:<20} | {pm2_str} | {fecha_str}")
                else:
                    print(f"  [{pos:>4}/{total}] ? indetermin. | {barrio:<20} | {pm2_str} — {razon}")
            else:
                if activa is False:
                    desactivadas.append(id_zp)
                    tqdm.write(f"  [{pos:>4}/{total}] ✗ desactivada | {barrio} | {pm2_str} | {fecha_str}")
                elif activa is None:
                    tqdm.write(f"  [{pos:>4}/{total}] ? indetermin. | {barrio} | {razon}")

            if idx < total - 1:
                await asyncio.sleep(DELAY_ENTRE_REQUESTS + random.uniform(0, 0.5))

    activas_count      = sum(1 for v in resultados.values() if v is True)
    desactivadas_count = len(desactivadas)
    indeterminadas     = sum(1 for v in resultados.values() if v is None)

    # -----------------------------------------------------------------------
    # 4. Escribir resultados
    # -----------------------------------------------------------------------
    print(f"\n[4/4] Guardando resultados...")

    if desactivadas:
        if not dry_run:
            marcar_inactivas_db(desactivadas)
            print(f"  ✓ SQLite: {desactivadas_count} propiedades marcadas activa=0")
        else:
            print(f"  [dry-run] SQLite: {desactivadas_count} propiedades a marcar activa=0")

        if not sin_sheets:
            if sh:
                sincronizar_desactivadas_sheets(sh, desactivadas, dry_run=dry_run)
            else:
                print("  Sheet: sin conexión, sync omitido")
    else:
        print("  Sin propiedades desactivadas — nada que escribir")

    # -----------------------------------------------------------------------
    # Resumen
    # -----------------------------------------------------------------------
    elapsed = time.time() - t0
    mins    = int(elapsed // 60)
    segs    = int(elapsed % 60)

    print(f"\n{'=' * 65}")
    print("RESUMEN")
    print(f"{'=' * 65}")
    print(f"  Verificadas:     {total}")
    print(f"  Activas:         {activas_count}")
    print(f"  Desactivadas:    {desactivadas_count}")
    print(f"  Indeterminadas:  {indeterminadas}  (CF / error de red)")
    print(f"  Tiempo total:    {mins}m {segs}s")
    print(f"{'=' * 65}\n")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Verifica si las propiedades activas siguen publicadas en ZonaProp"
    )
    parser.add_argument(
        "--limite", type=int, default=None,
        help="Verificar solo las primeras N propiedades (útil para pruebas)",
    )
    parser.add_argument(
        "--dias", type=int, default=None,
        metavar="N",
        help="Solo verificar propiedades ingresadas hace más de N días "
             "(prioriza las más antiguas, orden ASC por fecha_primera_vez)",
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
        dias=args.dias,
        sin_sheets=args.sin_sheets,
        dry_run=args.dry_run,
    ))
