"""
Visita el detalle de cada propiedad en ZonaProp y actualiza:
  - tipo_propiedad: extraído del h2 de la página (ej: "Departamento", "Cochera")
  - activa: 0 si la publicación fue dada de baja, 1 si sigue activa

Solo procesa registros con tipo_propiedad IS NULL, por lo que puede
interrumpirse y retomarse sin repetir trabajo ya hecho.

Valores especiales en tipo_propiedad:
  '_inactiva' → publicación dada de baja (activa = 0)
  '_error'    → falló la visita; se reintenta corriendo el script de nuevo
                (se puede limpiar con: UPDATE propiedades SET tipo_propiedad = NULL
                 WHERE tipo_propiedad = '_error')

Uso:
  python pipeline/actualizar_propiedades.py
  python pipeline/actualizar_propiedades.py --limite 20   # para pruebas
"""

import argparse
import asyncio
import random
import sqlite3
from datetime import datetime

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

stealth = Stealth()

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
DB_PATH = "data/zonaprop.db"

BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--headless=new",
]
CONTEXT_OPTS = dict(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    viewport={"width": 1366, "height": 768},
    locale="es-AR",
)

# Textos que ZonaProp muestra cuando una publicación fue dada de baja
TEXTOS_INACTIVA = [
    "esta publicación no está disponible",
    "este aviso no está disponible",
    "el aviso que buscas no está disponible",
    "aviso no disponible",
    "la publicación que buscas no está disponible",
    "publicación no disponible",
    "esta publicacion no esta disponible",
    "aviso no esta disponible",
]


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    """Agrega columnas nuevas si no existen en la DB."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(propiedades)")}

    if "tipo_propiedad" not in cols:
        conn.execute("ALTER TABLE propiedades ADD COLUMN tipo_propiedad TEXT")
        print("  → Columna tipo_propiedad agregada")

    if "activa" not in cols:
        conn.execute("ALTER TABLE propiedades ADD COLUMN activa INTEGER DEFAULT 1")
        conn.execute("UPDATE propiedades SET activa = 1 WHERE activa IS NULL")
        print("  → Columna activa agregada (todos los registros marcados como activos)")

    conn.commit()


def fetch_pendientes(conn: sqlite3.Connection, limite: int | None = None) -> list[dict]:
    """Retorna registros con tipo_propiedad IS NULL y URL disponible."""
    q = """
        SELECT id_zonaprop, url, barrio, precio_actual, moneda
        FROM propiedades
        WHERE tipo_propiedad IS NULL
          AND url IS NOT NULL
        ORDER BY fecha_primera_vez ASC
    """
    if limite:
        q += f" LIMIT {limite}"
    cols = ["id_zonaprop", "url", "barrio", "precio_actual", "moneda"]
    return [dict(zip(cols, row)) for row in conn.execute(q).fetchall()]


def guardar_tipo(conn: sqlite3.Connection, id_zonaprop: str, tipo: str) -> None:
    conn.execute(
        "UPDATE propiedades SET tipo_propiedad = ?, activa = 1 WHERE id_zonaprop = ?",
        (tipo, id_zonaprop),
    )
    conn.commit()


def marcar_inactiva(conn: sqlite3.Connection, id_zonaprop: str) -> None:
    conn.execute(
        "UPDATE propiedades SET tipo_propiedad = '_inactiva', activa = 0 WHERE id_zonaprop = ?",
        (id_zonaprop,),
    )
    conn.commit()


def marcar_error(conn: sqlite3.Connection, id_zonaprop: str) -> None:
    """
    Marca con '_error' para no repetir en la misma corrida.
    El usuario puede reintentar errores con:
      UPDATE propiedades SET tipo_propiedad = NULL WHERE tipo_propiedad = '_error'
    """
    conn.execute(
        "UPDATE propiedades SET tipo_propiedad = '_error' WHERE id_zonaprop = ?",
        (id_zonaprop,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------
async def new_stealth_page(pw):
    """Abre un browser + context + page frescos con stealth aplicado."""
    browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
    context = await browser.new_context(**CONTEXT_OPTS)
    page = await context.new_page()
    await stealth.apply_stealth_async(page)
    return browser, page


async def wait_for_cf(page, timeout_secs: int = 25) -> bool:
    """Espera a que Cloudflare libere la página. Devuelve True si pasó."""
    for _ in range(timeout_secs):
        title = await page.title()
        if "moment" not in title.lower() and "cloudflare" not in title.lower():
            return True
        await page.wait_for_timeout(1000)
    return False


async def visitar_detalle(pw, url: str) -> dict:
    """
    Visita la URL en un browser fresco y extrae:
      activa       → bool
      tipo_propiedad → str | None
    """
    browser, page = await new_stealth_page(pw)
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 404 directo
        if response and response.status == 404:
            return {"activa": False, "tipo_propiedad": None}

        passed = await wait_for_cf(page)
        if not passed:
            raise TimeoutError("Cloudflare no se resolvió en el tiempo esperado")

        await page.wait_for_timeout(1500)

        # Detectar baja por texto en la página
        body_text = (await page.inner_text("body")).lower()
        if any(t in body_text for t in TEXTOS_INACTIVA):
            return {"activa": False, "tipo_propiedad": None}

        # Extraer tipo de propiedad del h2 con clase title-type
        # Ej: "Departamento · 75m² · 3 ambientes" → "Departamento"
        tipo = None
        tipo_el = await page.query_selector("[class*='title-type']")
        if tipo_el:
            texto = (await tipo_el.inner_text()).strip()
            candidato = texto.split("·")[0].strip()
            tipo = candidato if candidato else None

        return {"activa": True, "tipo_propiedad": tipo}

    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(limite: int | None = None) -> None:
    conn = sqlite3.connect(DB_PATH)

    print("\nVerificando schema de la DB...")
    init_db(conn)

    pendientes = fetch_pendientes(conn, limite)
    total = len(pendientes)
    total_db = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]

    if not pendientes:
        print("\nNo hay registros pendientes (tipo_propiedad IS NULL). Nada que hacer.")
        conn.close()
        return

    print(f"\nRegistros en DB:          {total_db}")
    print(f"Pendientes a procesar:    {total}")
    if limite:
        print(f"Límite aplicado:          {limite}")
    print(f"Inicio:                   {datetime.now().strftime('%H:%M:%S')}")
    print("-" * 65)

    n_actualizados = 0
    n_inactivos = 0
    n_errores = 0

    async with async_playwright() as pw:
        for i, reg in enumerate(pendientes, 1):
            id_zp    = reg["id_zonaprop"]
            url      = reg["url"]
            barrio   = (reg["barrio"] or "?").split(",")[0][:18]
            moneda   = reg["moneda"] or ""
            precio   = reg["precio_actual"]
            precio_s = f"{moneda} {precio:,}" if precio else "?"

            try:
                res = await visitar_detalle(pw, url)

                if not res["activa"]:
                    marcar_inactiva(conn, id_zp)
                    n_inactivos += 1
                    print(f"[{i:>4}/{total}] INACTIVA          | {barrio}")

                else:
                    tipo = res["tipo_propiedad"] or "?"
                    guardar_tipo(conn, id_zp, tipo)
                    n_actualizados += 1
                    print(f"[{i:>4}/{total}] {tipo:<18} | {barrio:<18} {precio_s} ✓")

            except Exception as e:
                marcar_error(conn, id_zp)
                n_errores += 1
                print(f"[{i:>4}/{total}] ERROR              | {str(e)[:55]}")

            # Delay entre visitas (evita bloqueos)
            if i < total:
                await asyncio.sleep(random.uniform(2, 4))

    conn.close()

    elapsed_note = f"Finalizado: {datetime.now().strftime('%H:%M:%S')}"
    print("\n" + "=" * 65)
    print("RESUMEN  —  " + elapsed_note)
    print(f"  Actualizados (tipo extraído):  {n_actualizados}")
    print(f"  Inactivos (dados de baja):     {n_inactivos}")
    print(f"  Errores:                       {n_errores}")
    print(f"  Total procesados:              {n_actualizados + n_inactivos + n_errores} / {total}")
    if n_errores:
        print()
        print("  Para reintentar errores:")
        print("    python -c \"import sqlite3; c=sqlite3.connect('data/zonaprop.db'); "
              "c.execute(\\\"UPDATE propiedades SET tipo_propiedad=NULL WHERE tipo_propiedad='_error'\\\"); "
              "c.commit()\"")
    print("=" * 65)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Actualiza tipo_propiedad y activa visitando cada detalle en ZonaProp"
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Procesar solo los primeros N registros pendientes (útil para pruebas)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(limite=args.limite))
