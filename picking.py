import telebot
from telebot import types
import gspread
import anthropic
import sys
import base64
import os
import tempfile
import json
import re
from dotenv import load_dotenv


load_dotenv()


def get_required_env(var_name):
    value = os.getenv(var_name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno requerida: {var_name}")
    return value


class UserWhitelist:
    def __init__(self, user_ids):
        self.user_ids = user_ids

    def __contains__(self, user_id):
        return not self.user_ids or user_id in self.user_ids


def parse_user_ids(value):
    if not value:
        return set()

    user_ids = set()
    for raw_id in value.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            user_ids.add(int(raw_id))
        except ValueError:
            print(f"[WARN] Ignorando user id invalido en USERS_WHITELIST: {raw_id}")
    return user_ids


def normalize_label(value):
    return (
        value.lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("n°", "numero")
        .strip()
    )


def normalize_extracted_value(value, fallback=""):
    if value is None:
        return fallback
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "no visible", "no se ve", "no legible", "n/a", "na"}:
        return fallback
    return text


def parse_json_response(content):
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return None, None

    fecha = normalize_extracted_value(payload.get("fecha"))
    pedido_num = normalize_extracted_value(payload.get("pedido_num"))
    num_cajas = normalize_extracted_value(payload.get("num_cajas"))
    responsable = normalize_extracted_value(payload.get("responsable"), "SIN RESPONSABLE")

    productos = []
    for producto in payload.get("productos", []):
        if not isinstance(producto, dict):
            continue
        codigo = normalize_extracted_value(producto.get("codigo")).upper()
        cantidad = normalize_extracted_value(producto.get("cantidad"))
        if codigo and cantidad:
            productos.append([codigo, cantidad])

    fila_valida = [fecha, pedido_num, num_cajas, responsable]
    if not fecha or not pedido_num or not num_cajas:
        return None, productos

    return fila_valida, productos


def parse_openai_response(content):
    fila_json, productos_json = parse_json_response(content)
    if fila_json:
        return fila_json, productos_json

    fields = {
        "fecha": None,
        "pedido_num": None,
        "num_cajas": None,
        "responsable": "SIN RESPONSABLE",
    }
    productos = []
    parsing_productos = False

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        normalized_line = normalize_label(line)
        if "codigo" in normalized_line and "cantidad" in normalized_line:
            parsing_productos = True
            continue

        if "|" in line:
            parts = [part.strip() for part in line.split("|") if part.strip()]
            if not parts or all(set(part) <= {"-", ":"} for part in parts):
                continue

            if not parsing_productos and len(parts) == 4:
                normalized_parts = [normalize_label(part) for part in parts]
                if "fecha" not in normalized_parts and "responsable" not in normalized_parts:
                    fields["fecha"], fields["pedido_num"], fields["num_cajas"], fields["responsable"] = parts
                    continue

            if parsing_productos and len(parts) >= 2:
                productos.append(parts[:2])
                continue

        if not parsing_productos and (":" in line or "=" in line):
            separator = ":" if ":" in line else "="
            key, value = line.split(separator, 1)
            key = normalize_label(key)
            value = value.strip()
            if not value:
                continue

            if "fecha" in key:
                fields["fecha"] = value
            elif "pedido" in key:
                fields["pedido_num"] = value
            elif "cajas" in key:
                fields["num_cajas"] = value
            elif "responsable" in key:
                fields["responsable"] = value
            continue

        if parsing_productos:
            product_match = re.match(r"^([A-Za-z0-9]+)\s*[,;|]\s*(\d+)\s*$", line)
            if product_match:
                productos.append([product_match.group(1), product_match.group(2)])

    fila_valida = [
        fields["fecha"],
        fields["pedido_num"],
        fields["num_cajas"],
        normalize_extracted_value(fields["responsable"], "SIN RESPONSABLE"),
    ]
    if not fila_valida[0] or not fila_valida[1] or not fila_valida[2]:
        return None, productos

    return fila_valida, productos


def get_google_client():
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credenciales.json")

    if credentials_json:
        return gspread.service_account_from_dict(json.loads(credentials_json))

    return gspread.service_account(filename=credentials_file)

print("[INFO] Iniciando bot de picking...")
try:
    anthropic_api_key = get_required_env("ANTHROPIC_API_KEY")
    telegram_bot_token = get_required_env("TELEGRAM_BOT_TOKEN")
    google_sheet_name = os.getenv("GOOGLE_SHEET_NAME", "ISOLA")

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    bot = telebot.TeleBot(telegram_bot_token)
    gc = get_google_client()
    hoja = gc.open(google_sheet_name).sheet1

    # --- Asegurar encabezado con columna Chocolates ---

    # --- Validar y limpiar encabezado y filas antiguas ---
    columnas_requeridas = ["Fecha", "Pedido N°", "N° hoja", "Total hojas", "Número de cajas", "Responsable", "Chocolates", "Usuario Telegram"]
    encabezado = hoja.row_values(1)
    if encabezado != columnas_requeridas:
        # Eliminar todas las filas y dejar solo el encabezado correcto
        num_filas = len(hoja.get_all_values())
        if num_filas > 0:
            hoja.delete_rows(1, num_filas)
        hoja.insert_row(columnas_requeridas, 1)
        print("[INFO] Encabezado y filas antiguas limpiados en Google Sheets.")
    else:
        # Si el encabezado es correcto, eliminar filas que no tengan la cantidad correcta de columnas
        todas = hoja.get_all_values()
        filas_invalidas = [i+1 for i, fila in enumerate(todas[1:]) if len(fila) != len(columnas_requeridas)]
        for idx in reversed(filas_invalidas):
            hoja.delete_rows(idx+1)
        if filas_invalidas:
            print(f"[INFO] Filas antiguas inválidas eliminadas: {filas_invalidas}")

    print("[INFO] Conexiones exitosas. Esperando mensajes en Telegram...")
except Exception as e:
    print(f"[ERROR] Fallo en la inicialización: {e}")
    sys.exit(1)


# --- Manejo de múltiples hojas por pedido ---
user_pedidos = {}
# --- Pedidos pendientes de aprobación por el admin ---
pendientes_aprobacion = {}


# --- ID del administrador (solo este puede aprobar) ---
ADMIN_USER_ID = 275573212
USERS_WHITELIST = UserWhitelist(parse_user_ids(os.getenv("USERS_WHITELIST")))


def enviar_pedido_a_aprobacion(user_id, pedido_num):
    found = False
    owner_id = None
    for uid in user_pedidos:
        if pedido_num in user_pedidos[uid]:
            found = True
            owner_id = uid
            break

    if not found:
        return False, f"No hay hojas registradas para el pedido {pedido_num}."

    if user_id != owner_id:
        return False, "❌ Solo el usuario que inició el pedido puede enviarlo a aprobación."

    if user_id == ADMIN_USER_ID:
        hojas = user_pedidos[owner_id][pedido_num]
        try:
            guardar_pedido_en_sheets(pedido_num, hojas)
        except Exception as e:
            print(f"[ERROR] No se pudo guardar el pedido {pedido_num} en Google Sheets: {e}")
            return False, f"❌ Error al guardar en Google Sheets: {e}"

        del user_pedidos[owner_id][pedido_num]
        return True, f"✅ Pedido {pedido_num} guardado directamente en Google Sheets por el administrador."

    pendientes_aprobacion[pedido_num] = {
        "hojas": user_pedidos[owner_id][pedido_num],
        "owner_id": owner_id
    }

    try:
        hojas = user_pedidos[owner_id][pedido_num]
        for idx, hoja_datos in enumerate(hojas, 1):
            resumen = f"Pedido {pedido_num} pendiente de aprobación. Hoja {idx} de {len(hojas)}.\nEnviado por usuario: {owner_id}\n"
            resumen += f"Fecha: {hoja_datos['fecha']}\nPedido N°: {hoja_datos['pedido_num']}\nCajas: {hoja_datos['num_cajas']}\nResponsable: {hoja_datos['responsable']}\nChocolates: {hoja_datos.get('chocolates', 0)}\nUsuario Telegram: {hoja_datos.get('usuario_telegram', '')}\n"
            if hoja_datos.get('productos'):
                resumen += "Productos extraídos:\nCódigo | Cantidad\n"
                for prod in hoja_datos['productos']:
                    resumen += f"{prod[0]} | {prod[1]}\n"
            markup = types.InlineKeyboardMarkup()
            btn_aprobar = types.InlineKeyboardButton("Aprobar", callback_data=f"aprobar:{pedido_num}:{idx-1}")
            btn_editar = types.InlineKeyboardButton("Editar", callback_data=f"editar:{pedido_num}:{idx-1}")
            markup.add(btn_aprobar, btn_editar)
            bot.send_photo(ADMIN_USER_ID, hoja_datos["file_id"], caption=resumen, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] No se pudo notificar al admin: {e}")
        return False, f"❌ No se pudo notificar al administrador: {e}"

    del user_pedidos[owner_id][pedido_num]
    return True, f"⏳ Pedido {pedido_num} enviado para aprobación del administrador. Será revisado antes de guardarse en Google Sheets."


def guardar_pedido_en_sheets(pedido_num, hojas, start_index=1, total_hojas=None):
    if total_hojas is None:
        total_hojas = len(hojas)

    for offset, hoja_datos in enumerate(hojas):
        hoja_idx = start_index + offset
        fila = [
            hoja_datos["fecha"],
            hoja_datos["pedido_num"],
            str(hoja_idx),
            str(total_hojas),
            hoja_datos["num_cajas"],
            hoja_datos["responsable"],
            hoja_datos.get("chocolates", 0),
            hoja_datos.get("usuario_telegram", "")
        ]
        hoja.append_row(fila)

        try:
            if hoja_datos.get("chat_id") and hoja_datos.get("message_id"):
                bot.delete_message(hoja_datos["chat_id"], hoja_datos["message_id"])
        except Exception as e:
            print(f"[ERROR] No se pudo borrar la foto del chat: {e}")


def obtener_filas_pedido_en_sheets(pedido_num):
    filas = []
    todas = hoja.get_all_values()
    for row_number, row_values in enumerate(todas[1:], start=2):
        if len(row_values) > 1 and row_values[1] == str(pedido_num):
            filas.append(row_number)
    return filas


def guardar_hoja_admin_en_sheets(hoja_datos):
    pedido_num = hoja_datos["pedido_num"]
    filas_existentes = obtener_filas_pedido_en_sheets(pedido_num)
    numero_hoja = len(filas_existentes) + 1
    total_hojas = numero_hoja

    for row_number in filas_existentes:
        hoja.update_cell(row_number, 4, str(total_hojas))

    fila = [
        hoja_datos["fecha"],
        hoja_datos["pedido_num"],
        str(numero_hoja),
        str(total_hojas),
        hoja_datos["num_cajas"],
        hoja_datos["responsable"],
        hoja_datos.get("chocolates", 0),
        hoja_datos.get("usuario_telegram", "")
    ]
    hoja.append_row(fila)
    return numero_hoja



# --- Lista de códigos de chocolates ---
CODIGOS_CHOCOLATES = set([
    "0010","0011","0012","0018","0019","0020","0021","0022","0023","0024","0025","0026","0030","0031","0032","0033","0036","0042","0044","0050","0062","0063","0080","0081","0082","0084","0088","0102","0106",
    "57100113","57100114","57100260","57100263","57100427","57100428","57100429","57100430","57100452","57100474","57100522","57100523","57100594","57100610","57100737","57100864","57100865","57100940","57100956","57101210","57101407","57101411","57101412","BPE12F","DMA57101412"
])

@bot.message_handler(content_types=['photo'])
def procesar_evaluacion(message):
    try:
        user_id = message.from_user.id
        username = message.from_user.username or ""
        nombre = (message.from_user.first_name or "") + " " + (message.from_user.last_name or "")
        usuario_telegram = username if username else nombre.strip() if nombre.strip() else str(user_id)
        print("[INFO] Foto recibida. Procesando...")
        bot.reply_to(message, "📸 Foto recibida. Procesando con IA... espera un momento.")

        # 1. Descargar la foto
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp:
            temp.write(downloaded_file)
            temp_path = temp.name

        # 2. Enviar la imagen a OpenAI GPT-4o para extraer datos
        prompt = (
            "Analiza esta foto de una hoja de picking y responde SOLO con JSON valido, sin texto extra ni markdown.\n"
            "Busca estos campos exactos en el documento:\n"
            "- fecha: toma la fecha visible del documento.\n"
            "- pedido_num: toma el valor de 'Pedido N°' o 'Pedido No'.\n"
            "- num_cajas: toma el total de la columna 'Bultos/Cajas'. Si no hay total impreso, suma las cantidades visibles de esa columna.\n"
            "- responsable: toma solo el nombre escrito en la seccion 'PICKING' junto a 'Responsable'. Si el campo esta vacio o no se ve, devuelve 'SIN RESPONSABLE'.\n"
            "- productos: extrae cada fila de producto usando la columna 'Referencia' como codigo y la columna 'Bultos/Cajas' como cantidad.\n"
            "Ignora encabezados, observaciones, cliente, direccion y cualquier texto que no sea producto.\n"
            "No inventes datos. Si un valor no se ve claramente, usa cadena vacia, excepto 'responsable' que debe ser 'SIN RESPONSABLE'.\n"
            "Devuelve exactamente este esquema JSON:\n"
            "{\n"
            '  "fecha": "",\n'
            '  "pedido_num": "",\n'
            '  "num_cajas": "",\n'
            '  "responsable": "",\n'
            '  "productos": [\n'
            '    {"codigo": "", "cantidad": ""}\n'
            "  ]\n"
            "}"
        )
        with open(temp_path, "rb") as image_file:
            img_base64 = base64.b64encode(image_file.read()).decode("utf-8")
            print(f"[DEBUG] Tamaño base64: {len(img_base64)}")
            print(f"[DEBUG] Inicio base64: {img_base64[:50]}")
            try:
                response = client.messages.create(
                    model="claude-opus-4-8",
                    system="Eres un experto en reconocimiento de texto en imágenes.",
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": img_base64
                                }
                            },
                            {"type": "text", "text": prompt}
                        ]
                    }],
                    max_tokens=600
                )
            except Exception as e:
                print(f"[DEBUG] Error al enviar a Claude: {e}")
                raise
        os.remove(temp_path)

        # 3. Procesar la respuesta de Claude
        datos = next(b.text for b in response.content if b.type == "text").strip()
        fila_valida, productos = parse_openai_response(datos)

        # Contar chocolates
        total_chocolates = 0
        for prod in productos:
            codigo = prod[0].upper()
            try:
                cantidad = int(prod[1])
            except:
                cantidad = 0
            if codigo in CODIGOS_CHOCOLATES:
                total_chocolates += cantidad

        if fila_valida and all(fila_valida):
            fecha, pedido_num, num_cajas, responsable = fila_valida
            hoja_datos = {
                "fecha": fecha,
                "pedido_num": pedido_num,
                "num_cajas": num_cajas,
                "responsable": responsable,
                "chocolates": total_chocolates,
                "usuario_telegram": usuario_telegram,
                "productos": productos,
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "file_id": message.photo[-1].file_id
            }

            if user_id == ADMIN_USER_ID:
                numero_hoja = guardar_hoja_admin_en_sheets(hoja_datos)
                bot.reply_to(message, f"✅ Hoja {numero_hoja} del pedido {pedido_num} guardada directamente en Google Sheets.")
                print(f"[INFO] Hoja {numero_hoja} del pedido {pedido_num} guardada directamente por el administrador {user_id}")
                return

            # --- Validar que el pedido no esté siendo usado por otro usuario ---
            for uid in user_pedidos:
                if pedido_num in user_pedidos[uid]:
                    if uid == user_id:
                        bot.reply_to(message, f"❌ Ya has iniciado el registro del pedido {pedido_num}. Si necesitas agregar más hojas, envíalas. Si deseas cerrarlo, copia y pega: finalizar {pedido_num}")
                        print(f"[ERROR] El usuario ya inició el pedido {pedido_num}.")
                        return
                    else:
                        # Alerta visible en el grupo
                        nombre_alerta = usuario_telegram
                        bot.send_message(message.chat.id, f"⚠️ Atención: {nombre_alerta} intentó registrar el pedido {pedido_num}, pero ya está siendo registrado por otra persona. Solo el usuario original puede continuar ese pedido.")
                        print(f"[ERROR] Pedido {pedido_num} ya registrado por otro usuario.")
                        return
            if user_id not in user_pedidos:
                user_pedidos[user_id] = {}
            if pedido_num not in user_pedidos[user_id]:
                user_pedidos[user_id][pedido_num] = []
            user_pedidos[user_id][pedido_num].append(hoja_datos)
            total_hojas = len(user_pedidos[user_id][pedido_num])
            # Botón para finalizar pedido
            markup = types.InlineKeyboardMarkup()
            btn_finalizar = types.InlineKeyboardButton("Finalizar pedido", callback_data=f"finalizar:{pedido_num}")
            markup.add(btn_finalizar)
            if total_hojas == 1:
                bot.reply_to(message, f"✅ Hoja 1 registrada para el pedido {pedido_num}.\nChocolates en esta hoja: {total_chocolates}.\nEnvía otra foto si hay más hojas, o para cerrar el pedido pulsa el botón:", reply_markup=markup)
            else:
                bot.reply_to(message, f"✅ Hoja {total_hojas} registrada para el pedido {pedido_num}.\nChocolates en esta hoja: {total_chocolates}.\nEnvía otra foto si hay más hojas, o para cerrar el pedido pulsa el botón:", reply_markup=markup)
            print(f"[INFO] Hoja {total_hojas} guardada temporalmente para pedido {pedido_num} del usuario {user_id}")
        else:
            print(f"[DEBUG] Respuesta cruda de OpenAI sin parsear correctamente:\n{datos}")
            bot.reply_to(message, "❌ No se encontraron datos válidos para guardar.")
            print("[ERROR] No se encontraron datos válidos para guardar.")
    except Exception as e:
        print(f"[ERROR] {e}")
        bot.reply_to(message, f"❌ Error procesando la imagen: {e}")


# --- Handler para finalizar y guardar todas las hojas de un pedido ---
@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('finalizar'))

# --- Handler para finalizar y enviar a aprobación del admin ---
@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('finalizar'))
def finalizar_pedido(message):
    try:
        user_id = message.from_user.id
        partes = message.text.strip().split()
        if len(partes) < 2:
            bot.reply_to(message, "Debes indicar el número de pedido: finalizar <pedido_num>")
            return
        pedido_num = partes[1]
        ok, respuesta = enviar_pedido_a_aprobacion(user_id, pedido_num)
        bot.reply_to(message, respuesta)
    except Exception as e:
        print(f"[ERROR] {e}")
        bot.reply_to(message, f"❌ Error al enviar a aprobación: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('finalizar:'))
def finalizar_pedido_callback(call):
    try:
        pedido_num = call.data.split(':', 1)[1]
        ok, respuesta = enviar_pedido_a_aprobacion(call.from_user.id, pedido_num)
        bot.answer_callback_query(call.id, respuesta, show_alert=not ok)
        if ok:
            bot.send_message(call.message.chat.id, respuesta)
    except Exception as e:
        print(f"[ERROR] Finalizar callback: {e}")
        bot.answer_callback_query(call.id, f"❌ Error al finalizar el pedido: {e}", show_alert=True)


# --- Handler para que solo el admin apruebe y guarde en Google Sheets ---

# --- Handler para aprobar pedido desde botón (callback) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('aprobar:'))
def aprobar_pedido_callback(call):
    try:
        user_id = call.from_user.id
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Solo el administrador puede aprobar pedidos.", show_alert=True)
            return
        parts = call.data.split(':')
        pedido_num = parts[1]
        hoja_idx = int(parts[2]) if len(parts) > 2 else 0
        if pedido_num not in pendientes_aprobacion:
            bot.answer_callback_query(call.id, f"No hay pedido pendiente de aprobación con el número {pedido_num}.", show_alert=True)
            return
        hojas = pendientes_aprobacion[pedido_num]["hojas"]
        if hoja_idx >= len(hojas):
            bot.answer_callback_query(call.id, "Índice de hoja inválido.", show_alert=True)
            return
        hoja_datos = hojas[hoja_idx]
        guardar_pedido_en_sheets(pedido_num, [hoja_datos], start_index=hoja_idx + 1, total_hojas=len(hojas))
        bot.edit_message_caption(caption=f"✅ Hoja {hoja_idx+1} del pedido {pedido_num} aprobada y guardada en Google Sheets. La foto original ha sido eliminada.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        print(f"[INFO] Hoja {hoja_idx+1} del pedido {pedido_num} aprobada y guardada por el admin {user_id}")
        # Eliminar la hoja aprobada de la lista
        hojas.pop(hoja_idx)
        if not hojas:
            del pendientes_aprobacion[pedido_num]
    except Exception as e:
        print(f"[ERROR] {e}")
        bot.answer_callback_query(call.id, f"❌ Error al aprobar el pedido: {e}", show_alert=True)

# --- Handler para editar hoja (flujo interactivo) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('editar:'))
def editar_pedido_callback(call):
    try:
        user_id = call.from_user.id
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Solo el administrador puede editar pedidos.", show_alert=True)
            return
        parts = call.data.split(':')
        pedido_num = parts[1]
        hoja_idx = int(parts[2]) if len(parts) > 2 else 0
        if pedido_num not in pendientes_aprobacion:
            bot.answer_callback_query(call.id, f"No hay pedido pendiente con el número {pedido_num}.", show_alert=True)
            return
        hojas = pendientes_aprobacion[pedido_num]["hojas"]
        if hoja_idx >= len(hojas):
            bot.answer_callback_query(call.id, "Índice de hoja inválido.", show_alert=True)
            return
        hoja_datos = hojas[hoja_idx]
        # Guardar contexto de edición
        if "edicion" not in pendientes_aprobacion[pedido_num]:
            pendientes_aprobacion[pedido_num]["edicion"] = {}
        pendientes_aprobacion[pedido_num]["edicion"][user_id] = hoja_idx
        # Mostrar datos actuales y pedir edición
        texto = (
            f"Edita los datos de la hoja {hoja_idx+1} del pedido {pedido_num} enviando los campos en este formato (uno por línea):\n"
            f"fecha=\n{hoja_datos['fecha']}\n"
            f"pedido_num=\n{hoja_datos['pedido_num']}\n"
            f"num_cajas=\n{hoja_datos['num_cajas']}\n"
            f"responsable=\n{hoja_datos['responsable']}\n"
            f"chocolates=\n{hoja_datos.get('chocolates', 0)}\n"
            f"usuario_telegram=\n{hoja_datos.get('usuario_telegram', '')}\n"
            f"productos=\n" + '\n'.join([f"{p[0]},{p[1]}" for p in hoja_datos.get('productos', [])]) + "\n"
            "\nEjemplo de respuesta:\nfecha=2024-05-10\npedido_num=1234\nnum_cajas=10\nresponsable=Juan\nchocolates=5\nusuario_telegram=@usuario\nproductos=0010,2\nproductos=0020,3"
        )
        bot.send_message(user_id, texto)
        bot.answer_callback_query(call.id, "Envía los datos editados como mensaje.", show_alert=True)
    except Exception as e:
        print(f"[ERROR] Edición: {e}")
        bot.answer_callback_query(call.id, f"❌ Error en edición: {e}", show_alert=True)

# --- Handler para recibir datos editados del admin ---
@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_USER_ID and any(p in m.text for p in ["fecha=","pedido_num=","num_cajas=","responsable=","chocolates=","usuario_telegram=","productos="]))
def recibir_edicion_admin(message):
    try:
        user_id = message.from_user.id
        # Buscar pedido y hoja en edición
        for pedido_num, datos in pendientes_aprobacion.items():
            edicion = datos.get("edicion", {})
            if user_id in edicion:
                hoja_idx = edicion[user_id]
                hoja_datos = datos["hojas"][hoja_idx]
                # Parsear campos
                for line in message.text.splitlines():
                    if '=' not in line:
                        continue
                    key, val = line.split('=',1)
                    key = key.strip()
                    val = val.strip()
                    if key == "fecha":
                        hoja_datos["fecha"] = val
                    elif key == "pedido_num":
                        hoja_datos["pedido_num"] = val
                    elif key == "num_cajas":
                        hoja_datos["num_cajas"] = val
                    elif key == "responsable":
                        hoja_datos["responsable"] = val
                    elif key == "chocolates":
                        hoja_datos["chocolates"] = val
                    elif key == "usuario_telegram":
                        hoja_datos["usuario_telegram"] = val
                    elif key == "productos":
                        if 'productos_editados' not in hoja_datos:
                            hoja_datos['productos_editados'] = []
                        hoja_datos['productos_editados'].append(val)
                if 'productos_editados' in hoja_datos:
                    hoja_datos['productos'] = [p.split(',') for p in hoja_datos['productos_editados'] if ',' in p]
                    del hoja_datos['productos_editados']
                # Reenviar mensaje con botones de aprobar/editar y datos actualizados
                resumen = (
                    f"Hoja {hoja_idx+1} del pedido {pedido_num} EDITADA.\n"
                    f"Fecha: {hoja_datos['fecha']}\nPedido N°: {hoja_datos['pedido_num']}\nCajas: {hoja_datos['num_cajas']}\nResponsable: {hoja_datos['responsable']}\nChocolates: {hoja_datos.get('chocolates', 0)}\nUsuario Telegram: {hoja_datos.get('usuario_telegram', '')}\n"
                )
                if hoja_datos.get('productos'):
                    resumen += "Productos extraídos:\nCódigo | Cantidad\n"
                    for prod in hoja_datos['productos']:
                        resumen += f"{prod[0]} | {prod[1]}\n"
                markup = types.InlineKeyboardMarkup()
                btn_aprobar = types.InlineKeyboardButton("Aprobar", callback_data=f"aprobar:{pedido_num}:{hoja_idx}")
                btn_editar = types.InlineKeyboardButton("Editar", callback_data=f"editar:{pedido_num}:{hoja_idx}")
                markup.add(btn_aprobar, btn_editar)
                # Reenviar la foto si existe
                if 'file_id' in hoja_datos:
                    bot.send_photo(user_id, hoja_datos['file_id'], caption=resumen, reply_markup=markup)
                else:
                    bot.send_message(user_id, resumen, reply_markup=markup)
                bot.reply_to(message, f"✅ Hoja {hoja_idx+1} del pedido {pedido_num} actualizada. Puedes aprobarla ahora desde el nuevo mensaje.")
                del datos["edicion"][user_id]
                return
    except Exception as e:
        print(f"[ERROR] Recibir edición: {e}")
        bot.reply_to(message, f"❌ Error al actualizar: {e}")


bot.polling(timeout=10, long_polling_timeout=5)