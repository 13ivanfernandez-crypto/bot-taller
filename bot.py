"""
Bot de Telegram para taller de motos (versión 100 % gratuita)
--------------------------------------------------------------
- Consejos por código de avería de cualquier marca.
- Busca esquemas eléctricos en internet y devuelve los enlaces.
- Analiza fotos (cuadro, conectores, mazo...).
- Solo responde a los usuarios autorizados del taller.

Gratis porque usa:
- Telegram Bot API (gratis).
- Google Gemini, plan gratuito (con límite de consultas diarias).
- Búsqueda web con la librería "ddgs" (gratis, sin clave).
- Se ejecuta en cualquier ordenador con Python (no necesita servidor ni dirección pública).
"""

import base64
import hashlib
import html
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturoTimeout
from collections import defaultdict, deque

import requests
from dotenv import load_dotenv

try:
    from ddgs import DDGS
except ImportError:  # nombre antiguo de la librería
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

_CARPETA = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_CARPETA, "claves.txt"), encoding="utf-8-sig")
load_dotenv(os.path.join(_CARPETA, ".env"))

# ─── Configuración ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Modelo principal (mejor, pero pocas consultas gratis al día) y de reserva (más consultas)
MODELO_PRINCIPAL = os.environ.get("MODELO_PRINCIPAL", "gemini-3.8-flash")
MODELO_RESERVA = os.environ.get("MODELO_RESERVA", "gemini-3.5-flash-lite")
TALLER_NOMBRE = os.environ.get("TALLER_NOMBRE", "el taller")
# IDs de Telegram autorizados, separados por comas. Cada uno lo consigue enviando /miid al bot.
USUARIOS_AUTORIZADOS = {
    u.strip() for u in os.environ.get("USUARIOS_AUTORIZADOS", "").split(",") if u.strip()
}

MAX_HISTORIAL = 10              # mensajes recordados por chat (pregunta + respuesta = 2)
HISTORIAL_CADUCA = 2 * 3600     # se olvida la conversación tras 2 h sin actividad
TG_MAX = 4000                   # Telegram admite 4096 caracteres por mensaje

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_FILES = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot-taller")

# ─── Instrucciones para la IA ──────────────────────────────────────────────────
SYSTEM_PROMPT = f"""Eres el asistente técnico de {TALLER_NOMBRE}, un taller de motos. \
Hablas con mecánicos profesionales por Telegram, en español, de forma directa y práctica.

Trabajas con TODAS las marcas: japonesas, europeas, scooters y marcas chinas recientes.

CÓDIGOS DE AVERÍA
- Si falta marca, modelo o año, pídelos en una sola línea antes de diagnosticar: el mismo \
código significa cosas distintas según el fabricante.
- Estructura: 1) Qué significa. 2) Causas probables, de más a menos frecuente. \
3) Comprobaciones paso a paso con valores orientativos y dónde medir. 4) Cómo borrarlo \
y reaprendizajes si los hay.
- Nunca inventes valores, colores de cable ni números de pin. Si no lo sabes con \
seguridad y los resultados de búsqueda no lo confirman, dilo y recomienda el manual de taller.

ESQUEMAS ELÉCTRICOS
- Te paso resultados de búsqueda web junto a la pregunta. Elige los enlaces más útiles \
(manuales de taller, foros técnicos, webs de recambios con diagramas) y di para qué \
modelo y años es cada uno. Avisa si no es el modelo o año exacto.
- SOLO puedes dar enlaces que aparezcan en los resultados de búsqueda. Nunca inventes \
direcciones web. Si no hay nada útil, dilo y sugiere qué buscar.

FOTOS
- Si mandan una foto, analízala e identifica lo que puedas (testigos, códigos, conectores).

FORMATO
- Respuestas cortas y fáciles de leer en el móvil. Usa **negrita** para lo importante y \
listas con "•". Nada de tablas.
"""

AYUDA = (
    "🔧 <b>Asistente del taller</b>\n\n"
    "Puedes pedirme:\n"
    "• <b>Códigos de avería</b>: <i>Yamaha MT-07 2019, código 15</i>\n"
    "• <b>Esquemas eléctricos</b>: <i>Esquema del encendido Honda CBR600RR 2008</i>\n"
    "• <b>Fotos</b>: manda una foto del cuadro o del conector y pregúntame\n\n"
    "/parar → cancelar la consulta que está pensando\n"
    "/nuevo → empezar una conversación desde cero\n"
    "/ayuda → ver este mensaje"
)

PALABRAS_BUSQUEDA = (
    "esquema", "diagrama", "cableado", "cable", "pinout", "pines", "conector", "manual",
    "código", "codigo", "error", "avería", "averia", "fallo", "dtc", "testigo", "centralita",
    "ecu", "sensor", "relé", "rele", "regulador", "estator", "bobina", "fusible",
)

# ─── Estado en memoria ─────────────────────────────────────────────────────────
historiales: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORIAL))
ultima_actividad: dict[int, float] = {}
consulta_actual: dict[int, int] = defaultdict(int)   # nº de consulta vigente por chat
_lock_consultas = threading.Lock()


def nueva_consulta(chat_id: int) -> int:
    """Marca una consulta nueva; cualquier consulta anterior de ese chat queda anulada."""
    with _lock_consultas:
        consulta_actual[chat_id] += 1
        return consulta_actual[chat_id]


def sigue_vigente(chat_id: int, numero: int) -> bool:
    return consulta_actual[chat_id] == numero


# ─── Telegram ──────────────────────────────────────────────────────────────────
def markdown_a_html(texto: str) -> str:
    """Convierte el Markdown de la IA al HTML que entiende Telegram."""
    t = html.escape(texto, quote=False)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    t = re.sub(r"^\s*[\*\-]\s+", "• ", t, flags=re.M)
    return t.strip()


def trocear(texto: str, limite: int = TG_MAX) -> list[str]:
    partes = []
    while len(texto) > limite:
        corte = texto.rfind("\n", 0, limite)
        if corte < limite // 2:
            corte = texto.rfind(" ", 0, limite)
        if corte <= 0:
            corte = limite
        partes.append(texto[:corte].strip())
        texto = texto[corte:].strip()
    if texto:
        partes.append(texto)
    return partes


def enviar(chat_id: int, texto: str, ya_es_html: bool = False) -> None:
    for trozo in trocear(texto):
        cuerpo = trozo if ya_es_html else markdown_a_html(trozo)
        r = requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": chat_id, "text": cuerpo, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=30)
        if not r.ok:  # Si el formato falla, se manda como texto plano
            requests.post(f"{TG_API}/sendMessage",
                          json={"chat_id": chat_id, "text": trozo}, timeout=30)


def escribiendo(chat_id: int) -> None:
    try:
        requests.post(f"{TG_API}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"}, timeout=10)
    except requests.RequestException:
        pass


def descargar_foto(file_id: str) -> bytes:
    info = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    ruta = info["result"]["file_path"]
    r = requests.get(f"{TG_FILES}/{ruta}", timeout=60)
    r.raise_for_status()
    return r.content


# ─── Búsqueda web (gratis) ─────────────────────────────────────────────────────
_buscador = ThreadPoolExecutor(max_workers=4)


def necesita_busqueda(texto: str) -> bool:
    t = texto.lower()
    return any(p in t for p in PALABRAS_BUSQUEDA) or bool(re.search(r"\d", t))


def buscar_en_internet(texto: str, contexto_previo: str = "") -> str:
    if DDGS is None:
        return ""
    consulta = texto if len(texto.split()) >= 5 else f"{contexto_previo} {texto}".strip()
    consulta = consulta[:200]
    t = consulta.lower()
    if any(p in t for p in ("esquema", "diagrama", "cableado", "pinout", "pines")):
        consultas = [f"{consulta} wiring diagram", f"{consulta} esquema eléctrico pdf"]
    else:
        consultas = [f"{consulta} motorcycle fault code", f"{consulta} moto código avería"]

    vistos, lineas = set(), []
    for q in consultas:
        try:
            futuro = _buscador.submit(lambda q=q: DDGS(timeout=10).text(q, region="es-es", max_results=6))
            resultados = futuro.result(timeout=20) or []
        except FuturoTimeout:
            log.warning("La búsqueda tardó demasiado (%s), sigo sin ella.", q)
            continue
        except Exception as e:
            log.warning("Búsqueda fallida (%s): %s", q, e)
            continue
        for r in resultados:
            url = r.get("href", "")
            if url and url not in vistos:
                vistos.add(url)
                lineas.append(f"- {r.get('title', '')}\n  {url}\n  {r.get('body', '')[:250]}")
    return "\n".join(lineas[:10])


# ─── Gemini ────────────────────────────────────────────────────────────────────
class LimiteAgotado(Exception):
    pass


class ClaveGoogleMal(Exception):
    pass


def llamar_gemini(modelo: str, contenidos: list) -> str:
    r = requests.post(
        GEMINI_URL.format(modelo=modelo),
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contenidos,
            "generationConfig": {"maxOutputTokens": 2500, "temperature": 0.3},
        },
        timeout=90,
    )
    if r.status_code in (401, 403):
        log.error("Google rechaza la clave (%s): %s", r.status_code, r.text[:300])
        raise ClaveGoogleMal(r.text[:300])
    if r.status_code == 429:
        raise LimiteAgotado(modelo)
    if r.status_code == 404:  # modelo no disponible para esta clave
        raise LimiteAgotado(modelo)
    if not r.ok:
        log.error("Error de Google (%s) con %s: %s", r.status_code, modelo, r.text[:300])
        r.raise_for_status()
    datos = r.json()
    partes = (datos.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in partes if not p.get("thought")).strip()


def preguntar_ia(chat_id: int, texto: str, foto: bytes | None = None, numero: int = 0) -> str:
    if time.time() - ultima_actividad.get(chat_id, 0) > HISTORIAL_CADUCA:
        historiales[chat_id].clear()
    ultima_actividad[chat_id] = time.time()
    historial = historiales[chat_id]

    previo = next((m["parts"][0]["text"] for m in reversed(historial) if m["role"] == "user"), "")
    prompt = texto
    if necesita_busqueda(texto) or (previo and len(texto.split()) < 5):
        resultados = buscar_en_internet(texto, previo)
        if resultados:
            prompt += f"\n\n[Resultados de búsqueda web]\n{resultados}"
        else:
            prompt += "\n\n[No hay resultados de búsqueda web disponibles: no des enlaces.]"

    partes = [{"text": prompt}]
    if foto:
        partes.insert(0, {"inline_data": {"mime_type": "image/jpeg",
                                          "data": base64.b64encode(foto).decode()}})
    contenidos = list(historial) + [{"role": "user", "parts": partes}]

    respuesta = ""
    for modelo in (MODELO_PRINCIPAL, MODELO_RESERVA):
        try:
            respuesta = llamar_gemini(modelo, contenidos)
            break
        except LimiteAgotado:
            log.warning("Límite gratuito agotado o modelo no disponible: %s", modelo)
    else:
        return ("⏳ Se han agotado las consultas gratuitas de hoy. "
                "Se renuevan cada día (sobre las 9:00, hora de España).")

    respuesta = respuesta or "No he podido generar una respuesta. Prueba a reformular la pregunta."
    if numero and not sigue_vigente(chat_id, numero):
        return respuesta  # consulta cancelada: no se guarda
    # Guardar en el historial solo el texto (sin foto ni resultados, para ahorrar)
    historial.append({"role": "user", "parts": [{"text": ("[Foto] " if foto else "") + texto}]})
    historial.append({"role": "model", "parts": [{"text": respuesta}]})
    return respuesta


# ─── Procesar mensajes ─────────────────────────────────────────────────────────
def procesar(mensaje: dict) -> None:
    chat_id = mensaje["chat"]["id"]
    usuario = str(mensaje.get("from", {}).get("id", ""))
    texto = (mensaje.get("text") or mensaje.get("caption") or "").strip()
    comando = texto.split("@")[0].lower() if texto.startswith("/") else ""
    log.info("Mensaje de %s: %s", usuario, (texto or "[foto]")[:80])

    if comando == "/miid":
        enviar(chat_id, f"Tu ID de Telegram es: <code>{usuario}</code>", ya_es_html=True)
        return
    if USUARIOS_AUTORIZADOS and usuario not in USUARIOS_AUTORIZADOS:
        log.warning("Usuario no autorizado: %s", usuario)
        enviar(chat_id, "🔒 Este bot es privado del taller. Pide acceso y manda tu /miid al encargado.",
               ya_es_html=True)
        return
    if comando in ("/start", "/ayuda", "/help"):
        enviar(chat_id, AYUDA, ya_es_html=True)
        return
    if comando in ("/parar", "/stop", "/cancelar"):
        nueva_consulta(chat_id)
        enviar(chat_id, "⏹️ Consulta cancelada. Pregúntame lo siguiente.", ya_es_html=True)
        return
    if comando == "/nuevo":
        nueva_consulta(chat_id)
        historiales[chat_id].clear()
        enviar(chat_id, "🆕 Listo, empezamos de cero. ¿Qué moto y qué avería?", ya_es_html=True)
        return

    foto = None
    if mensaje.get("photo"):
        escribiendo(chat_id)
        foto = descargar_foto(mensaje["photo"][-1]["file_id"])  # la de mayor resolución
        texto = texto or "Analiza esta foto y dime qué ves relevante para el diagnóstico."
    elif not texto:
        enviar(chat_id, "De momento entiendo texto y fotos 📷. Escribe /ayuda.", ya_es_html=True)
        return

    numero = nueva_consulta(chat_id)
    enviar(chat_id, "🔎 Consultando... (escribe /parar para cancelar)", ya_es_html=True)
    escribiendo(chat_id)
    inicio = time.time()
    try:
        respuesta = preguntar_ia(chat_id, texto, foto, numero)
    except ClaveGoogleMal:
        respuesta = ("⚠️ Google ha rechazado la clave de la IA. Revisa la clave GEMINI_API_KEY "
                     "(en Render: Environment).")
    except requests.Timeout:
        respuesta = "⏱️ La IA ha tardado demasiado en contestar. Prueba otra vez."
    if not sigue_vigente(chat_id, numero):
        log.info("Consulta de %s cancelada; respuesta descartada.", usuario)
        return
    enviar(chat_id, respuesta)
    log.info("Respondido a %s en %.0f s", usuario, time.time() - inicio)


def atender(mensaje: dict) -> None:
    chat_id = mensaje["chat"]["id"]
    try:
        procesar(mensaje)
    except Exception:
        log.exception("Error procesando mensaje")
        enviar(chat_id, "⚠️ Algo ha fallado. Inténtalo de nuevo.", ya_es_html=True)


def evitar_pausa_consola() -> None:
    """En Windows, hacer clic en la ventana negra la congela. Esto lo desactiva."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)
        modo = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(modo)):
            k32.SetConsoleMode(h, (modo.value & ~0x0040) | 0x0080)  # quita QUICK_EDIT
    except Exception:
        pass


def comprobar_token() -> bool:
    try:
        r = requests.get(f"{TG_API}/getMe", timeout=20)
    except requests.RequestException as e:
        log.warning("No hay conexión con Telegram: %s", e)
        return True
    if r.ok:
        log.info("Conectado como @%s", r.json()["result"].get("username"))
        return True
    return False


def main() -> None:
    evitar_pausa_consola()
    if not comprobar_token():
        print("\n  ** El token de Telegram no es correcto. **\n"
              "  Cierra esta ventana, borra el archivo claves.txt y vuelve a abrir iniciar.bat.\n")
        time.sleep(3600)
        return
    try:
        info = requests.get(f"{TG_API}/getWebhookInfo", timeout=20).json().get("result", {})
    except Exception:
        info = {}
    if info.get("url"):
        print("\n  ** Este bot ya funciona en internet (Render). **\n"
              "  No hace falta tener esta ventana abierta: puedes cerrarla.\n")
        time.sleep(3600)
        return
    log.info("Bot del taller arrancado. Esperando mensajes... (Ctrl+C para parar)")
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates",
                             params={"timeout": 50, "offset": offset}, timeout=60)
            if r.status_code in (401, 404):
                print("\n  ** El token de Telegram no es correcto. **\n"
                      "  Cierra esta ventana, borra el archivo claves.txt y vuelve a abrir iniciar.bat.\n"
                      "  Pega SOLO el codigo (tipo 123456789:AAH...), no el mensaje entero de BotFather.\n")
                time.sleep(3600)
                continue
            r.raise_for_status()
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                mensaje = update.get("message")
                if not mensaje:
                    continue
                threading.Thread(target=atender, args=(mensaje,), daemon=True).start()
        except KeyboardInterrupt:
            log.info("Bot detenido.")
            break
        except Exception as e:
            log.warning("Sin conexión con Telegram (%s). Reintentando en 5 s...", e)
            time.sleep(5)


# ─── Modo internet (Render): Telegram nos envía los mensajes ────────────────────
URL_PUBLICA = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
SECRETO = hashlib.sha256(TELEGRAM_TOKEN.encode()).hexdigest()[:40]
RUTA_WEBHOOK = f"/telegram/{SECRETO[:16]}"
MANTENER_DESPIERTO = os.environ.get("MANTENER_DESPIERTO", "si").lower() in ("si", "sí", "1", "true")

try:
    from flask import Flask, abort, request as peticion
except ImportError:
    Flask = None

app = None
if Flask is not None:
    app = Flask(__name__)

    @app.get("/")
    def salud():
        return "Bot del taller funcionando ✅"

    @app.post(RUTA_WEBHOOK)
    def recibir():
        if peticion.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRETO:
            abort(403)
        update = peticion.get_json(silent=True) or {}
        mensaje = update.get("message")
        if mensaje:
            threading.Thread(target=atender, args=(mensaje,), daemon=True).start()
        return "OK"


def configurar_webhook() -> None:
    if not URL_PUBLICA:
        return
    try:
        r = requests.post(f"{TG_API}/setWebhook", json={
            "url": URL_PUBLICA + RUTA_WEBHOOK,
            "secret_token": SECRETO,
            "allowed_updates": ["message"],
        }, timeout=20)
        log.info("Webhook de Telegram: %s", r.json().get("description", r.text[:200]))
    except Exception as e:
        log.error("No he podido configurar Telegram: %s", e)


def mantener_despierto() -> None:
    """Render gratis se duerme a los 15 min sin visitas: nos visitamos cada 10 min."""
    while True:
        time.sleep(600)
        try:
            requests.get(URL_PUBLICA + "/", timeout=30)
        except requests.RequestException:
            pass


if URL_PUBLICA and app is not None:
    configurar_webhook()
    if MANTENER_DESPIERTO:
        threading.Thread(target=mantener_despierto, daemon=True).start()
    log.info("Bot del taller en internet: %s", URL_PUBLICA)


if __name__ == "__main__":
    main()
