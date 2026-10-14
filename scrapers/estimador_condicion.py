"""
estimador_condicion.py
======================
Evalúa el estado de propiedades inmobiliarias usando fotos + Claude Vision API.

Para cada propiedad en la DB que no tiene `estado_inmueble`:
  1. Navega a la URL de ZonaProp con Playwright (stealth + headless)
  2. Extrae las URLs de fotos de la galería (máximo MAX_FOTOS, default 5)
  3. Descarga las fotos en memoria y las convierte a base64
  4. Las envía a Claude claude-haiku-4-5 con un prompt de evaluación
  5. Parsea la respuesta JSON y extrae: score (1-10), nivel_refaccion, notas
  6. Actualiza las columnas estado_inmueble, nivel_refaccion y notas_condicion en la DB

Costo estimado: ~$0.006 por propiedad
  (5 fotos × 1600 tokens imagen × $0.0008/1K tokens input ≈ $0.006)

Variables de entorno requeridas:
  ANTHROPIC_API_KEY  — API key de Anthropic

Uso:
  python scrapers/estimador_condicion.py                   # procesa todos sin evaluar
  python scrapers/estimador_condicion.py --id 59010183     # procesa uno específico
  python scrapers/estimador_condicion.py --max 10          # procesa máximo N propiedades
  python scrapers/estimador_condicion.py --url https://... # procesa una URL directamente
  python scrapers/estimador_condicion.py --rerun           # re-evalúa los que ya tienen score
"""

import argparse
import asyncio
import base64
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
DB_PATH = "data/zonaprop.db"
MAX_FOTOS = 5          # máximo de fotos por propiedad a enviar a Claude
MIN_IMG_SIZE = 10_240  # ignorar imágenes menores a 10 KB
CLAUDE_MODEL = "claude-haiku-4-5"
SLEEP_MIN = 3.0        # segundos mínimos entre propiedades
SLEEP_MAX = 8.0        # segundos máximos entre propiedades

stealth = Stealth()

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

NIVELES_VALIDOS = {"ninguna", "leve", "media", "importante", "total"}

PROMPT_EVALUACION = """Sos un experto en evaluación de inmuebles en Argentina.
Analizá estas fotos de una propiedad en venta y evaluá su estado.

Respondé ÚNICAMENTE en este formato JSON (sin texto adicional):
{
  "score": <número entero del 1 al 10>,
  "nivel_refaccion": "<ninguna|leve|media|importante|total>",
  "notas": "<descripción breve en español de máximo 150 caracteres>"
}

Escala de score:
10 - Inmueble a estrenar o en perfectas condiciones
8-9 - Muy buen estado, sin necesidad de refacción
6-7 - Buen estado general, pequeños detalles a mejorar
4-5 - Estado regular, necesita refacción media (baños, cocina, pintura)
2-3 - Mal estado, necesita refacción importante (instalaciones, pisos, etc.)
1 - Muy mal estado, necesita refacción total o casi total

Nivel de refacción:
- ninguna: score 8-10
- leve: score 6-7 (pintura, limpieza, detalles)
- media: score 4-5 (baño, cocina, pintura completa)
- importante: score 2-3 (instalaciones, pisos, aberturas)
- total: score 1 (demolición parcial y reconstrucción)"""


# ---------------------------------------------------------------------------
# Verificación de dependencias y entorno
# ---------------------------------------------------------------------------
def verificar_entorno():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ERROR] La variable de entorno ANTHROPIC_API_KEY no está seteada.")
        print("        Exportala antes de correr el script:")
        print("        export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("[ERROR] El módulo 'anthropic' no está instalado.")
        print("        Instalalo con: pip install anthropic")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Agregar columnas nuevas si la DB ya existía sin ellas
    cols = {row[1] for row in conn.execute("PRAGMA table_info(propiedades)")}

    migraciones = [
        ("estado_inmueble",  "ALTER TABLE propiedades ADD COLUMN estado_inmueble INTEGER"),
        ("notas_condicion",  "ALTER TABLE propiedades ADD COLUMN notas_condicion TEXT"),
        ("nivel_refaccion",  "ALTER TABLE propiedades ADD COLUMN nivel_refaccion TEXT"),
    ]
    for col, sql in migraciones:
        if col not in cols:
            conn.execute(sql)
            print(f"[DB] Columna '{col}' agregada.")

    conn.commit()
    return conn


def get_propiedades_a_evaluar(
    conn: sqlite3.Connection,
    id_especifico: str | None = None,
    url_directa: str | None = None,
    max_props: int | None = None,
    rerun: bool = False,
) -> list[dict]:
    if url_directa:
        # Buscar por URL o insertar una fila temporal
        row = conn.execute(
            "SELECT id_zonaprop, url, barrio FROM propiedades WHERE url = ?",
            (url_directa,),
        ).fetchone()
        if row:
            return [dict(row)]
        # URL no está en DB: devolver un dict sintético
        return [{"id_zonaprop": "EXTERNO", "url": url_directa, "barrio": None}]

    if id_especifico:
        row = conn.execute(
            "SELECT id_zonaprop, url, barrio FROM propiedades WHERE id_zonaprop = ?",
            (id_especifico,),
        ).fetchone()
        return [dict(row)] if row else []

    if rerun:
        query = "SELECT id_zonaprop, url, barrio FROM propiedades WHERE url IS NOT NULL"
    else:
        query = (
            "SELECT id_zonaprop, url, barrio FROM propiedades "
            "WHERE url IS NOT NULL AND estado_inmueble IS NULL"
        )

    if max_props:
        query += f" LIMIT {int(max_props)}"

    rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


def guardar_evaluacion(
    conn: sqlite3.Connection,
    id_zonaprop: str,
    score: int,
    nivel: str,
    notas: str,
):
    conn.execute(
        """UPDATE propiedades
           SET estado_inmueble = ?,
               nivel_refaccion = ?,
               notas_condicion = ?
           WHERE id_zonaprop = ?""",
        (score, nivel, notas, id_zonaprop),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Playwright: esperar Cloudflare
# ---------------------------------------------------------------------------
async def wait_for_cf(page, timeout_secs: int = 60) -> bool:
    for _ in range(timeout_secs):
        title = await page.title()
        if "moment" not in title.lower() and "cloudflare" not in title.lower():
            return True
        await page.wait_for_timeout(1000)
    return False


async def new_stealth_page(pw):
    browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)
    context = await browser.new_context(**CONTEXT_OPTS)
    page = await context.new_page()
    await stealth.apply_stealth_async(page)
    return browser, page


# ---------------------------------------------------------------------------
# Extracción de URLs de fotos desde ZonaProp
# ---------------------------------------------------------------------------
def _es_foto_valida(url: str) -> bool:
    """Filtra URLs que correspondan a fotos reales de propiedades."""
    if not url:
        return False
    dominios_ok = ("zonaprop", "mlstatic", "mlistaticos", "meli.com")
    if not any(d in url for d in dominios_ok):
        return False
    # Ignorar íconos, logos, placeholders
    ignorar = ("icon", "logo", "placeholder", "sprite", "blank", "loading", "1x1")
    url_lower = url.lower()
    if any(p in url_lower for p in ignorar):
        return False
    # Debe parecer una imagen
    if not re.search(r"\.(jpe?g|png|webp)", url_lower):
        # A veces las URLs no tienen extensión pero son imágenes; permitir si tiene buen dominio
        if "zonaprop" not in url_lower and "mlstatic" not in url_lower:
            return False
    return True


async def extraer_urls_fotos(page) -> list[str]:
    """Extrae URLs de fotos del carousel de ZonaProp. Devuelve hasta MAX_FOTOS URLs."""
    urls: list[str] = []
    seen: set[str] = set()

    # Selectores candidatos (de más específico a más general)
    selectores = [
        "div[class*='gallery'] img",
        "div[class*='Gallery'] img",
        "div[class*='carousel'] img",
        "div[class*='slider'] img",
        "section[class*='gallery'] img",
        "img[class*='gallery']",
        "img[data-src*='zonaprop']",
        "img[data-src*='mlstatic']",
        "img[src*='zonaprop']",
        "img[src*='mlstatic']",
    ]

    for selector in selectores:
        elementos = await page.query_selector_all(selector)
        for el in elementos:
            # Intentar data-src primero (lazy loading), luego src
            for attr in ("data-src", "src", "data-lazy", "data-original"):
                url = await el.get_attribute(attr)
                if url and url not in seen and _es_foto_valida(url):
                    seen.add(url)
                    urls.append(url)
                    if len(urls) >= MAX_FOTOS:
                        return urls
        if urls:
            break  # si encontramos con este selector, no seguir buscando

    # Fallback: buscar en el HTML usando regex
    if not urls:
        html = await page.content()
        candidatas = re.findall(
            r'https://[^"\'>\s]+(?:zonaprop|mlstatic|mlistaticos)[^"\'>\s]*'
            r'\.(?:jpe?g|png|webp)[^"\'>\s]*',
            html,
            flags=re.IGNORECASE,
        )
        for url in candidatas:
            if url not in seen and _es_foto_valida(url):
                seen.add(url)
                urls.append(url)
                if len(urls) >= MAX_FOTOS:
                    break

    return urls


async def obtener_fotos_propiedad(pw, url: str) -> list[str]:
    """Navega a la URL de la propiedad y devuelve lista de URLs de fotos."""
    browser, page = await new_stealth_page(pw)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        passed = await wait_for_cf(page)
        if not passed:
            return []
        await page.wait_for_timeout(2000)
        return await extraer_urls_fotos(page)
    except Exception as e:
        print(f"    [!] Error navegando {url}: {e}")
        return []
    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# Descarga de imágenes en memoria
# ---------------------------------------------------------------------------
async def descargar_fotos_base64(urls: list[str]) -> list[dict]:
    """Descarga fotos en memoria y las convierte a base64. Filtra por tamaño mínimo."""
    resultados = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                contenido = resp.content
                if len(contenido) < MIN_IMG_SIZE:
                    continue
                # Detectar media type
                ct = resp.headers.get("content-type", "image/jpeg")
                if "png" in ct:
                    media_type = "image/png"
                elif "webp" in ct:
                    media_type = "image/webp"
                else:
                    media_type = "image/jpeg"
                b64 = base64.standard_b64encode(contenido).decode("utf-8")
                resultados.append({"media_type": media_type, "data": b64})
            except Exception as e:
                print(f"    [!] Error descargando foto {url}: {e}")
    return resultados


# ---------------------------------------------------------------------------
# Claude Vision API
# ---------------------------------------------------------------------------
def evaluar_con_claude(fotos_b64: list[dict]) -> dict | None:
    """
    Envía las fotos a Claude y retorna dict con score, nivel_refaccion, notas.
    Retorna None si falla.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Construir content con todas las imágenes
    content = []
    for foto in fotos_b64:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": foto["media_type"],
                "data": foto["data"],
            },
        })
    content.append({"type": "text", "text": PROMPT_EVALUACION})

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": content}],
        )
        texto = response.content[0].text.strip()
        return parsear_respuesta_claude(texto)
    except Exception as e:
        print(f"    [!] Error en Claude API: {e}")
        return None


def parsear_respuesta_claude(texto: str) -> dict | None:
    """Parsea el JSON de la respuesta de Claude. Intenta con regex si falla json.loads."""
    # Intento 1: json puro
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass

    # Intento 2: extraer bloque JSON del texto
    match = re.search(r"\{.*?\}", texto, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Intento 3: regex campo a campo
    score_m = re.search(r'"score"\s*:\s*(\d+)', texto)
    nivel_m = re.search(
        r'"nivel_refaccion"\s*:\s*"(ninguna|leve|media|importante|total)"', texto
    )
    notas_m = re.search(r'"notas"\s*:\s*"([^"]{1,200})"', texto)

    if score_m:
        return {
            "score": int(score_m.group(1)),
            "nivel_refaccion": nivel_m.group(1) if nivel_m else "media",
            "notas": notas_m.group(1) if notas_m else "",
        }

    print(f"    [!] No se pudo parsear respuesta de Claude: {texto[:200]}")
    return None


def validar_evaluacion(datos: dict) -> dict | None:
    """Valida y normaliza los campos del resultado de Claude."""
    if not datos:
        return None

    score = datos.get("score")
    nivel = datos.get("nivel_refaccion", "")
    notas = datos.get("notas", "")

    # Validar score
    try:
        score = int(score)
    except (TypeError, ValueError):
        return None
    if not (1 <= score <= 10):
        score = max(1, min(10, score))

    # Validar nivel
    if nivel not in NIVELES_VALIDOS:
        # Inferir desde score
        if score >= 8:
            nivel = "ninguna"
        elif score >= 6:
            nivel = "leve"
        elif score >= 4:
            nivel = "media"
        elif score >= 2:
            nivel = "importante"
        else:
            nivel = "total"

    # Truncar notas
    notas = str(notas)[:150]

    return {"score": score, "nivel_refaccion": nivel, "notas": notas}


# ---------------------------------------------------------------------------
# Procesamiento de una propiedad
# ---------------------------------------------------------------------------
async def procesar_propiedad(pw, prop: dict) -> dict:
    """
    Procesa una propiedad: obtiene fotos, llama a Claude y retorna resultado.

    Retorna dict con claves:
      status: 'ok' | 'sin_fotos' | 'error'
      score, nivel_refaccion, notas  (solo si status=='ok')
    """
    id_zp = prop["id_zonaprop"]
    url = prop["url"]

    # 1. Obtener URLs de fotos
    foto_urls = await obtener_fotos_propiedad(pw, url)
    if not foto_urls:
        return {"id": id_zp, "status": "sin_fotos"}

    # 2. Descargar fotos en memoria
    fotos_b64 = await descargar_fotos_base64(foto_urls)
    if not fotos_b64:
        return {"id": id_zp, "status": "sin_fotos"}

    # 3. Llamar a Claude
    resultado_raw = evaluar_con_claude(fotos_b64)
    if not resultado_raw:
        return {"id": id_zp, "status": "error"}

    # 4. Validar
    resultado = validar_evaluacion(resultado_raw)
    if not resultado:
        return {"id": id_zp, "status": "error"}

    return {
        "id": id_zp,
        "status": "ok",
        "score": resultado["score"],
        "nivel_refaccion": resultado["nivel_refaccion"],
        "notas": resultado["notas"],
        "n_fotos": len(fotos_b64),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(args):
    verificar_entorno()

    conn = init_db(DB_PATH)

    propiedades = get_propiedades_a_evaluar(
        conn,
        id_especifico=args.id,
        url_directa=args.url,
        max_props=args.max,
        rerun=args.rerun,
    )

    if not propiedades:
        print("No hay propiedades para evaluar.")
        conn.close()
        return

    total = len(propiedades)
    print(f"Propiedades a evaluar: {total}\n")

    contadores = {"ok": 0, "sin_fotos": 0, "error": 0, "scores": []}

    async with async_playwright() as pw:
        for idx, prop in enumerate(propiedades, start=1):
            id_zp = prop["id_zonaprop"]
            barrio = (prop.get("barrio") or "?").strip()[:20]

            resultado = await procesar_propiedad(pw, prop)

            if resultado["status"] == "ok":
                score = resultado["score"]
                nivel = resultado["nivel_refaccion"]
                notas = resultado["notas"]

                # Guardar en DB (salvo que sea fila sintética por --url sin DB)
                if id_zp != "EXTERNO":
                    guardar_evaluacion(conn, id_zp, score, nivel, notas)

                contadores["ok"] += 1
                contadores["scores"].append(score)
                print(
                    f"[{idx:3d}/{total}] ✓ {id_zp} | {barrio} | "
                    f"score: {score} | {nivel} | \"{notas}\""
                )

            elif resultado["status"] == "sin_fotos":
                contadores["sin_fotos"] += 1
                print(f"[{idx:3d}/{total}] ✗ {id_zp} | {barrio} | sin fotos — skip")

            else:
                contadores["error"] += 1
                print(f"[{idx:3d}/{total}] ! {id_zp} | {barrio} | error")

            # Pausa entre propiedades (salvo la última)
            if idx < total:
                await asyncio.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

    conn.close()

    # Resumen final
    scores = contadores["scores"]
    promedio = sum(scores) / len(scores) if scores else 0

    print(f"\n{'=' * 60}")
    print("RESUMEN")
    print(f"  Evaluados:   {contadores['ok']}")
    print(f"  Sin fotos:   {contadores['sin_fotos']}")
    print(f"  Errores:     {contadores['error']}")
    if scores:
        print(f"  Score prom:  {promedio:.1f}")
    print(f"  DB: {DB_PATH}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Estima el estado de propiedades usando fotos + Claude Vision API"
    )
    parser.add_argument(
        "--id",
        type=str,
        default=None,
        metavar="ID_ZONAPROP",
        help="Procesar solo la propiedad con este ID",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        metavar="N",
        help="Procesar máximo N propiedades",
    )
    parser.add_argument(
        "--url",
        type=str,
        default=None,
        metavar="URL",
        help="Procesar una URL directamente (no tiene que estar en la DB)",
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        default=False,
        help="Re-evaluar propiedades que ya tienen score (sobreescribe)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
