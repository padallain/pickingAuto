import telebot
from telebot import types
import gspread
from openai import OpenAI
import sys
import base64
import os
import tempfile
import json
from dotenv import load_dotenv


load_dotenv()


def get_required_env(var_name):
    value = os.getenv(var_name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno requerida: {var_name}")
    return value


def get_google_client():
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credenciales.json")

    if credentials_json:
        return gspread.service_account_from_dict(json.loads(credentials_json))

    return gspread.service_account(filename=credentials_file)

print("[INFO] Iniciando bot de picking...")
try:
    openai_api_key = get_required_env("OPENAI_API_KEY")
    telegram_bot_token = get_required_env("TELEGRAM_BOT_TOKEN")
    google_sheet_name = os.getenv("GOOGLE_SHEET_NAME", "ISOLA")

    client = OpenAI(api_key=openai_api_key)
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




# --- Lista de códigos de chocolates ---
CODIGOS_CHOCOLATES = set([
    "0010","0011","0012","0018","0019","0020","0021","0022","0023","0024","0025","0026","0030","0031","0032","0033","0036","0042","0044","0050","0062","0063","0080","0081","0082","0084","0088","0102","0106",
    "57100113","57100114","57100260","57100263","57100427","57100428","57100429","57100430","57100452","57100474","57100522","57100523","57100594","57100610","57100737","57100864","57100865","57100940","57100956","57101210","57101407","57101411","57101412","BPE12F","DMA57101412"
])

@bot.message_handler(content_types=['photo'])
def procesar_evaluacion(message):
    # Solo usuarios autorizados pueden usar el bot
   
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
            "Extrae los siguientes datos de la imagen:\n"
            "- Fecha del documento (parte superior derecha o donde aparezca)\n"
            "- Pedido N° (parte superior derecha)\n"
            "- Número de cajas que aparece en la línea de Observaciones (si existe) o suma las cantidades de cajas que aparecen en la columna de cajas/bultos\n"
            "- Responsable (nombre en la sección 'PICKING')\n"
            "- Una tabla con los productos: código y cantidad de cada producto que aparece en la hoja.\n"
            "Responde primero los datos generales en formato tabla: Fecha, Pedido N°, Número de cajas, Responsable. Luego, debajo, una tabla con columnas: Código, Cantidad."
        )
        with open(temp_path, "rb") as image_file:
            img_base64 = base64.b64encode(image_file.read()).decode("utf-8")
            print(f"[DEBUG] Tamaño base64: {len(img_base64)}")
            print(f"[DEBUG] Inicio base64: {img_base64[:50]}")
            try:
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "Eres un experto en reconocimiento de texto en imágenes."},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}}
                        ]}
                    ],
                    max_tokens=600
                )
            except Exception as e:
                print(f"[DEBUG] Error al enviar a OpenAI: {e}")
                raise
        os.remove(temp_path)

        # 3. Procesar la respuesta de OpenAI
        datos = response.choices[0].message.content.strip()
        fila_valida = None
        productos = []
        parsing_productos = False
        for linea in datos.splitlines():
            linea = linea.strip()
            # Buscar tabla de datos generales
            if not parsing_productos:
                if "|" in linea and not linea.lower().startswith("| fecha") and "-" not in linea:
                    partes = [x.strip() for x in linea.split("|") if x.strip()]
                    if len(partes) == 4:
                        fila_valida = partes
                if linea.lower().startswith("| código"):
                    parsing_productos = True
                continue
            # Buscar tabla de productos
            if parsing_productos:
                if "|" in linea and not linea.lower().startswith("| código") and "-" not in linea:
                    partes = [x.strip() for x in linea.split("|") if x.strip()]
                    if len(partes) >= 2:
                        productos.append(partes)

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
            # --- Validar que el pedido no esté siendo usado por otro usuario ---
            fecha, pedido_num, num_cajas, responsable = fila_valida
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
            user_pedidos[user_id][pedido_num].append({
                "fecha": fecha,
                "pedido_num": pedido_num,
                "num_cajas": num_cajas,
                "responsable": responsable,
                "chocolates": total_chocolates,
                "usuario_telegram": usuario_telegram,
                "productos": productos,
                "chat_id": message.chat.id,
                "message_id": message.message_id
            })
            total_hojas = len(user_pedidos[user_id][pedido_num])
            cierre_msg = f"finalizar {pedido_num}"
            if total_hojas == 1:
                bot.reply_to(message, f"✅ Hoja 1 registrada para el pedido {pedido_num}.\nChocolates en esta hoja: {total_chocolates}.\nEnvía otra foto si hay más hojas, o para cerrar el pedido copia y pega este mensaje: \n\n{cierre_msg}")
            else:
                bot.reply_to(message, f"✅ Hoja {total_hojas} registrada para el pedido {pedido_num}.\nChocolates en esta hoja: {total_chocolates}.\nEnvía otra foto si hay más hojas, o para cerrar el pedido copia y pega este mensaje: \n\n{cierre_msg}")
            print(f"[INFO] Hoja {total_hojas} guardada temporalmente para pedido {pedido_num} del usuario {user_id}")
        else:
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
    # Solo usuarios autorizados pueden usar el bot
    if message.from_user.id not in USERS_WHITELIST:
        bot.reply_to(message, "⛔️ No tienes permiso para usar este bot. Contacta al administrador.")
        return
    try:
        user_id = message.from_user.id
        partes = message.text.strip().split()
        if len(partes) < 2:
            bot.reply_to(message, "Debes indicar el número de pedido: finalizar <pedido_num>")
            return
        pedido_num = partes[1]
        # Buscar si el pedido existe y a quién pertenece
        found = False
        for uid in user_pedidos:
            if pedido_num in user_pedidos[uid]:
                found = True
                owner_id = uid
                break
        if not found:
            bot.reply_to(message, f"No hay hojas registradas para el pedido {pedido_num}.")
            return
        if user_id != owner_id:
            bot.reply_to(message, "❌ Solo el administrador puede aprobar y cerrar este pedido.")
            return
        # Guardar en pendientes de aprobación
        pendientes_aprobacion[pedido_num] = {
            "hojas": user_pedidos[owner_id][pedido_num],
            "owner_id": owner_id
        }
        bot.reply_to(message, f"⏳ Pedido {pedido_num} enviado para aprobación del administrador. Será revisado antes de guardarse en Google Sheets.")
        # Notificar al admin con botón de aprobación y datos extraídos
        try:
            hojas = user_pedidos[owner_id][pedido_num]
            resumen = f"Pedido {pedido_num} pendiente de aprobación. Total hojas: {len(hojas)}.\nEnviado por usuario: {owner_id}\n\n"
            for idx, hoja_datos in enumerate(hojas, 1):
                resumen += f"--- Hoja {idx} ---\n"
                resumen += f"Fecha: {hoja_datos['fecha']}\nPedido N°: {hoja_datos['pedido_num']}\nCajas: {hoja_datos['num_cajas']}\nResponsable: {hoja_datos['responsable']}\nChocolates: {hoja_datos.get('chocolates', 0)}\nUsuario Telegram: {hoja_datos.get('usuario_telegram', '')}\n"
                if hoja_datos.get('productos'):
                    resumen += "Productos extraídos:\nCódigo | Cantidad\n"
                    for prod in hoja_datos['productos']:
                        resumen += f"{prod[0]} | {prod[1]}\n"
                resumen += "\n"
            markup = types.InlineKeyboardMarkup()
            btn = types.InlineKeyboardButton("Aprobar", callback_data=f"aprobar:{pedido_num}")
            markup.add(btn)
            bot.send_message(ADMIN_USER_ID, resumen, reply_markup=markup)
        except Exception as e:
            print(f"[ERROR] No se pudo notificar al admin: {e}")
        # Eliminar de user_pedidos
        del user_pedidos[owner_id][pedido_num]
    except Exception as e:
        print(f"[ERROR] {e}")
        bot.reply_to(message, f"❌ Error al enviar a aprobación: {e}")


# --- Handler para que solo el admin apruebe y guarde en Google Sheets ---

# --- Handler para aprobar pedido desde botón (callback) ---
@bot.callback_query_handler(func=lambda call: call.data.startswith('aprobar:'))
def aprobar_pedido_callback(call):
    # Solo usuarios autorizados pueden usar el bot
    
    try:
        user_id = call.from_user.id
        if user_id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "❌ Solo el administrador puede aprobar pedidos.", show_alert=True)
            return
        pedido_num = call.data.split(':', 1)[1]
        if pedido_num not in pendientes_aprobacion:
            bot.answer_callback_query(call.id, f"No hay pedido pendiente de aprobación con el número {pedido_num}.", show_alert=True)
            return
        hojas = pendientes_aprobacion[pedido_num]["hojas"]
        total_hojas = len(hojas)
        for idx, hoja_datos in enumerate(hojas, 1):
            fila = [
                hoja_datos["fecha"],
                hoja_datos["pedido_num"],
                str(idx),
                str(total_hojas),
                hoja_datos["num_cajas"],
                hoja_datos["responsable"],
                hoja_datos.get("chocolates", 0),
                hoja_datos.get("usuario_telegram", "")
            ]
            hoja.append_row(fila)
            # Intentar borrar la foto original del chat
            try:
                if hoja_datos.get("chat_id") and hoja_datos.get("message_id"):
                    bot.delete_message(hoja_datos["chat_id"], hoja_datos["message_id"])
            except Exception as e:
                print(f"[ERROR] No se pudo borrar la foto del chat: {e}")
        bot.edit_message_text(f"✅ Pedido {pedido_num} aprobado y guardado con {total_hojas} hoja(s) en Google Sheets. Las fotos originales han sido eliminadas del grupo.", call.message.chat.id, call.message.message_id)
        print(f"[INFO] Pedido {pedido_num} aprobado y guardado con {total_hojas} hoja(s) por el admin {user_id}")
        del pendientes_aprobacion[pedido_num]
    except Exception as e:
        print(f"[ERROR] {e}")
        bot.answer_callback_query(call.id, f"❌ Error al aprobar el pedido: {e}", show_alert=True)

bot.polling(timeout=10, long_polling_timeout=5)