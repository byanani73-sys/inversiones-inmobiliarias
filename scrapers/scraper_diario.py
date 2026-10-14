"""
scraper_diario.py — Actualización diaria de ZonaProp

Fases:
  1. Resetear es_nuevo
  2. Recorrer listado (orden fecha desc) y detallar nuevas inline
  3. Actualizar precios de propiedades existentes
  4. Sincronizar Google Sheets (nuevos, precios)

Modos:
  - Local (default):       usa SQLite como fuente de verdad, sincroniza Sheets al final
  - GitHub Actions (auto): usa Google Sheets como fuente de verdad (sin SQLite)

Uso:
  python scrapers/scraper_diario.py
  python scrapers/scraper_diario.py --max-paginas 5   # prueba limitada
  python scrapers/scraper_diario.py --sin-sheets       # omitir sync con Sheets (solo local)
"""

import argparse
import asyncio
import os
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

_stealth = Stealth()

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.scraper_inicial import (
    extract_cards, fetch_detail, wait_for_cf,
    parse_price, parse_int,
)
from pipeline.mapear_barrios import resolver_barrio

# ---------------------------------------------------------------------------
# Parseo de tipo de operación y tipo de propiedad desde la URL
# ---------------------------------------------------------------------------
# Formato del slug de 8 chars en /clasificado/XXXXXXXX-...:
#   pos 0-1  operación  (ve / al / te)
#   pos 2-3  "cl"       (fijo, ignorar)
#   pos 4-7  tipo       (appa / phpa / capa / ...)
_TIPO_OPERACION_MAP = {
    "ve": "Venta",
    "al": "Alquiler",
    "te": "Alquiler Temporario",
}
_TIPO_PROPIEDAD_MAP = {
    "appa": "Departamento",
    "phpa": "PH",
    "copa": "Consultorio",
    "capa": "Casa",
    "ocpa": "Oficina",
    "fcpa": "Fondo de Comercio",
    "gapa": "Cochera",
    "trpa": "Terreno",
    "edpa": "Edificio",
    "lcpa": "Local Comercial",
    "bgpa": "Galpón",
    "bnpa": "Bóveda",
    "depa": "Depósito",
    "htpa": "Hotel",
}

import re as _slug_re
_SLUG_RE = _slug_re.compile(r"/clasificado/([a-z]{8})-")

def parsear_slug(url: str) -> tuple[str | None, str | None]:
    """
    Devuelve (tipo_operacion, tipo_propiedad) extraídos del slug de la URL.
    Si el slug no se reconoce devuelve (None, None).
    Si el código de tipo no está en la tabla devuelve tipo = 'Otro'.
    """
    if not url:
        return None, None
    m = _SLUG_RE.search(url)
    if not m:
        return None, None
    slug = m.group(1)
    ops  = _TIPO_OPERACION_MAP.get(slug[0:2])
    tipo = _TIPO_PROPIEDAD_MAP.get(slug[4:8], "Otro")
    return ops, tipo

# ---------------------------------------------------------------------------
# Browser config — misma config que scraper_inicial para local,
# args reforzados solo en GitHub Actions
# ---------------------------------------------------------------------------
LOCAL_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--headless=new",
]
LOCAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
LOCAL_VIEWPORT = {"width": 1366, "height": 768}

CI_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-zygote",
    "--single-process",
    "--window-size=1280,800",
]
CI_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CI_VIEWPORT = {"width": 1280, "height": 800}


async def new_stealth_page_diario(pw):
    """Browser con config adaptada al entorno (local vs CI)."""
    if USAR_SHEETS_COMO_DB:  # GitHub Actions
        args = CI_BROWSER_ARGS
        ua = CI_USER_AGENT
        vp = CI_VIEWPORT
    else:  # Local (Mac/Windows)
        args = LOCAL_BROWSER_ARGS
        ua = LOCAL_USER_AGENT
        vp = LOCAL_VIEWPORT

    browser = await pw.chromium.launch(headless=True, args=args)
    context = await browser.new_context(
        user_agent=ua,
        viewport=vp,
        locale="es-AR",
    )
    page = await context.new_page()
    await _stealth.apply_stealth_async(page)
    return browser, page


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
BASE_URL         = "https://www.zonaprop.com.ar/inmuebles-venta-capital-federal-orden-publicado-descendente-dueno-directo.html"
DB_PATH          = "data/zonaprop.db"
CREDENTIALS_PATH = "credentials.json"
SHEET_ID         = "13fWYaAwwe9qyVqfb08Qhu_zusak_uVvTQv3-1rhfwDA"
WORKSHEET_NAME   = "Hoja 1"

USAR_SHEETS_COMO_DB = os.environ.get("GITHUB_ACTIONS") == "true"

# Columnas que el usuario edita directamente en el Sheet.
# NUNCA se sobreescriben en filas existentes; en filas nuevas reciben defaults.
COLS_USUARIO = {
    "estado",
    "nota",
    "proxima_llamada",
    "notas_llamada",
    "telefono_publicante",
    "nombre_publicante",
}

# Parada temprana: si se recorren N páginas consecutivas sin encontrar
# ninguna propiedad nueva, el scraper asume que ya pasó la frontera
# de publicaciones recientes y detiene el escaneo.
PAGINAS_SIN_NUEVAS_LIMITE = 5

COLUMNAS_REQUERIDAS = [
    ("es_nuevo",              "INTEGER DEFAULT 0"),
    ("bajo_precio",           "INTEGER DEFAULT 0"),
    ("precio_anterior",       "INTEGER"),
    ("activa",                "INTEGER DEFAULT 1"),
    ("tipo_operacion",        "TEXT"),
    ("tipo_propiedad",        "TEXT"),
    ("barrio_simple",         "TEXT"),
    ("orden_barrio",          "INTEGER"),
    ("m2_descubiertos",       "INTEGER"),
    ("m2_tasables",           "INTEGER"),
    ("precio_por_m2_total",   "REAL"),
    ("precio_por_m2_tasable", "REAL"),
    ("nombre_publicante",     "TEXT"),
    ("telefono_publicante",   "TEXT"),
]


# ---------------------------------------------------------------------------
# Cálculo de m² y precios
# ---------------------------------------------------------------------------
def calcular_columnas(m2_totales, m2_cubiertos, precio_actual):
    if m2_cubiertos is None or m2_totales is None or precio_actual is None:
        return None, None, None, None
    if m2_cubiertos > m2_totales:
        return None, None, None, None
    m2_desc = m2_totales - m2_cubiertos
    if m2_desc <= 25:
        m2_tas = m2_cubiertos + round(m2_desc * 0.5)
    else:
        m2_tas = m2_cubiertos + round(m2_desc * 0.33)
    pm2_total   = round(precio_actual / m2_totales, 2) if m2_totales > 0 else None
    pm2_tasable = round(precio_actual / m2_tas,    2) if m2_tas    > 0 else None
    return m2_desc, m2_tas, pm2_total, pm2_tasable


# ---------------------------------------------------------------------------
# Datos ficticios rotativos para el formulario de contacto de ZonaProp
# ---------------------------------------------------------------------------
FAKE_CONTACTS = [
    {"nombre": "Martín García",       "email": "martin.garcia.baires@gmail.com",  "telefono": "1140123456"},
    {"nombre": "Laura Fernández",     "email": "laurafernandez87@hotmail.com",     "telefono": "1152347890"},
    {"nombre": "Diego Rodríguez",     "email": "diego.rodriguez.caba@gmail.com",   "telefono": "1163456789"},
    {"nombre": "Ana Gómez",           "email": "ana.gomez.2024@gmail.com",         "telefono": "1174561234"},
    {"nombre": "Pablo López",         "email": "pablolopez.ba@outlook.com",        "telefono": "1185672345"},
    {"nombre": "Valentina Torres",    "email": "valen.torres.arg@gmail.com",       "telefono": "1196783456"},
    {"nombre": "Sebastián Martínez",  "email": "seba.martinez.bsas@gmail.com",     "telefono": "1107894567"},
    {"nombre": "Carolina Sánchez",    "email": "caro.sanchez.ok@hotmail.com",      "telefono": "1118905678"},
    {"nombre": "Facundo Pérez",       "email": "facundo.perez.cba@gmail.com",      "telefono": "1129016789"},
    {"nombre": "Sofía Romero",        "email": "sofia.romero.arg@gmail.com",       "telefono": "1130127890"},
]
_fake_idx = 0

def next_fake_contact() -> dict:
    global _fake_idx
    c = FAKE_CONTACTS[_fake_idx % len(FAKE_CONTACTS)]
    _fake_idx += 1
    return c


async def fetch_telefono(
    pw,
    url: str,
    debug_path: str | None = None,
    headless: bool = False,
) -> tuple[str | None, str | None]:
    """
    Abre un browser stealth nuevo, navega a la página de la propiedad,
    hace click en "Ver teléfono", completa el formulario de contacto si
    aparece (con datos ficticios rotativos), y extrae el nombre y teléfono
    del publicante.

    Devuelve: (nombre_publicante, telefono_publicante)
      — cualquiera puede ser None si no se encontró.
    Si hay bloqueo o error irrecuperable loguea y devuelve (None, None).

    debug_path — si se especifica, guarda un screenshot en esa ruta
                 cada vez que no se logra obtener el teléfono.
    headless   — False (default) usa browser visible, que bypasea Cloudflare.
                 True para entornos sin display (launchd/CI); el teléfono
                 probablemente no se obtendrá pero el nombre sí.
    """
    TIMEOUT_NAV = 25_000
    TIMEOUT_EL  = 6_000

    async def _screenshot(page):
        if not debug_path:
            return
        try:
            from pathlib import Path as _Path
            _Path(debug_path).parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=debug_path, full_page=True)
            print(f"    [debug] Screenshot guardado en {debug_path}")
        except Exception as se:
            print(f"    [debug] No se pudo guardar screenshot: {se}")

    # Selectores para el botón "Ver teléfono"
    BTN_SELS = [
        '[data-qa="PHONE_WHATSAPP_CONTACT"]',
        '[data-qa="LEAD_PHONE"]',
        '[data-qa="CONTACT_PHONE"]',
        'button:has-text("Ver teléfono")',
        'a:has-text("Ver teléfono")',
        '[class*="phone-btn"]',
        '[class*="phoneBtn"]',
        '[class*="show-phone"]',
        '[class*="action-phone"]',
    ]
    # Selectores para extraer el teléfono ya visible
    PHONE_SELS = [
        'a[href^="tel:"]',
        '[data-qa="PHONE_NUMBER"]',
        '[data-qa="PHONE_VALUE"]',
        '[class*="phone-number"]',
        '[class*="phoneNumber"]',
        '[class*="call-value"]',
        '[class*="number-phone"]',
    ]
    # Selectores para el nombre del publicante
    NOMBRE_SELS = [
        '[data-qa="PUBLISHER_NAME"]',
        '[data-qa="OWNER_NAME"]',
        '[class*="publisher-name"]',
        '[class*="publisherName"]',
        '[class*="owner-name"]',
        '[class*="contact-name"]',
        '[class*="advertiser-name"]',
    ]

    import re as _re

    browser_args = LOCAL_BROWSER_ARGS if headless else [
        "--disable-blink-features=AutomationControlled",
    ]
    browser = await pw.chromium.launch(headless=headless, args=browser_args)
    try:
        # Minimizar la ventana del browser en Mac para que no moleste al usuario.
        # Se prueba con "Chromium" primero (Playwright usa Chromium empaquetado);
        # si falla, se intenta con "Google Chrome".
        if not headless:
            import subprocess as _sp
            await asyncio.sleep(0.5)
            for _app in ("Chromium", "Google Chrome"):
                _r = _sp.run(
                    ["osascript", "-e",
                     f'tell application "{_app}" to set miniaturized of every window to true'],
                    capture_output=True,
                )
                if _r.returncode == 0:
                    break

        context = await browser.new_context(
            user_agent=LOCAL_USER_AGENT,
            viewport=LOCAL_VIEWPORT,
            locale="es-AR",
        )
        page = await context.new_page()
        await _stealth.apply_stealth_async(page)

        # ── Interceptar respuestas de red para capturar el teléfono ──────────
        # ZonaProp retorna el teléfono en una respuesta JSON tras el submit del
        # formulario de contacto. Se activa solo DESPUÉS de hacer submit para
        # evitar falsos positivos de las requests de carga inicial de la página.
        phone_from_api    = [None]   # lista para poder modificar desde la closure
        capture_active    = [False]  # se activa solo tras el submit
        FAKE_PHONE_STRS   = {c["telefono"] for c in FAKE_CONTACTS}
        # Patrón estricto: números argentinos de 10-13 dígitos con prefijo válido
        AR_PHONE_RE = _re.compile(
            r'(?:(?:\+54|0054)\s*)?'         # prefijo internacional opcional
            r'(?:0?(?:11|15|[2-9]\d{1,3}))'  # código de área (11=CABA, 15=cel, etc.)
            r'[\s\-]?\d{4}[\s\-]?\d{4}'      # número local (8 dígitos)
        )

        # Solo aceptar respuestas del propio dominio de ZonaProp.
        # Cualquier número capturado de terceros (Google Ads, New Relic,
        # reCAPTCHA, analytics, etc.) es un falso positivo.
        ZONAPROP_DOMAIN = "zonaprop.com.ar"

        # Endpoints de ZonaProp con falsos positivos (precios, IDs, fechas)
        SKIP_ZP_PATHS = ("/recommended", "/similar", "/related")

        # Prefijos argentinos válidos (dígitos iniciales del número limpio)
        AR_VALID_PREFIXES = ("11", "15", "54", "91", "01", "549")

        # El endpoint que efectivamente devuelve el teléfono del publicante
        LEADS_ENDPOINT = "rp-api/leads"

        async def intercept_response(response):
            if phone_from_api[0] or not capture_active[0]:
                return
            try:
                url_r = response.url
                # Ignorar todo lo que no sea zonaprop.com.ar
                if ZONAPROP_DOMAIN not in url_r:
                    return
                # Ignorar endpoints internos con falsos positivos
                if any(p in url_r for p in SKIP_ZP_PATHS):
                    return
                if response.status != 200:
                    return
                ct = response.headers.get("content-type", "")
                if not any(x in ct for x in ("json", "javascript", "text")):
                    return
                body = await response.text()
                if not body:
                    return
                is_leads = LEADS_ENDPOINT in url_r
                for m in AR_PHONE_RE.finditer(body):
                    cleaned = _re.sub(r'\D', '', m.group())
                    if cleaned in FAKE_PHONE_STRS:
                        continue
                    if len(cleaned) < 10 or len(cleaned) > 13:
                        continue
                    if not any(cleaned.startswith(p) for p in AR_VALID_PREFIXES):
                        continue
                    phone_from_api[0] = cleaned
                    src = "leads" if is_leads else "api"
                    print(f"    [tel] teléfono capturado ({src}): {cleaned} ({url_r[:70]})")
                    break
            except Exception:
                pass

        page.on("response", intercept_response)
        # ─────────────────────────────────────────────────────────────────────

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAV)
        except Exception as e:
            print(f"    [tel] error navegación: {type(e).__name__}")
            return None, None

        if not await wait_for_cf(page, timeout_secs=20):
            print(f"    [tel] Cloudflare bloqueó")
            await _screenshot(page)
            return None, None

        await page.wait_for_timeout(1500)

        # --- Intentar extraer nombre antes de clickear (aparece visible) ---
        nombre_val = None
        for sel in NOMBRE_SELS:
            try:
                el = await page.query_selector(sel)
                if el:
                    t = (await el.text_content() or "").strip()
                    if t:
                        nombre_val = t
                        break
            except Exception:
                pass

        # --- Buscar y clickear botón "Ver teléfono" ---
        phone_btn = None
        for sel in BTN_SELS:
            try:
                el = await page.query_selector(sel)
                if el:
                    phone_btn = el
                    break
            except Exception:
                pass

        if not phone_btn:
            # No hay botón — puede que el teléfono esté directamente visible
            pass
        else:
            # Registrar posición de todos los inputs ANTES del click
            # para poder identificar los del modal (los nuevos) después
            pre_click_positions: set[tuple] = set()
            try:
                all_pre = await page.query_selector_all(
                    'input:not([type="hidden"]):not([type="search"])'
                    ':not([type="checkbox"]):not([type="radio"])'
                )
                for el in all_pre:
                    try:
                        if await el.is_visible():
                            box = await el.bounding_box()
                            if box:
                                pre_click_positions.add(
                                    (round(box["x"]), round(box["y"]))
                                )
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                await phone_btn.click()
            except Exception:
                pass

            # Dar tiempo al modal para renderizarse completamente
            await page.wait_for_timeout(2500)

            # --- Detectar inputs del modal via JavaScript (evita problemas de
            #     pointer-events con el background overlay del modal) ---
            modal_input_count = await page.evaluate("""(prePositions) => {
                const modal = document.querySelector('#modal-mount');
                if (!modal) return 0;
                const inputs = modal.querySelectorAll(
                    'input:not([type="hidden"]):not([type="search"])' +
                    ':not([type="checkbox"]):not([type="radio"])'
                );
                return inputs.length;
            }""", list(pre_click_positions))

            print(f"    [tel] {modal_input_count} input(s) de modal detectados")

            if modal_input_count > 0:
                contact = next_fake_contact()

                # Llenar el formulario vía JavaScript (React-compatible):
                # usa el setter nativo del prototipo para que React detecte
                # el cambio y habilite el botón Enviar.
                filled_count = await page.evaluate("""(vals) => {
                    const modal = document.querySelector('#modal-mount');
                    if (!modal) return 0;
                    const inputs = Array.from(modal.querySelectorAll(
                        'input:not([type="hidden"]):not([type="search"])' +
                        ':not([type="checkbox"]):not([type="radio"])'
                    ));
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    let filled = 0;
                    inputs.slice(0, vals.length).forEach((inp, i) => {
                        setter.call(inp, vals[i]);
                        inp.dispatchEvent(new Event('input',  {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        filled++;
                    });
                    return filled;
                }""", [contact["nombre"], contact["email"], contact["telefono"]])

                print(f"    [tel] {filled_count} campo(s) llenados con datos de contacto")
                await page.wait_for_timeout(400)

                # Activar captura de API ANTES del submit
                capture_active[0] = True

                # Submit vía JavaScript (también evita el problema del overlay)
                submit_result = await page.evaluate("""() => {
                    const modal = document.querySelector('#modal-mount');
                    if (!modal) return 'no-modal';
                    const btn = modal.querySelector('button[type="submit"]') ||
                                Array.from(modal.querySelectorAll('button')).find(b =>
                                    /enviar|ver tel|continuar/i.test(b.textContent)
                                );
                    if (btn) { btn.click(); return 'clicked: ' + btn.textContent.trim().substring(0, 30); }
                    const form = modal.querySelector('form');
                    if (form) { form.requestSubmit(); return 'form-submit'; }
                    return 'not-found';
                }""")
                print(f"    [tel] submit: {submit_result}")

                # Esperar activamente a que ZonaProp procese el lead (hasta 6s)
                await page.wait_for_timeout(1500)
                for tick in range(8):
                    # 1. ¿Ya lo tenemos de la interceptación de API?
                    if phone_from_api[0]:
                        print(f"    [tel] teléfono API confirmado en tick {tick}")
                        break
                    # 2. ¿Apareció algún selector de teléfono en el DOM?
                    found_early = False
                    for sel in ['a[href^="tel:"]'] + PHONE_SELS:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                found_early = True
                                break
                        except Exception:
                            pass
                    if found_early:
                        print(f"    [tel] teléfono DOM detectado en tick {tick}")
                        break
                    await page.wait_for_timeout(500)
            else:
                await page.wait_for_timeout(1500)

        # Screenshot intermedio — captura el estado de la página en el momento
        # de intentar extraer el teléfono (útil para ver si el número apareció
        # con otro selector o si la página cambió de estado)
        if debug_path:
            inter_path = debug_path.replace(".png", "_pre_extract.png")
            try:
                from pathlib import Path as _Path
                _Path(inter_path).parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=inter_path, full_page=True)
            except Exception:
                pass

        # --- Extraer teléfono del publicante ---
        telefono_val = None

        # 0) Resultado de intercepción de red (endpoint rp-api/leads más confiable)
        if phone_from_api[0]:
            telefono_val = phone_from_api[0]

        # 1) Modal de éxito: ZonaProp muestra "Teléfonos disponibles: 54 9 XXXXXXXXXX"
        #    Extraemos el texto del modal y buscamos el número con regex.
        if not telefono_val:
            try:
                modal_text = await page.evaluate("""() => {
                    const modal = document.querySelector('#modal-mount');
                    return modal ? modal.innerText : '';
                }""")
                fake_phones = {c["telefono"] for c in FAKE_CONTACTS}
                for m in AR_PHONE_RE.finditer(modal_text or ""):
                    cleaned = _re.sub(r'\D', '', m.group())
                    if cleaned in fake_phones:
                        continue
                    if len(cleaned) < 10 or len(cleaned) > 13:
                        continue
                    telefono_val = cleaned
                    print(f"    [tel] teléfono extraído del modal DOM: {cleaned}")
                    break
            except Exception:
                pass

        # 2) Selectores CSS estándar
        if not telefono_val:
            for sel in PHONE_SELS:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        if sel.startswith("a[href"):
                            href = await el.get_attribute("href") or ""
                            val = href.replace("tel:", "").strip()
                        else:
                            val = (await el.text_content() or "").strip()
                        if val and any(c.isdigit() for c in val):
                            telefono_val = val
                            break
                except Exception:
                    pass

        # 3) Fallback: regex sobre el texto completo de la página
        if not telefono_val:
            try:
                body_text = await page.evaluate("() => document.body.innerText")
                fake_phones = {c["telefono"] for c in FAKE_CONTACTS}
                for m in AR_PHONE_RE.finditer(body_text or ""):
                    cleaned = _re.sub(r'\D', '', m.group())
                    if cleaned in fake_phones:
                        continue
                    if len(cleaned) < 10 or len(cleaned) > 13:
                        continue
                    telefono_val = cleaned
                    break
            except Exception:
                pass

        # Segundo intento nombre (después del click, puede haber aparecido)
        if not nombre_val:
            for sel in NOMBRE_SELS:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        t = (await el.text_content() or "").strip()
                        if t:
                            nombre_val = t
                            break
                except Exception:
                    pass

        if not telefono_val:
            print(f"    [!] No se pudo obtener teléfono — posible bloqueo ZonaProp")
            await _screenshot(page)

        return nombre_val, telefono_val

    except Exception as e:
        print(f"    [tel] excepción inesperada: {e}")
        return None, None
    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# Google Sheets — conexión y estado
# ---------------------------------------------------------------------------
def connect_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


def col_letter(idx: int) -> str:
    """Índice 0-based → letra(s) de columna."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def cargar_sheet_state(ws) -> dict:
    """
    Carga todo el Sheet en memoria una sola vez.
    Devuelve:
      ws         — el worksheet (para writes posteriores)
      headers    — lista de headers (fila 1)
      h_idx      — {header: col_index_0based}
      id_to_row  — {id_zonaprop: row_num_1based}
      existentes — {id_zonaprop: precio_actual}  (solo activas)
    """
    all_values = ws.get_all_values()
    if not all_values:
        return {"ws": ws, "headers": [], "h_idx": {}, "id_to_row": {}, "existentes": {}}

    headers = all_values[0]
    h_idx   = {h: i for i, h in enumerate(headers)}

    id_col     = h_idx.get("id_zonaprop", 0)
    precio_col = h_idx.get("precio_actual")
    activa_col = h_idx.get("activa")

    id_to_row  = {}
    existentes = {}

    for r_num, row in enumerate(all_values, 1):
        if r_num == 1 or not row or len(row) <= id_col:
            continue
        id_zp = row[id_col]
        if not id_zp:
            continue
        id_to_row[id_zp] = r_num

        activa = row[activa_col] if activa_col is not None and len(row) > activa_col else "1"
        if activa in ("", "1", "TRUE", "True", "true"):
            precio_str = row[precio_col] if precio_col is not None and len(row) > precio_col else ""
            try:
                existentes[id_zp] = int(precio_str) if precio_str else None
            except (ValueError, TypeError):
                existentes[id_zp] = None

    return {
        "ws":        ws,
        "headers":   headers,
        "h_idx":     h_idx,
        "id_to_row": id_to_row,
        "existentes": existentes,
        "all_values": all_values,   # reutilizado en resetear_es_nuevo_sheets
    }


# ---------------------------------------------------------------------------
# DB helpers (modo local)
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    existentes = {row[1] for row in conn.execute("PRAGMA table_info(propiedades)")}
    for nombre, tipo in COLUMNAS_REQUERIDAS:
        if nombre not in existentes:
            conn.execute(f"ALTER TABLE propiedades ADD COLUMN {nombre} {tipo}")
            print(f"  [DB] Columna agregada: {nombre}")
    conn.commit()


def get_existing_ids_db(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT id_zonaprop, precio_actual FROM propiedades WHERE activa = 1 OR activa IS NULL"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def resetear_es_nuevo_db(conn: sqlite3.Connection) -> int:
    cur = conn.execute("UPDATE propiedades SET es_nuevo = 0 WHERE es_nuevo = 1")
    conn.commit()
    return cur.rowcount


def insertar_en_db(insertados: list) -> None:
    conn = sqlite3.connect(DB_PATH)
    for row in insertados:
        try:
            conn.execute("""
                INSERT INTO propiedades (
                    id_zonaprop, url, fecha_primera_vez, fecha_actualizacion,
                    precio_actual, moneda, expensas,
                    m2_totales, m2_cubiertos, m2_descubiertos, m2_tasables,
                    precio_por_m2_total, precio_por_m2_tasable,
                    ambientes, dormitorios, banos, toilettes, cocheras,
                    antiguedad, disposicion, orientacion, luminosidad,
                    direccion, barrio, barrio_simple, orden_barrio,
                    descripcion, tipo_operacion, tipo_propiedad,
                    es_nuevo, activa, bajo_precio, precio_anterior,
                    nombre_publicante, telefono_publicante
                ) VALUES (
                    :id_zonaprop, :url, :fecha_primera_vez, :fecha_actualizacion,
                    :precio_actual, :moneda, :expensas,
                    :m2_totales, :m2_cubiertos, :m2_descubiertos, :m2_tasables,
                    :precio_por_m2_total, :precio_por_m2_tasable,
                    :ambientes, :dormitorios, :banos, :toilettes, :cocheras,
                    :antiguedad, :disposicion, :orientacion, :luminosidad,
                    :direccion, :barrio, :barrio_simple, :orden_barrio,
                    :descripcion, :tipo_operacion, :tipo_propiedad,
                    :es_nuevo, :activa, :bajo_precio, :precio_anterior,
                    :nombre_publicante, :telefono_publicante
                )
            """, row)
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()


def actualizar_precios_db(actualizados: list, hoy: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    for upd in actualizados:
        conn.execute("""
            UPDATE propiedades SET
                precio_actual       = ?,
                precio_anterior     = ?,
                bajo_precio         = ?,
                fecha_actualizacion = ?
            WHERE id_zonaprop = ?
        """, (upd["precio_nuevo"], upd["precio_anterior"],
              upd["bajo_precio"], hoy, upd["id"]))
    conn.commit()
    conn.close()



# ---------------------------------------------------------------------------
# Sheets helpers (modo GitHub Actions)
# ---------------------------------------------------------------------------
def resetear_es_nuevo_sheets(sh: dict) -> int:
    """
    Pone es_nuevo=FALSE en todas las filas del Sheet que lo tengan en 1/TRUE.
    Usa batch_update: una sola llamada a la API sin importar cuántas filas haya.
    Reutiliza el all_values ya cargado en sh para no hacer un segundo get.
    """
    ws         = sh["ws"]
    h_idx      = sh["h_idx"]
    all_values = sh.get("all_values", [])

    if "es_nuevo" not in h_idx:
        return 0

    col_i        = h_idx["es_nuevo"]
    es_nuevo_col = col_letter(col_i)

    updates = []
    for r_num, row in enumerate(all_values, 1):
        if r_num == 1:
            continue
        val = row[col_i] if len(row) > col_i else ""
        if val in ("1", "TRUE", "True", "true"):
            updates.append({"range": f"{es_nuevo_col}{r_num}", "values": [[0]]})

    if updates:
        ws.batch_update(updates)
    return len(updates)


def escribir_nuevos_en_sheets(sh: dict, insertados: list) -> None:
    if not insertados:
        return

    ws      = sh["ws"]
    headers = sh["headers"]

    # Defaults para columnas de usuario en filas nuevas.
    # telefono_publicante y nombre_publicante SÍ se incluyen si el scraper los
    # capturó (no son "datos del usuario" en una fila recién creada).
    DEFAULTS_USUARIO = {
        "estado":          "Sin llamar",
        "nota":            "",
        "proxima_llamada": "",
        "notas_llamada":   "",
    }

    nuevas_filas = []
    for row in insertados:
        fila = []
        for h in headers:
            if h in DEFAULTS_USUARIO:
                fila.append(DEFAULTS_USUARIO[h])
            else:
                val = row.get(h)
                if val is None:
                    fila.append("")
                elif isinstance(val, bool):
                    fila.append("TRUE" if val else "FALSE")
                else:
                    fila.append(val)
        nuevas_filas.append(fila)

    all_values       = ws.get_all_values()
    ultima_fila      = len(all_values)
    filas_actuales   = ws.row_count
    filas_necesarias = ultima_fila + len(nuevas_filas) + 100
    if filas_necesarias > filas_actuales:
        ws.add_rows(filas_necesarias - filas_actuales)
        print(f"  Sheet expandido a {filas_necesarias} filas")

    ws.append_rows(nuevas_filas, value_input_option="RAW")
    print(f"  ✓ {len(insertados)} filas nuevas escritas en Sheets")
    time.sleep(1)

    # Actualizar id_to_row en el estado local
    nueva_fila_num = ultima_fila + 1
    for row in insertados:
        sh["id_to_row"][row["id_zonaprop"]] = nueva_fila_num
        nueva_fila_num += 1


def actualizar_precios_sheets(sh: dict, actualizados: list) -> None:
    if not actualizados:
        return

    ws        = sh["ws"]
    h_idx     = sh["h_idx"]
    id_to_row = sh["id_to_row"]

    if "precio_actual" not in h_idx:
        return

    precio_col      = col_letter(h_idx["precio_actual"])
    precio_ant_col  = col_letter(h_idx["precio_anterior"]) if "precio_anterior" in h_idx else None
    bajo_precio_col = col_letter(h_idx["bajo_precio"])     if "bajo_precio"     in h_idx else None

    updates = []
    for upd in actualizados:
        row_num = id_to_row.get(upd["id"])
        if not row_num:
            continue
        updates.append({"range": f"{precio_col}{row_num}", "values": [[upd["precio_nuevo"]]]})
        if precio_ant_col:
            updates.append({"range": f"{precio_ant_col}{row_num}", "values": [[upd["precio_anterior"]]]})
        if bajo_precio_col:
            updates.append({"range": f"{bajo_precio_col}{row_num}", "values": [[upd["bajo_precio"]]]})

    if updates:
        ws.batch_update(updates)
        bajaron  = sum(1 for u in actualizados if u["bajo_precio"])
        subieron = len(actualizados) - bajaron
        print(f"  ✓ {len(actualizados)} precios actualizados ({bajaron} bajaron, {subieron} subieron)")
        time.sleep(1)



# ---------------------------------------------------------------------------
# Fase 2+3 — Recorrer listado con detalles inline
# ---------------------------------------------------------------------------
async def recorrer_listado(pw, existentes: dict, hoy: str, max_paginas=None) -> tuple[set, list, list]:
    """
    Recorre todas las páginas del listado. Después de escanear cada página,
    visita los detalles de las propiedades nuevas e inserta en DB ANTES de
    avanzar a la siguiente. Esto genera actividad orgánica entre navegaciones
    y evita el bloqueo de Cloudflare.

    Devuelve: ids_vistos, insertados, actualizados
    """
    ids_vistos          = set()
    nuevos_ids          = set()   # deduplicar a lo largo de todas las páginas
    nuevos              = []      # todas las cards nuevas acumuladas
    actualizados        = []
    ids_procesados      = set()   # nuevos ya detallados e insertados
    insertados          = []      # rows insertados en DB (acumulado total)
    paginas_sin_nuevas  = 0       # contador de páginas consecutivas sin nuevas

    list_browser, list_page = await new_stealth_page_diario(pw)
    try:
        # Primera página: goto directo
        await list_page.goto(BASE_URL, wait_until="domcontentloaded")
        await list_page.wait_for_timeout(random.randint(3000, 6000))
        if not await wait_for_cf(list_page):
            print("  [!] Cloudflare bloqueó la página inicial")
            return ids_vistos, insertados, actualizados, 0

        page_num = 0

        while True:
            page_num += 1

            cards = await extract_cards(list_page)
            if not cards:
                print(f"  Página {page_num}: sin tarjetas → fin del listado")
                break

            for card in cards:
                id_zp = card.get("id_zonaprop")
                if not id_zp:
                    continue

                ids_vistos.add(id_zp)

                if id_zp in existentes:
                    precio_viejo = existentes[id_zp]
                    precio_nuevo = card.get("precio_actual")
                    if precio_nuevo and precio_viejo and precio_nuevo != precio_viejo:
                        if not any(a["id"] == id_zp for a in actualizados):
                            actualizados.append({
                                "id":              id_zp,
                                "precio_nuevo":    precio_nuevo,
                                "precio_anterior": precio_viejo,
                                "bajo_precio":     1 if precio_nuevo < precio_viejo else 0,
                            })
                elif id_zp not in nuevos_ids:
                    nuevos_ids.add(id_zp)
                    nuevos.append(card)

            print(f"  Página {page_num}: {len(cards)} tarjetas | "
                  f"Nuevas acumuladas: {len(nuevos_ids)} | "
                  f"Cambios precio: {len(actualizados)} | "
                  f"Vistas total: {len(ids_vistos)}")

            # Procesar detalles de los nuevos de esta página antes de continuar
            nuevos_pagina = [c for c in nuevos if c.get("id_zonaprop") in nuevos_ids - ids_procesados]
            if nuevos_pagina:
                insertados_pagina = await procesar_nuevos(pw, nuevos_pagina, hoy)
                insertar_en_db(insertados_pagina)
                ids_procesados.update(c.get("id_zonaprop") for c in nuevos_pagina)
                insertados.extend(insertados_pagina)
                print(f"  → {len(insertados_pagina)} nuevas insertadas en DB")
                paginas_sin_nuevas = 0
            else:
                paginas_sin_nuevas += 1
                if paginas_sin_nuevas >= PAGINAS_SIN_NUEVAS_LIMITE:
                    print(f"  [!] {PAGINAS_SIN_NUEVAS_LIMITE} páginas consecutivas sin nuevas "
                          f"→ parada temprana en página {page_num}")
                    break

            if max_paginas and page_num >= max_paginas:
                print(f"  Límite de páginas alcanzado ({max_paginas})")
                break

            next_btn = await list_page.query_selector('[data-qa="PAGING_NEXT"]')
            if not next_btn:
                print(f"  Página {page_num}: sin botón 'siguiente' → fin")
                break

            await asyncio.sleep(random.uniform(2, 4))
            await next_btn.click()
            await list_page.wait_for_load_state("domcontentloaded")
            if not await wait_for_cf(list_page):
                print("  [!] Cloudflare bloqueó la página siguiente")
                break
            await list_page.wait_for_timeout(2000)

        print(f"\n  Listado recorrido: {page_num} páginas | "
              f"{len(ids_vistos)} IDs únicos | {len(insertados)} insertados")

    finally:
        await list_browser.close()

    return ids_vistos, insertados, actualizados, page_num


# ---------------------------------------------------------------------------
# Fase 3 — Procesar propiedades nuevas (solo construye rows, no escribe)
# ---------------------------------------------------------------------------
async def procesar_nuevos(pw, nuevos: list, hoy: str) -> list:
    """
    Visita el detalle de cada propiedad nueva y construye el row completo.
    No escribe a ninguna DB — devuelve la lista de rows.
    """
    insertados = []
    total      = len(nuevos)

    for i, card in enumerate(nuevos, 1):
        id_zp = card.get("id_zonaprop")
        url   = card.get("url")
        if not id_zp or not url:
            continue

        tipo_op, tipo_prop = parsear_slug(url)

        detail = {}
        try:
            await asyncio.sleep(random.uniform(3, 7))
            detail = await fetch_detail(pw, url)
        except Exception as e:
            print(f"  [{i:>4}/{total}] [error detalle] {id_zp}: {e}")

        nombre_publicante   = None
        telefono_publicante = None
        try:
            await asyncio.sleep(random.uniform(2, 4))
            nombre_publicante, telefono_publicante = await fetch_telefono(
                pw, url, headless=True  # launchd corre sin display
            )
        except Exception as e:
            print(f"  [{i:>4}/{total}] [error teléfono] {id_zp}: {e}")

        m2_totales   = detail.get("m2_totales")  or card.get("m2_totales")
        m2_cubiertos = detail.get("m2_cubiertos")
        precio       = card.get("precio_actual")
        m2_desc, m2_tas, pm2_tot, pm2_tas = calcular_columnas(m2_totales, m2_cubiertos, precio)
        barrio_simple, orden_barrio = resolver_barrio(card.get("barrio"))

        row = {
            "id_zonaprop":           id_zp,
            "url":                   url,
            "fecha_primera_vez":     hoy,
            "fecha_actualizacion":   hoy,
            "precio_actual":         precio,
            "moneda":                card.get("moneda"),
            "expensas":              card.get("expensas"),
            "m2_totales":            m2_totales,
            "m2_cubiertos":          m2_cubiertos,
            "m2_descubiertos":       m2_desc,
            "m2_tasables":           m2_tas,
            "precio_por_m2_total":   pm2_tot,
            "precio_por_m2_tasable": pm2_tas,
            "ambientes":             detail.get("ambientes")   or card.get("ambientes"),
            "dormitorios":           detail.get("dormitorios") or card.get("dormitorios"),
            "banos":                 detail.get("banos")       or card.get("banos"),
            "toilettes":             detail.get("toilettes"),
            "cocheras":              detail.get("cocheras")    or card.get("cocheras"),
            "antiguedad":            detail.get("antiguedad"),
            "disposicion":           detail.get("disposicion"),
            "orientacion":           detail.get("orientacion"),
            "luminosidad":           detail.get("luminosidad"),
            "direccion":             card.get("direccion"),
            "barrio":                card.get("barrio"),
            "barrio_simple":         barrio_simple,
            "orden_barrio":          orden_barrio,
            "descripcion":           card.get("descripcion"),
            "tipo_operacion":        tipo_op,
            "tipo_propiedad":        tipo_prop,
            "es_nuevo":              1,
            "activa":                1,
            "bajo_precio":           0,
            "precio_anterior":       None,
            "nombre_publicante":     nombre_publicante,
            "telefono_publicante":   telefono_publicante,
        }

        insertados.append(row)
        pm2_str = f"USD {pm2_tas:,.0f}/m²" if pm2_tas else "sin m²"
        tel_str = f" 📞{telefono_publicante}" if telefono_publicante else ""
        print(f"  [{i:>4}/{total}] ✓ {barrio_simple:<18} {pm2_str}  {url.split('/')[-1][:40]}{tel_str}")

    return insertados


# ---------------------------------------------------------------------------
# Fase 5 — Sincronizar Sheets (modo local: sync incremental al final)
# ---------------------------------------------------------------------------
def sincronizar_sheets(insertados: list, actualizados: list) -> None:
    print("  Conectando al Sheet...")
    ws = connect_sheets()
    sh = cargar_sheet_state(ws)

    if not sh["headers"]:
        print("  [!] Sheet vacío — omitiendo sync")
        return

    # Resetear es_nuevo ANTES de escribir las nuevas del día,
    # para que el Sheet refleje exactamente las propiedades de HOY.
    n_reseteados = resetear_es_nuevo_sheets(sh)
    if n_reseteados:
        print(f"  ✓ {n_reseteados} filas con es_nuevo reseteadas en Sheet")
    else:
        print("  es_nuevo: sin filas que resetear")

    if not insertados and not actualizados:
        print("  Nada más que sincronizar con Sheets")
        return

    headers   = sh["headers"]
    h_idx     = sh["h_idx"]
    id_to_row = sh["id_to_row"]

    # -----------------------------------------------------------------------
    # Nuevos: separar los que ya existen en el Sheet de los verdaderamente nuevos
    # Un ID puede estar en el Sheet pero no en SQLite si fue cargado por otro
    # medio (migrar_a_sheets.py). En ese caso solo actualizamos columnas seguras.
    # -----------------------------------------------------------------------
    if insertados:
        ya_en_sheet      = [r for r in insertados if     r["id_zonaprop"] in id_to_row]
        realmente_nuevos = [r for r in insertados if not r["id_zonaprop"] in id_to_row]

        # Filas que ya existen: actualizar solo columnas del scraper.
        # NUNCA tocar columnas de usuario (COLS_USUARIO): estado, nota, teléfono, etc.
        COLS_SCRAPER_UPDATE = ["precio_actual", "precio_anterior", "bajo_precio", "es_nuevo"]
        updates = []
        for row in ya_en_sheet:
            row_num = id_to_row[row["id_zonaprop"]]
            for col in COLS_SCRAPER_UPDATE:
                if col in h_idx and col not in COLS_USUARIO:
                    val = row.get(col, "")
                    if val is None: val = ""
                    updates.append({
                        "range":  f"{col_letter(h_idx[col])}{row_num}",
                        "values": [[val]],
                    })
        if updates:
            ws.batch_update(updates)
            print(f"  ✓ {len(ya_en_sheet)} filas ya existentes — actualizadas precio/es_nuevo "
                  f"(columnas de usuario intactas)")
            time.sleep(1)

        # Filas verdaderamente nuevas: append al final.
        # Para columnas de usuario se aplican defaults; nunca se usa row.get() en ellas.
        if realmente_nuevos:
            DEFAULTS_USUARIO = {
                "estado":          "Sin llamar",
                "nota":            "",
                "proxima_llamada": "",
                "notas_llamada":   "",
                # telefono_publicante y nombre_publicante SÍ se incluyen
                # desde el scraper si fueron capturados (fila recién creada).
            }
            nuevas_filas = []
            for row in realmente_nuevos:
                fila = []
                for h in headers:
                    if h in DEFAULTS_USUARIO:
                        fila.append(DEFAULTS_USUARIO[h])
                    else:
                        val = row.get(h)
                        if val is None:
                            fila.append("")
                        elif isinstance(val, bool):
                            fila.append("TRUE" if val else "FALSE")
                        else:
                            fila.append(val)
                nuevas_filas.append(fila)

            all_values       = ws.get_all_values()
            filas_necesarias = len(all_values) + len(nuevas_filas) + 100
            if filas_necesarias > ws.row_count:
                ws.add_rows(filas_necesarias - ws.row_count)
                print(f"  Sheet expandido a {filas_necesarias} filas")

            ws.append_rows(nuevas_filas, value_input_option="RAW")
            print(f"  ✓ {len(realmente_nuevos)} filas nuevas agregadas al Sheet")
            time.sleep(1)

        if not ya_en_sheet and not realmente_nuevos:
            print("  Sin nuevos para escribir")

    # -----------------------------------------------------------------------
    # Actualizados: solo precio_actual, precio_anterior, bajo_precio
    # (ya implementado correctamente en actualizar_precios_sheets)
    # -----------------------------------------------------------------------
    if actualizados:
        actualizar_precios_sheets(sh, actualizados)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(max_paginas=None, sin_sheets=False):
    t0  = time.time()
    hoy = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'=' * 65}")
    print(f"SCRAPER DIARIO — {hoy}")
    modo = "SHEETS (GitHub Actions)" if USAR_SHEETS_COMO_DB else "LOCAL (SQLite)"
    print(f"Modo: {modo}")
    print(f"{'=' * 65}")

    # -----------------------------------------------------------------------
    # Modo GitHub Actions — Google Sheets como fuente de verdad
    # -----------------------------------------------------------------------
    if USAR_SHEETS_COMO_DB:
        print("\nConectando a Google Sheets...")
        ws = connect_sheets()
        sh = cargar_sheet_state(ws)
        existentes = sh["existentes"]
        print(f"  {len(existentes)} propiedades activas cargadas desde Sheets")

        print("\n[1/5] Reseteando es_nuevo en Sheets...")
        reseteados = resetear_es_nuevo_sheets(sh)
        print(f"      {reseteados} registros reseteados")

        print("\n[2/5+3/5] Recorriendo listado y detallando nuevas inline...")
        async with async_playwright() as pw:
            ids_vistos, insertados, actualizados, paginas_recorridas = await recorrer_listado(
                pw, existentes, hoy, max_paginas
            )

        print(f"\n[3/5] Detalles procesados inline — {len(insertados)} nuevas procesadas")

        print(f"\n[4/5] Actualizando precios en Sheets...")
        if actualizados:
            actualizar_precios_sheets(sh, actualizados)
        else:
            print("      Sin cambios de precio")

        print(f"\n[5/5] Escribiendo nuevos en Sheets...")
        if insertados:
            escribir_nuevos_en_sheets(sh, insertados)
        else:
            print("      Sin nuevos para escribir")

        total_inicial = len(existentes)
        total_final   = total_inicial + len(insertados)

    # -----------------------------------------------------------------------
    # Modo local — SQLite como fuente de verdad
    # -----------------------------------------------------------------------
    else:
        conn = sqlite3.connect(DB_PATH)
        print("\n[DB] Verificando schema...")
        init_db(conn)
        total_inicial = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]
        print(f"     {total_inicial} propiedades en DB")

        print("\n[1/5] Reseteando es_nuevo...")
        reseteados = resetear_es_nuevo_db(conn)
        print(f"      {reseteados} registros reseteados")

        existentes = get_existing_ids_db(conn)
        conn.close()

        print("\n[2/5+3/5] Recorriendo listado y detallando nuevas inline...")
        async with async_playwright() as pw:
            ids_vistos, insertados, actualizados, paginas_recorridas = await recorrer_listado(
                pw, existentes, hoy, max_paginas
            )

        print(f"\n[3/5] Detalles procesados inline — {len(insertados)} nuevas insertadas en DB")

        print(f"\n[4/5] Actualizando precios...")
        if actualizados:
            actualizar_precios_db(actualizados, hoy)
            bajaron  = sum(1 for u in actualizados if u["bajo_precio"])
            subieron = len(actualizados) - bajaron
            print(f"      {len(actualizados)} precios actualizados ({bajaron} bajaron, {subieron} subieron)")
        else:
            print("      Sin cambios de precio")

        if not sin_sheets:
            print(f"\n[5/5] Sincronizando Google Sheets...")
            try:
                sincronizar_sheets(insertados, actualizados)
            except Exception as e:
                print(f"  [!] Error en sync Sheets: {e}")
        else:
            print("\n[5/5] Sheets omitido (--sin-sheets)")

        conn = sqlite3.connect(DB_PATH)
        total_final = conn.execute("SELECT COUNT(*) FROM propiedades").fetchone()[0]
        conn.close()

    # -----------------------------------------------------------------------
    # Resumen
    # -----------------------------------------------------------------------
    elapsed  = time.time() - t0
    mins     = int(elapsed // 60)
    segs     = int(elapsed % 60)
    bajaron  = sum(1 for u in actualizados if u["bajo_precio"])
    subieron = len(actualizados) - bajaron

    print(f"\n{'=' * 65}")
    print("RESUMEN")
    print(f"{'=' * 65}")
    print(f"  Propiedades (inicio → fin):  {total_inicial} → {total_final}")
    print(f"  Páginas recorridas:          {paginas_recorridas}")
    print(f"  IDs vistos hoy:              {len(ids_vistos)}")
    print(f"  Nuevos insertados:           {len(insertados)}")
    print(f"  Precios actualizados:        {len(actualizados)} ({bajaron} bajaron, {subieron} subieron)")
    print(f"  Tiempo total:                {mins}m {segs}s")
    print(f"{'=' * 65}\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Scraper diario de ZonaProp")
    parser.add_argument(
        "--max-paginas", type=int, default=None,
        help="Límite de páginas a recorrer (útil para pruebas)",
    )
    parser.add_argument(
        "--sin-sheets", action="store_true",
        help="Omitir sincronización con Google Sheets (solo modo local)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(
        max_paginas=args.max_paginas,
        sin_sheets=args.sin_sheets,
    ))
