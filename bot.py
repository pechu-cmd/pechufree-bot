import logging
import os
import json
import base64
import httpx
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SHEET_URL = os.environ.get("GOOGLE_SHEET_URL")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")  # JSON completo como string
AUTHORIZED_USERS = os.environ.get("AUTHORIZED_USERS", "").split(",")  # IDs separados por coma

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── GOOGLE SHEETS ────────────────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(GOOGLE_SHEET_URL)
    return sh

def get_fijos():
    sh = get_sheet()
    ws = sh.worksheet("💰 FIJOS")
    rows = ws.get_all_values()
    fijos = []
    for i, row in enumerate(rows[4:], start=5):  # desde fila 5
        if len(row) >= 6 and row[2] and row[3]:
            try:
                monto = float(str(row[3]).replace(",","").replace("S/","").strip())
                fijos.append({
                    "row": i, "dia": row[1], "concepto": row[2],
                    "monto": monto, "beneficiario": row[4],
                    "estado": row[5], "fecha_pago": row[6] if len(row)>6 else "",
                    "monto_pagado": row[7] if len(row)>7 else "",
                    "notas": row[8] if len(row)>8 else ""
                })
            except: pass
    return fijos

def get_variables():
    sh = get_sheet()
    ws = sh.worksheet("📦 VARIABLES")
    rows = ws.get_all_values()
    variables = []
    for i, row in enumerate(rows[4:], start=5):
        if len(row) >= 5 and row[3] and row[4]:
            try:
                monto = float(str(row[4]).replace(",","").replace("S/","").strip())
                variables.append({
                    "row": i, "vence": row[1], "categoria": row[2],
                    "concepto": row[3], "monto": monto, "ref": row[5] if len(row)>5 else "",
                    "estado": row[6] if len(row)>6 else "PENDIENTE",
                    "fecha_pago": row[7] if len(row)>7 else "",
                    "monto_pagado": row[8] if len(row)>8 else "",
                    "notas": row[9] if len(row)>9 else ""
                })
            except: pass
    return variables

def marcar_pagado_fijo(row_num, fecha_pago, monto_pagado):
    sh = get_sheet()
    ws = sh.worksheet("💰 FIJOS")
    ws.update_cell(row_num, 6, "PAGADO")
    ws.update_cell(row_num, 7, fecha_pago)
    ws.update_cell(row_num, 8, str(monto_pagado))

def marcar_pagado_variable(row_num, fecha_pago, monto_pagado):
    sh = get_sheet()
    ws = sh.worksheet("📦 VARIABLES")
    ws.update_cell(row_num, 7, "PAGADO")
    ws.update_cell(row_num, 8, fecha_pago)
    ws.update_cell(row_num, 9, str(monto_pagado))

def agregar_variable(vence, categoria, concepto, monto, ref="", notas=""):
    sh = get_sheet()
    ws = sh.worksheet("📦 VARIABLES")
    ws.append_row([vence, categoria, concepto, monto, ref, "PENDIENTE", "", "", notas])

# ── CLAUDE VISION ────────────────────────────────────────────────────────────
async def analizar_captura(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={
               "model": "claude-sonnet-4-6",
                "max_tokens": 500,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text", "text": """Analizá esta captura de comprobante de pago bancario o transferencia.
Extraé SOLO estos datos en formato JSON, sin texto adicional:
{
  "monto": número (solo el monto principal pagado, sin simbolos),
  "beneficiario": "nombre del destinatario o empresa",
  "fecha": "DD/MM/YYYY",
  "concepto": "descripción del pago si aparece",
  "moneda": "PEN o USD"
}
Si no podés leer algún dato, ponelo como null."""}
                    ]
                }]
            }
        )
        data = resp.json()
        text = data["content"][0]["text"]
        try:
            clean = text.strip().replace("```json","").replace("```","").strip()
            return json.loads(clean)
        except:
            return {"monto": None, "beneficiario": None, "fecha": None, "concepto": None, "moneda": "PEN"}

# ── HELPERS ──────────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    if not AUTHORIZED_USERS or AUTHORIZED_USERS == [""]:
        return True  # Sin restricción si no se configuró
    return str(user_id) in AUTHORIZED_USERS

def format_sol(monto):
    return f"S/ {monto:,.2f}"

def get_pendientes_hoy():
    hoy = datetime.now()
    dia_hoy = hoy.day
    fijos = [f for f in get_fijos() if f["estado"] == "PENDIENTE" and str(f["dia"]).strip().isdigit() and int(f["dia"]) == dia_hoy]
    variables = [v for v in get_variables() if v["estado"] == "PENDIENTE" and v["vence"]]
    # Filtrar variables que vencen hoy
    vars_hoy = []
    for v in variables:
        try:
            fv = datetime.strptime(v["vence"], "%d/%m/%Y")
            if fv.date() == hoy.date():
                vars_hoy.append(v)
        except: pass
    return fijos, vars_hoy

def get_pendientes_semana():
    hoy = datetime.now()
    fin_semana = hoy + timedelta(days=7)
    dia_hoy = hoy.day
    fijos = [f for f in get_fijos() if f["estado"] == "PENDIENTE" and str(f["dia"]).strip().isdigit() and dia_hoy <= int(f["dia"]) <= (hoy + timedelta(days=7)).day]
    variables = []
    for v in get_variables():
        if v["estado"] == "PENDIENTE" and v["vence"]:
            try:
                fv = datetime.strptime(v["vence"], "%d/%m/%Y")
                if hoy.date() <= fv.date() <= fin_semana.date():
                    variables.append(v)
            except: pass
    return fijos, variables

# ── COMANDOS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("❌ No tenés acceso a este bot.")
        return
    texto = (
        "👋 *Hola\\! Soy el bot de egresos de Pechufree\\.*\n\n"
        "📸 *Mandame una captura de pago* y lo registro automáticamente\\.\n\n"
        "*Comandos disponibles:*\n"
        "/hoy — Pagos pendientes de hoy\n"
        "/semana — Pagos de esta semana\n"
        "/mes — Resumen completo del mes\n"
        "/pendientes — Solo los sin pagar\n"
        "/agregar — Agregar egreso nuevo"
    )
    await update.message.reply_text(texto, parse_mode="MarkdownV2")

async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    fijos, variables = get_pendientes_hoy()
    hoy_str = datetime.now().strftime("%d/%m/%Y")

    if not fijos and not variables:
        await update.message.reply_text(f"✅ No hay pagos pendientes para hoy ({hoy_str})")
        return

    texto = f"🗓 *Pagos pendientes — hoy {hoy_str}*\n\n"
    total = 0
    if fijos:
        texto += "💰 *FIJOS:*\n"
        for f in fijos:
            texto += f"• {f['concepto']} — {format_sol(f['monto'])}\n"
            total += f['monto']
    if variables:
        texto += "\n📦 *VARIABLES:*\n"
        for v in variables:
            texto += f"• {v['concepto']} — {format_sol(v['monto'])}\n"
            total += v['monto']
    texto += f"\n💳 *Total hoy: {format_sol(total)}*"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    fijos, variables = get_pendientes_semana()

    if not fijos and not variables:
        await update.message.reply_text("✅ No hay pagos pendientes esta semana")
        return

    texto = "📅 *Pagos pendientes — próximos 7 días*\n\n"
    total = 0
    if fijos:
        texto += "💰 *FIJOS:*\n"
        for f in fijos:
            texto += f"• Día {f['dia']} — {f['concepto']}: {format_sol(f['monto'])}\n"
            total += f['monto']
    if variables:
        texto += "\n📦 *VARIABLES:*\n"
        for v in variables:
            texto += f"• {v['vence']} — {v['concepto']}: {format_sol(v['monto'])}\n"
            total += v['monto']
    texto += f"\n💳 *Total semana: {format_sol(total)}*"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    fijos = get_fijos()
    variables = get_variables()

    total_fijos = sum(f['monto'] for f in fijos)
    total_vars = sum(v['monto'] for v in variables)
    pagado_fijos = sum(f['monto'] for f in fijos if f['estado']=='PAGADO')
    pagado_vars = sum(v['monto'] for v in variables if v['estado']=='PAGADO')
    pendiente = (total_fijos + total_vars) - (pagado_fijos + pagado_vars)

    texto = (
        f"📊 *Resumen del mes*\n\n"
        f"💰 Total fijos: {format_sol(total_fijos)}\n"
        f"📦 Total variables: {format_sol(total_vars)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧾 *Total mes: {format_sol(total_fijos + total_vars)}*\n\n"
        f"✅ Pagado: {format_sol(pagado_fijos + pagado_vars)}\n"
        f"⏳ Pendiente: {format_sol(pendiente)}\n"
        f"📈 Ejecutado: {((pagado_fijos+pagado_vars)/(total_fijos+total_vars)*100):.1f}%"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    fijos = [f for f in get_fijos() if f['estado']=='PENDIENTE']
    variables = [v for v in get_variables() if v['estado']=='PENDIENTE']

    if not fijos and not variables:
        await update.message.reply_text("✅ Todo pagado. No hay pendientes.")
        return

    texto = "⏳ *Egresos pendientes de pago*\n\n"
    total = 0
    if fijos:
        texto += "💰 *FIJOS:*\n"
        for f in fijos:
            texto += f"• Día {f['dia']} — {f['concepto']}: {format_sol(f['monto'])}\n"
            total += f['monto']
    if variables:
        texto += "\n📦 *VARIABLES:*\n"
        for v in variables:
            texto += f"• {v['vence']} — {v['concepto']}: {format_sol(v['monto'])}\n"
            total += v['monto']
    texto += f"\n💳 *Total pendiente: {format_sol(total)}*"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    context.user_data['agregando'] = {'paso': 1}
    await update.message.reply_text(
        "➕ *Agregar egreso nuevo*\n\n"
        "¿Cuál es el concepto o proveedor?\n"
        "_(Ej: Insumos García, Comisión Rappi, Delivery)_",
        parse_mode="Markdown"
    )

# ── MANEJO DE FOTOS (CLAUDE VISION) ─────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return

    await update.message.reply_text("🔍 Analizando el comprobante...")

    # Descargar imagen
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    # Analizar con Claude Vision
    datos = await analizar_captura(bytes(image_bytes))

    if not datos.get('monto'):
        await update.message.reply_text(
            "❌ No pude leer el monto del comprobante.\n"
            "Intentá con una foto más clara o registralo manualmente con /agregar"
        )
        return

    # Guardar datos en contexto para confirmación
    context.user_data['pago_pendiente'] = datos
    fecha = datos.get('fecha') or datetime.now().strftime("%d/%m/%Y")
    beneficiario = datos.get('beneficiario') or "No detectado"
    monto = datos.get('monto', 0)
    concepto = datos.get('concepto') or beneficiario

    texto = (
        f"📋 *Leí este pago:*\n\n"
        f"💰 Monto: {format_sol(monto)}\n"
        f"🏢 Beneficiario: {beneficiario}\n"
        f"📅 Fecha: {fecha}\n"
        f"📝 Concepto: {concepto}\n\n"
        f"¿Es correcto?"
    )

    # Buscar si matchea con algún egreso pendiente
    fijos = [f for f in get_fijos() if f['estado']=='PENDIENTE']
    variables = [v for v in get_variables() if v['estado']=='PENDIENTE']
    todos = [(f['concepto'], 'fijo', f['row'], f['monto']) for f in fijos] + \
            [(v['concepto'], 'variable', v['row'], v['monto']) for v in variables]

    # Match simple por monto
    matches = [t for t in todos if abs(t[3] - monto) < 10]

    if matches:
        concepto_match, tipo, row, monto_match = matches[0]
        context.user_data['pago_pendiente']['match'] = {'concepto': concepto_match, 'tipo': tipo, 'row': row, 'monto': monto_match}
        texto += f"\n\n✅ Encontré un egreso que coincide:\n*{concepto_match}* — {format_sol(monto_match)}"

    keyboard = [
        [InlineKeyboardButton("✅ Sí, registrar", callback_data="confirmar_pago")],
        [InlineKeyboardButton("✏️ Corregir concepto", callback_data="corregir_concepto")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
    ]
    await update.message.reply_text(texto, parse_mode="Markdown",
                                     reply_markup=InlineKeyboardMarkup(keyboard))

# ── CALLBACKS BOTONES ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "confirmar_pago":
        pago = context.user_data.get('pago_pendiente', {})
        fecha = pago.get('fecha') or datetime.now().strftime("%d/%m/%Y")
        monto = pago.get('monto', 0)
        match = pago.get('match')

        if match:
            if match['tipo'] == 'fijo':
                marcar_pagado_fijo(match['row'], fecha, monto)
            else:
                marcar_pagado_variable(match['row'], fecha, monto)
            await query.edit_message_text(
                f"✅ *Registrado como pagado*\n\n"
                f"📝 {match['concepto']}\n"
                f"💰 {format_sol(monto)}\n"
                f"📅 {fecha}\n\n"
                f"El Sheet se actualizó automáticamente 🎉",
                parse_mode="Markdown"
            )
        else:
            # No hay match — pedir concepto
            context.user_data['pago_sin_match'] = pago
            await query.edit_message_text(
                "No encontré este pago en la lista de pendientes.\n\n"
                "¿A qué categoría pertenece?\n"
                "_(Ej: Proveedor insumos, Delivery, Marketing, Otro)_"
            )
            context.user_data['esperando_categoria_nueva'] = True

    elif data == "corregir_concepto":
        await query.edit_message_text(
            "✏️ Escribí el concepto correcto:\n"
            "_(Ej: Alquiler Asia, Planilla Karen, Proveedor X)_"
        )
        context.user_data['corrigiendo_concepto'] = True

    elif data == "cancelar":
        context.user_data.clear()
        await query.edit_message_text("❌ Cancelado. No se registró nada.")

# ── MANEJO DE TEXTO (flujo agregar / corregir) ──────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    texto = update.message.text.strip()

    # Flujo: agregar egreso nuevo
    if 'agregando' in context.user_data:
        paso = context.user_data['agregando']['paso']
        if paso == 1:
            context.user_data['agregando']['concepto'] = texto
            context.user_data['agregando']['paso'] = 2
            await update.message.reply_text("💰 ¿Cuánto es el monto? (solo el número, ej: 1500.50)")
        elif paso == 2:
            try:
                monto = float(texto.replace(",","").replace("S/",""))
                context.user_data['agregando']['monto'] = monto
                context.user_data['agregando']['paso'] = 3
                await update.message.reply_text("📅 ¿Cuándo vence? (DD/MM/YYYY o 'hoy')")
            except:
                await update.message.reply_text("❌ No entendí el monto. Escribí solo el número, ej: 1500.50")
        elif paso == 3:
            vence = datetime.now().strftime("%d/%m/%Y") if texto.lower()=="hoy" else texto
            datos = context.user_data['agregando']
            agregar_variable(vence, "Otros", datos['concepto'], datos['monto'])
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ *Egreso agregado al Sheet*\n\n"
                f"📝 {datos['concepto']}\n"
                f"💰 {format_sol(datos['monto'])}\n"
                f"📅 Vence: {vence}",
                parse_mode="Markdown"
            )
        return

    # Flujo: corregir concepto de captura
    if context.user_data.get('corrigiendo_concepto'):
        pago = context.user_data.get('pago_pendiente', {})
        pago['concepto_corregido'] = texto
        context.user_data['corrigiendo_concepto'] = False
        fecha = pago.get('fecha') or datetime.now().strftime("%d/%m/%Y")
        monto = pago.get('monto', 0)
        agregar_variable(fecha, "Otros", texto, monto, notas="Registrado desde bot - captura")
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ *Registrado como nuevo egreso*\n\n"
            f"📝 {texto}\n💰 {format_sol(monto)}\n📅 {fecha}",
            parse_mode="Markdown"
        )
        return

    # Comando rápido: "pagado X"
    if texto.lower().startswith("pagado "):
        concepto_buscar = texto[7:].strip().lower()
        fijos = [f for f in get_fijos() if f['estado']=='PENDIENTE' and concepto_buscar in f['concepto'].lower()]
        variables = [v for v in get_variables() if v['estado']=='PENDIENTE' and concepto_buscar in v['concepto'].lower()]

        if fijos:
            f = fijos[0]
            marcar_pagado_fijo(f['row'], datetime.now().strftime("%d/%m/%Y"), f['monto'])
            await update.message.reply_text(f"✅ Marcado como pagado: *{f['concepto']}* — {format_sol(f['monto'])}", parse_mode="Markdown")
        elif variables:
            v = variables[0]
            marcar_pagado_variable(v['row'], datetime.now().strftime("%d/%m/%Y"), v['monto'])
            await update.message.reply_text(f"✅ Marcado como pagado: *{v['concepto']}* — {format_sol(v['monto'])}", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ No encontré '{concepto_buscar}' en los pendientes.\nUsá /pendientes para ver la lista.")
        return

    # Mensaje genérico
    await update.message.reply_text(
        "No entendí ese mensaje.\n\n"
        "📸 Mandame una *captura de pago* o usá un comando:\n"
        "/hoy /semana /mes /pendientes /agregar",
        parse_mode="Markdown"
    )

# ── RECORDATORIO DIARIO (se llama desde scheduler externo o webhook) ──────────
async def enviar_recordatorio_diario(app, chat_id: str):
    fijos, variables = get_pendientes_hoy()
    hoy = datetime.now().strftime("%d/%m/%Y")

    if not fijos and not variables:
        texto = f"☀️ Buenos días Anika — sin pagos urgentes para hoy ({hoy}) ✅"
    else:
        texto = f"☀️ *Buenos días Anika — Pagos de hoy {hoy}*\n\n"
        total = 0
        if fijos:
            texto += "🔴 *VENCE HOY (fijos):*\n"
            for f in fijos:
                texto += f"• {f['concepto']} — {format_sol(f['monto'])}\n"
                total += f['monto']
        if variables:
            texto += "\n🔴 *VENCE HOY (variables):*\n"
            for v in variables:
                texto += f"• {v['concepto']} — {format_sol(v['monto'])}\n"
                total += v['monto']
        texto += f"\n💳 *Total hoy: {format_sol(total)}*\n\n"
        texto += "📸 Mandame la captura cuando pagues y lo registro solo."

    await app.bot.send_message(chat_id=chat_id, text=texto, parse_mode="Markdown")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("mes", cmd_mes))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CommandHandler("agregar", cmd_agregar))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot PechuFree Egresos iniciado ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
