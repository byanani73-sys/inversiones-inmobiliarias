import argparse
import asyncio
import random
import re
import sqlite3
from datetime import datetime

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

stealth = Stealth()

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
BASE_URL = "https://www.zonaprop.com.ar/inmuebles-dueno-directo-capital-federal.html"
PALABRAS_COCHERA = ["cochera", "garage", "garaje"]
DB_PATH = "data/zonaprop.db"

# Limites para pruebas (None = sin limite)
MAX_PAGES = None
MAX_DETAILS = None

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

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS propiedades (
    id_zonaprop         TEXT PRIMARY KEY,
    url                 TEXT,
    fecha_primera_vez   TEXT,
    precio_actual       INTEGER,
    moneda              TEXT,
    expensas            INTEGER,
    m2_totales          INTEGER,
    m2_cubiertos        INTEGER,
    ambientes           INTEGER,
    dormitorios         INTEGER,
    banos               INTEGER,
    toilettes           INTEGER,
    cocheras            INTEGER,
    antiguedad          TEXT,
    disposicion         TEXT,
    orientacion         TEXT,
    luminosidad         TEXT,
    direccion           TEXT,
    barrio              TEXT,
    descripcion         TEXT,
    fecha_actualizacion TEXT,
    tipo_propiedad      TEXT
)
"""

INSERT_ROW = """
INSERT INTO propiedades (
    id_zonaprop, url, fecha_primera_vez, precio_actual, moneda, expensas,
    m2_totales, m2_cubiertos, ambientes, dormitorios, banos, toilettes,
    cocheras, antiguedad, disposicion, orientacion, luminosidad,
    direccion, barrio, descripcion, fecha_actualizacion, tipo_propiedad
) VALUES (
    :id_zonaprop, :url, :fecha_primera_vez, :precio_actual, :moneda, :expensas,
    :m2_totales, :m2_cubiertos, :ambientes, :dormitorios, :banos, :toilettes,
    :cocheras, :antiguedad, :disposicion, :orientacion, :luminosidad,
    :direccion, :barrio, :descripcion, :fecha_actualizacion, :tipo_propiedad
)
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_TABLE)
    # Migración: agregar columna si la DB ya existía sin ella
    cols = {row[1] for row in conn.execute("PRAGMA table_info(propiedades)")}
    if "tipo_propiedad" not in cols:
        conn.execute("ALTER TABLE propiedades ADD COLUMN tipo_propiedad TEXT")
    conn.commit()
    return conn


def es_cochera(card: dict, detail: dict) -> bool:
    """Devuelve True si el anuncio parece una cochera/garaje."""
    for campo in ("descripcion", "direccion"):
        texto = (card.get(campo) or "").lower()
        if any(p in texto for p in PALABRAS_COCHERA):
            return True
    return False


def id_exists(conn, id_zonaprop):
    row = conn.execute(
        "SELECT 1 FROM propiedades WHERE id_zonaprop = ?", (id_zonaprop,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Helpers de parseo
# ---------------------------------------------------------------------------
def parse_price(text):
    if not text:
        return None, None
    text = text.strip()
    match = re.match(r"(USD|\$|ARS)\s*([\d.]+)", text)
    if not match:
        return None, None
    moneda = "USD" if match.group(1) == "USD" else "ARS"
    monto = int(match.group(2).replace(".", ""))
    return moneda, monto


def parse_expenses(text):
    if not text:
        return None
    digits = re.findall(r"\d+", text.replace(".", ""))
    return int(digits[0]) if digits else None


def parse_int(text):
    if not text:
        return None
    nums = re.findall(r"\d+", text)
    return int(nums[0]) if nums else None


# ---------------------------------------------------------------------------
# Esperar Cloudflare
# ---------------------------------------------------------------------------
async def wait_for_cf(page, timeout_secs=60):
    for _ in range(timeout_secs):
        title = await page.title()
        if "moment" not in title.lower() and "cloudflare" not in title.lower():
            return True
        await page.wait_for_timeout(1000)
    return False


# ---------------------------------------------------------------------------
# Crear pagina con stealth
# ---------------------------------------------------------------------------
async def new_stealth_page(playwright_instance):
    """Crea un browser + context + page con stealth aplicado."""
    browser = await playwright_instance.chromium.launch(
        headless=True, args=BROWSER_ARGS,
    )
    context = await browser.new_context(**CONTEXT_OPTS)
    page = await context.new_page()
    await stealth.apply_stealth_async(page)
    return browser, page


# ---------------------------------------------------------------------------
# Extraccion del listado
# ---------------------------------------------------------------------------
async def extract_cards(page):
    cards = await page.query_selector_all('div[data-qa="posting PROPERTY"]')
    results = []
    for card in cards:
        id_zp = await card.get_attribute("data-id")
        url_rel = await card.get_attribute("data-to-posting")
        if url_rel and "?" in url_rel:
            url_rel = url_rel.split("?")[0]
        url = f"https://www.zonaprop.com.ar{url_rel}" if url_rel else None

        precio_el = await card.query_selector('[data-qa="POSTING_CARD_PRICE"]')
        precio_text = await precio_el.inner_text() if precio_el else None
        moneda, precio = parse_price(precio_text)

        expensas_el = await card.query_selector('[data-qa="expensas"]')
        expensas_text = await expensas_el.inner_text() if expensas_el else None
        expensas = parse_expenses(expensas_text)

        features_els = await card.query_selector_all(
            '[data-qa="POSTING_CARD_FEATURES"] span'
        )
        features = {}
        for fel in features_els:
            ft = (await fel.inner_text()).strip()
            if "tot" in ft:
                features["m2_totales"] = parse_int(ft)
            elif "amb" in ft:
                features["ambientes"] = parse_int(ft)
            elif "dorm" in ft:
                features["dormitorios"] = parse_int(ft)
            elif "ba" in ft.lower():
                features["banos"] = parse_int(ft)
            elif "coch" in ft:
                features["cocheras"] = parse_int(ft)

        dir_el = await card.query_selector(
            "h4[class*='postingLocations-module__location-address']"
        )
        direccion = await dir_el.inner_text() if dir_el else None

        barrio_el = await card.query_selector('[data-qa="POSTING_CARD_LOCATION"]')
        barrio = await barrio_el.inner_text() if barrio_el else None

        desc_el = await card.query_selector('[data-qa="POSTING_CARD_DESCRIPTION"] a')
        descripcion = await desc_el.inner_text() if desc_el else None

        results.append({
            "id_zonaprop": id_zp,
            "url": url,
            "moneda": moneda,
            "precio_actual": precio,
            "expensas": expensas,
            "direccion": direccion,
            "barrio": barrio,
            "descripcion": descripcion,
            **features,
        })
    return results


# ---------------------------------------------------------------------------
# Extraccion del detalle
# ---------------------------------------------------------------------------
DETAIL_FIELDS = {
    "icon-scubierta": "m2_cubiertos",
    "icon-stotal": "m2_totales",
    "icon-ambiente": "ambientes",
    "icon-dormitorio": "dormitorios",
    "icon-bano": "banos",
    "icon-toilete": "toilettes",
    "icon-cochera": "cocheras",
    "icon-antiguedad": "antiguedad",
    "icon-disposicion": "disposicion",
    "icon-orientacion": "orientacion",
    "icon-luminosidad": "luminosidad",
}

INT_FIELDS = {
    "m2_cubiertos", "m2_totales", "ambientes",
    "dormitorios", "banos", "cocheras", "toilettes",
}


async def extract_detail_fields(page):
    detail = {}

    # Tipo de propiedad: primer token del h2 con clase title-type
    # Ej: "Departamento · 75m² · 3 ambientes" → "Departamento"
    tipo_el = await page.query_selector("[class*='title-type']")
    if tipo_el:
        texto = (await tipo_el.inner_text()).strip()
        detail["tipo_propiedad"] = texto.split("·")[0].strip() or None

    features = await page.query_selector_all("li.icon-feature")
    for feat in features:
        for icon_class, field_name in DETAIL_FIELDS.items():
            icon = await feat.query_selector(f"i.{icon_class}")
            if icon:
                text = (await feat.inner_text()).strip()
                if field_name in INT_FIELDS:
                    detail[field_name] = parse_int(text)
                else:
                    detail[field_name] = text
                break
    return detail


async def fetch_detail(pw, url):
    """Abre un browser stealth nuevo, carga la URL, extrae y cierra.

    Cloudflare permite exactamente una navegacion por sesion de browser
    en modo headless. Por eso cada detalle se abre en un browser fresco.
    """
    browser, page = await new_stealth_page(pw)
    try:
        await page.goto(url, wait_until="domcontentloaded")
        passed = await wait_for_cf(page)
        if not passed:
            return {}
        await page.wait_for_timeout(2000)
        return await extract_detail_fields(page)
    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(limite=None):
    conn = init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_inserted = 0
    total_skipped = 0
    detail_ok = 0
    detail_fail = 0
    details_visited = 0

    async with async_playwright() as pw:
        # --- Browser principal para el listado ---
        list_browser, list_page = await new_stealth_page(pw)

        await list_page.goto(BASE_URL, wait_until="domcontentloaded")
        passed = await wait_for_cf(list_page)
        if not passed:
            print("[X] No se pudo pasar Cloudflare en la primera carga")
            await list_browser.close()
            conn.close()
            return
        await list_page.wait_for_timeout(2000)

        page_num = 1

        while True:
            print(f"\n--- Pagina {page_num} ---")

            cards = await extract_cards(list_page)
            if not cards:
                print("Sin tarjetas, fin del listado.")
                break

            page_new = 0
            page_skip = 0

            for card in cards:
                id_zp = card["id_zonaprop"]
                if not id_zp:
                    continue

                if id_exists(conn, id_zp):
                    page_skip += 1
                    total_skipped += 1
                    continue

                # Verificar limite de detalles
                if MAX_DETAILS is not None and details_visited >= MAX_DETAILS:
                    page_skip += 1
                    total_skipped += 1
                    continue

                # --- Visitar detalle en browser fresco ---
                detail_data = {}
                detail_url = card.get("url")
                if detail_url:
                    try:
                        await asyncio.sleep(random.uniform(3, 7))
                        detail_data = await fetch_detail(pw, detail_url)
                        details_visited += 1
                        if detail_data:
                            detail_ok += 1
                        else:
                            detail_fail += 1
                    except Exception as e:
                        detail_fail += 1
                        print(f"  [!] Error detalle {id_zp}: {e}")

                # Construir row: detalle tiene prioridad
                row = {
                    "id_zonaprop": id_zp,
                    "url": detail_url,
                    "fecha_primera_vez": now,
                    "precio_actual": card.get("precio_actual"),
                    "moneda": card.get("moneda"),
                    "expensas": card.get("expensas"),
                    "m2_totales": detail_data.get("m2_totales")
                    or card.get("m2_totales"),
                    "m2_cubiertos": detail_data.get("m2_cubiertos"),
                    "ambientes": detail_data.get("ambientes")
                    or card.get("ambientes"),
                    "dormitorios": detail_data.get("dormitorios")
                    or card.get("dormitorios"),
                    "banos": detail_data.get("banos")
                    or card.get("banos"),
                    "toilettes": detail_data.get("toilettes"),
                    "cocheras": detail_data.get("cocheras")
                    or card.get("cocheras"),
                    "antiguedad": detail_data.get("antiguedad"),
                    "disposicion": detail_data.get("disposicion"),
                    "orientacion": detail_data.get("orientacion"),
                    "luminosidad": detail_data.get("luminosidad"),
                    "direccion": card.get("direccion"),
                    "barrio": card.get("barrio"),
                    "descripcion": card.get("descripcion"),
                    "fecha_actualizacion": now,
                    "tipo_propiedad": detail_data.get("tipo_propiedad"),
                }

                # Filtrar cocheras antes de insertar
                if es_cochera(card, detail_data):
                    page_skip += 1
                    total_skipped += 1
                    print(f"  [cochera] Salteado {id_zp} — {card.get('descripcion', '')[:60]}")
                    continue

                try:
                    conn.execute(INSERT_ROW, row)
                    conn.commit()
                    page_new += 1
                    total_inserted += 1
                    barrio_short = (card.get("barrio") or "?")[:25]
                    moneda_str = card.get("moneda") or "?"
                    precio_str = (
                        f"{card.get('precio_actual'):,}"
                        if card.get("precio_actual")
                        else "?"
                    )
                    m2c = detail_data.get("m2_cubiertos", "-")
                    print(
                        f"  Anuncio {total_inserted + total_skipped} | "
                        f"Nuevo: {barrio_short} {moneda_str} {precio_str} | "
                        f"{m2c} m2 cub"
                    )
                    # Frenar si alcanzamos el limite de inserciones
                    if limite is not None and total_inserted >= limite:
                        break
                except sqlite3.IntegrityError:
                    page_skip += 1
                    total_skipped += 1

            print(
                f"Pagina {page_num}: "
                f"+{page_new} nuevos, ={page_skip} salteados, "
                f"{len(cards)} tarjetas"
            )

            # Limite de inserciones alcanzado
            if limite is not None and total_inserted >= limite:
                print(f"\nLimite alcanzado: {total_inserted} insertados.")
                break

            # Limite de paginas
            if MAX_PAGES is not None and page_num >= MAX_PAGES:
                print(f"\nLimite de prueba: {MAX_PAGES} paginas.")
                break

            # Click en "Siguiente" (se mantiene en la sesion CF del listado)
            next_btn = await list_page.query_selector('[data-qa="PAGING_NEXT"]')
            if not next_btn:
                print("\nNo hay boton 'siguiente', fin del listado.")
                break

            await asyncio.sleep(random.uniform(2, 4))
            await next_btn.click()
            await list_page.wait_for_load_state("domcontentloaded")
            passed = await wait_for_cf(list_page)
            if not passed:
                print("\n[X] CF bloqueo la pagina siguiente, terminando.")
                break
            await list_page.wait_for_timeout(2000)
            page_num += 1

        await list_browser.close()

    conn.close()
    print(f"\n{'=' * 50}")
    print("RESUMEN")
    print(f"  Insertados:     {total_inserted}")
    print(f"  Salteados:      {total_skipped}")
    print(f"  Detalles OK:    {detail_ok}")
    print(f"  Detalles Fail:  {detail_fail}")
    print(f"  DB: {DB_PATH}")
    print("=" * 50)


def parse_args():
    parser = argparse.ArgumentParser(description="Scraper inicial de ZonaProp")
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Frenar despues de insertar esta cantidad de registros nuevos",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(limite=args.limite))
