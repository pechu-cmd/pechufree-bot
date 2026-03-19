import logging
import os
import json
import base64
import httpx
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SHEET_URL  = os.environ.get("GOOGLE_SHEET_URL")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
AUTHORIZED_USERS  = [u.strip() for u in os.environ.get("AUTHORIZED_USERS", "").split(",") if u.strip()]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Columnas en la hoja EGRESOS (1-indexed para gspread)
# A=1 MES | B=2 TIPO | C=3 CATEGORIA | D=4 PROVEEDOR | E=5 PRODUCTO
# F=6 MONTO | G=7 REF | H=8 ESTADO | I=9 FECHA PAGO | J=10 NOTAS

CATEGORIAS = ["CMV","Envases","Personal","Alquiler","Mantenimiento","Servicios","Logística","Impuestos","Financieros","Equipos","Otros"]

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_url(GOOGLE_SHEET_URL)

def get_egresos(mes=None):
    ws = get_sheet().worksheet("💸 EGRESOS")
    rows = ws.get_all_values()
    egresos = []
    for i, row in enumerate(rows[4:], start=5):  # data desde fila 5
        if len(row) < 5 or not row[4]:  # necesita al menos PRODUCTO
            continue
        try:
            monto_str = str(row[5]).replace(",","").replace("S/","").strip() if len(row) > 5 else "0"
            monto = float(monto_str) if monto_str else 0.0
            e = {
                "row":       i,
                "mes":       row[0].strip(),
                "tipo":      row[1].strip(),
                "categoria": row[2].strip(),
                "proveedor": row[3].strip(),
                "producto":  row[4].strip(),
                "monto":     monto,
                "ref":       row[6].strip() if len(row) > 6 else "",
                "estado":    row[7].strip() if len(row) > 7 else "PENDIENTE",
                "fecha_pago":row[8].strip() if len(row) > 8 else "",
                "notas":     row[9].strip() if len(row) > 9 else "",
            }
            if mes is None or e["mes"] == mes:
                egresos.append(e)
        except Exception as ex:
            logger.warning(f"Error parsing row {i}: {ex}")
    return egresos

def mes_actual():
    return datetime.now().strftime("%m/%Y")

def agregar_egreso(mes, tipo, categoria, proveedor, producto, monto, ref="", notas=""):
    ws = get_sheet().worksheet("💸 EGRESOS")
    ws.append_row([mes, tipo, categoria, proveedor, producto, monto, ref, "PENDIENTE", "", notas])

def marcar_pagado(row_num, fecha_pago, monto_pagado):
    ws = get_sheet().worksheet("💸 EGRESOS")
    ws.update_cell(row_num, 8, "PAGADO")    # col H
    ws.update_cell(row_num, 9, fecha_pago)  # col I

def nuevo_mes(mes_origen, mes_destino):
    egresos = get_egresos(mes=mes_origen)
    fijos = [e for e in egresos if e["tipo"] == "FIJO"]
    ws = get_sheet().worksheet("💸 EGRESOS")
    for e in fijos:
        ws.append_row([mes_destino, "FIJO", e["categoria"], e["proveedor"],
                       e["producto"], e["monto"], e["ref"], "PENDIENTE", "",
                       f"Copiado desde {mes_origen}"])
    return len(fijos)

# ── CLAUDE VISION ─────────────────────────────────────────────────────────────
async def analizar_imagen(image_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json",
                         "x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01"},
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text", "text": 'Analizá este comprobante de pago. Respondé SOLO con JSON puro sin backticks: {"monto": numero, "proveedor": "nombre empresa o persona", "producto": "descripcion del pago o null", "fecha": "DD/MM/YYYY", "moneda": "PEN o USD"}. Si no podés leer un campo ponelo null.'}
                    ]}]
                }
            )
        data = resp.json()
        logger.info(f"Claude Vision status: {resp.status_code}")
        if "error" in data:
            logger.error(f"Claude error: {data['error']['message']}")
            return {}
        text = data["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        logger.error(f"Vision error: {e}")
        return {}

# ── AUTH ─────────────────────────────────────────────────────────────────────
def autorizado(user_id: int) -> bool:
    if not AUTHORIZED_USERS:
        return True
    return str(user_id) in AUTHORIZED_USERS

# ── HELPERS ───────────────────────────────────────────────────────────────────
def fmt(monto):
    return f"S/ {monto:,.2f}"

def teclado_categorias():
    rows = []
    for i in range(0, len(CATEGORIAS), 2):
        row = [InlineKeyboardButton(CATEGORIAS[i], callback_data=f"cat_{CATEGORIAS[i]}")]
        if i+1 < len(CATEGORIAS):
            row.append(InlineKeyboardButton(CATEGORIAS[i+1], callback_data=f"cat_{CATEGORIAS[i+1]}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ── COMANDOS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id):
        await update.message.reply_text("❌ No tenés acceso.")
        return
    await update.message.reply_text(
        "👋 *Hola\\! Bot de egresos PechuFree\\.*\n\n"
        "📸 Mandame una *captura de pago* y lo registro\\.\n\n"
        "*Comandos:*\n"
        "/hoy — Pendientes de hoy\n"
        "/mes — Resumen del mes\n"
        "/pendientes — Todo lo pendiente\n"
        "/agregar — Agregar egreso manual\n"
        "/nuevomes — Copiar fijos al mes siguiente",
        parse_mode="MarkdownV2"
    )

async def cmd_mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id): return
    try:
        mes = mes_actual()
        egresos = get_egresos(mes=mes)
        if not egresos:
            await update.message.reply_text(f"📭 No hay egresos cargados para {mes}.")
            return
        total     = sum(e["monto"] for e in egresos)
        pagado    = sum(e["monto"] for e in egresos if e["estado"] == "PAGADO")
        pendiente = sum(e["monto"] for e in egresos if e["estado"] == "PENDIENTE")
        cmv       = sum(e["monto"] for e in egresos if e["categoria"] == "CMV")
        pct       = (pagado/total*100) if total > 0 else 0
        texto = (
            f"📊 *Resumen {mes}*\n\n"
            f"🧾 Total: {fmt(total)}\n"
            f"🥩 CMV: {fmt(cmv)}\n"
            f"✅ Pagado: {fmt(pagado)}\n"
            f"⏳ Pendiente: {fmt(pendiente)}\n"
            f"📈 Ejecutado: {pct:.1f}%"
        )
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id): return
    try:
        mes = mes_actual()
        egresos = [e for e in get_egresos(mes=mes) if e["estado"] == "PENDIENTE"]
        if not egresos:
            await update.message.reply_text("✅ Todo pagado. Sin pendientes.")
            return
        total = sum(e["monto"] for e in egresos)
        texto = f"⏳ *Pendientes {mes}*\n\n"
        cat_actual = ""
        for e in sorted(egresos, key=lambda x: x["categoria"]):
            if e["categoria"] != cat_actual:
                cat_actual = e["categoria"]
                texto += f"\n*{cat_actual}*\n"
            nombre = e["producto"] or e["proveedor"]
            monto_str = fmt(e["monto"]) if e["monto"] else "—"
            texto += f"• {nombre} — {monto_str}\n"
        texto += f"\n💳 *Total: {fmt(total)}*"
        await update.message.reply_text(texto, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id): return
    await cmd_pendientes(update, context)

async def cmd_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id): return
    context.user_data.clear()
    context.user_data["agregando"] = {"paso": "producto"}
    await update.message.reply_text(
        "➕ *Agregar egreso*\n\n¿Cuál es el producto o concepto del gasto?\n_(ej: Harina de almendra, Planilla Karen, Luz marzo)_",
        parse_mode="Markdown"
    )

async def cmd_nuevomes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id): return
    context.user_data.clear()
    mes = mes_actual()
    m, y = int(mes[:2]), int(mes[3:])
    m += 1
    if m > 12: m, y = 1, y+1
    siguiente = f"{m:02d}/{y}"
    context.user_data["mes_origen"]  = mes
    context.user_data["mes_destino"] = siguiente
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Crear {siguiente}", callback_data="confirmar_nuevomes"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")
    ]])
    await update.message.reply_text(
        f"📅 ¿Copiamos todos los egresos *FIJOS* de `{mes}` al mes `{siguiente}`?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ── FOTO ──────────────────────────────────────────────────────────────────────
async def handle_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id): return
    await update.message.reply_text("🔍 Analizando el comprobante...")
    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        datos = await analizar_imagen(bytes(image_bytes))

        if not datos or not datos.get("monto"):
            await update.message.reply_text(
                "❌ No pude leer el monto.\n\n"
                "Intentá con una foto más clara o usá /agregar para cargarlo manual."
            )
            return

        monto     = datos["monto"]
        proveedor = datos.get("proveedor") or "No detectado"
        producto  = datos.get("producto") or proveedor
        fecha     = datos.get("fecha") or datetime.now().strftime("%d/%m/%Y")

        context.user_data.clear()
        context.user_data["foto_datos"] = {
            "monto": monto, "proveedor": proveedor,
            "producto": producto, "fecha": fecha
        }

        # Buscar match por monto en pendientes
        try:
            pendientes = [e for e in get_egresos(mes=mes_actual()) if e["estado"] == "PENDIENTE"]
            matches = [e for e in pendientes if e["monto"] > 0 and abs(e["monto"] - monto) < 15]
            if matches:
                context.user_data["foto_datos"]["match"] = matches[0]
        except Exception as ex:
            logger.warning(f"No se pudo buscar match: {ex}")

        match = context.user_data["foto_datos"].get("match")
        texto = (
            f"📋 *Leí este pago:*\n\n"
            f"💰 Monto: {fmt(monto)}\n"
            f"🏢 Proveedor: {proveedor}\n"
            f"📦 Producto: {producto}\n"
            f"📅 Fecha: {fecha}\n"
        )
        if match:
            texto += f"\n✅ Coincide con: *{match['producto']}* ({match['proveedor']}) — {fmt(match['monto'])}"
        else:
            texto += "\n⚠️ No encontré coincidencia en pendientes — se registrará como nuevo egreso."

        keyboard = [
            [InlineKeyboardButton("✅ Registrar", callback_data="foto_confirmar")],
            [InlineKeyboardButton("✏️ Cambiar datos", callback_data="foto_cambiar")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
        ]
        await update.message.reply_text(texto, parse_mode="Markdown",
                                         reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error foto: {e}")
        await update.message.reply_text("❌ Error procesando la imagen. Intentá de nuevo.")

# ── CALLBACKS ─────────────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "cancelar":
        context.user_data.clear()
        await q.edit_message_text("❌ Cancelado.")
        return

    # ── Foto: confirmar
    if data == "foto_confirmar":
        fd    = context.user_data.get("foto_datos", {})
        match = fd.get("match")
        fecha = fd.get("fecha") or datetime.now().strftime("%d/%m/%Y")
        monto = fd.get("monto", 0)
        try:
            if match:
                marcar_pagado(match["row"], fecha, monto)
                await q.edit_message_text(
                    f"✅ *Pagado registrado*\n\n"
                    f"📦 {match['producto']}\n"
                    f"🏢 {match['proveedor']}\n"
                    f"💰 {fmt(monto)}\n📅 {fecha}\n\n"
                    f"Sheet actualizado 🎉", parse_mode="Markdown"
                )
                context.user_data.clear()
            else:
                # Sin match → pedir categoría
                context.user_data["foto_sin_match"] = True
                await q.edit_message_text(
                    "¿A qué categoría corresponde este gasto?",
                    reply_markup=teclado_categorias()
                )
        except Exception as e:
            await q.edit_message_text(f"❌ Error al guardar: {e}")
            context.user_data.clear()
        return

    # ── Foto: cambiar datos
    if data == "foto_cambiar":
        context.user_data["foto_cambiar_dato"] = True
        await q.edit_message_text(
            "✏️ Escribí el *producto o concepto* correcto:"
        )
        return

    # ── Selección de categoría
    if data.startswith("cat_"):
        cat = data[4:]

        # Caso: foto sin match
        if context.user_data.get("foto_sin_match"):
            fd       = context.user_data.get("foto_datos", {})
            mes      = mes_actual()
            proveedor= fd.get("proveedor") or ""
            producto = fd.get("producto") or proveedor or "Pago sin identificar"
            monto    = fd.get("monto", 0)
            fecha    = fd.get("fecha") or datetime.now().strftime("%d/%m/%Y")
            try:
                agregar_egreso(mes, "VARIABLE", cat, proveedor, producto, monto,
                               notas="Registrado desde bot - foto")
                await q.edit_message_text(
                    f"✅ *Egreso registrado*\n\n"
                    f"📦 {producto}\n🏢 {proveedor}\n"
                    f"💰 {fmt(monto)}\n🏷 {cat}\n📅 {fecha}",
                    parse_mode="Markdown"
                )
            except Exception as e:
                await q.edit_message_text(f"❌ Error al guardar: {e}")
            context.user_data.clear()
            return

        # Caso: agregar manual
        if context.user_data.get("agregando"):
            context.user_data["agregando"]["categoria"] = cat
            context.user_data["agregando"]["paso"] = "tipo"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 FIJO", callback_data="tipo_FIJO"),
                InlineKeyboardButton("📦 VARIABLE", callback_data="tipo_VARIABLE")
            ]])
            await q.edit_message_text(
                f"Categoría: *{cat}*\n\n¿Es un gasto fijo o variable?",
                parse_mode="Markdown", reply_markup=keyboard
            )
        return

    # ── Tipo de egreso
    if data.startswith("tipo_"):
        tipo = data[5:]
        if context.user_data.get("agregando"):
            context.user_data["agregando"]["tipo"] = tipo
            context.user_data["agregando"]["paso"] = "confirmar"
            ag = context.user_data["agregando"]
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirmar", callback_data="agregar_confirmar"),
                InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")
            ]])
            await q.edit_message_text(
                f"📋 *Resumen del egreso:*\n\n"
                f"📦 {ag.get('producto','')}\n"
                f"🏢 {ag.get('proveedor','—')}\n"
                f"💰 {fmt(ag.get('monto', 0))}\n"
                f"🏷 {ag.get('categoria')} — {tipo}\n"
                f"📅 Mes: {mes_actual()}\n\n"
                f"¿Confirmás?",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        return

    # ── Confirmar agregar
    if data == "agregar_confirmar":
        ag = context.user_data.get("agregando", {})
        try:
            agregar_egreso(
                mes       = mes_actual(),
                tipo      = ag.get("tipo", "VARIABLE"),
                categoria = ag.get("categoria", "Otros"),
                proveedor = ag.get("proveedor", ""),
                producto  = ag.get("producto", ""),
                monto     = ag.get("monto", 0),
                notas     = ag.get("notas", "")
            )
            await q.edit_message_text(
                f"✅ *Egreso agregado*\n\n"
                f"📦 {ag.get('producto')}\n"
                f"🏢 {ag.get('proveedor','—')}\n"
                f"💰 {fmt(ag.get('monto', 0))}\n"
                f"🏷 {ag.get('categoria')}\n"
                f"📅 {mes_actual()}",
                parse_mode="Markdown"
            )
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {e}")
        context.user_data.clear()
        return

    # ── Confirmar nuevo mes
    if data == "confirmar_nuevomes":
        origen  = context.user_data.get("mes_origen")
        destino = context.user_data.get("mes_destino")
        try:
            n = nuevo_mes(origen, destino)
            await q.edit_message_text(
                f"✅ *Mes {destino} creado*\n\n"
                f"Se copiaron {n} egresos fijos de {origen}.\n"
                f"Los variables los agregás con /agregar.",
                parse_mode="Markdown"
            )
        except Exception as e:
            await q.edit_message_text(f"❌ Error: {e}")
        context.user_data.clear()
        return

# ── TEXTO ─────────────────────────────────────────────────────────────────────
async def handle_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorizado(update.effective_user.id): return
    texto = update.message.text.strip()

    # Flujo agregar manual
    if context.user_data.get("agregando"):
        paso = context.user_data["agregando"].get("paso")

        if paso == "producto":
            context.user_data["agregando"]["producto"] = texto
            context.user_data["agregando"]["paso"] = "proveedor"
            await update.message.reply_text(
                "🏢 ¿Cuál es el proveedor?\n_(ej: Bella Power, SUNAT, Luz del Sur — o escribí *-* si no aplica)_",
                parse_mode="Markdown"
            )
            return

        if paso == "proveedor":
            context.user_data["agregando"]["proveedor"] = "" if texto == "-" else texto
            context.user_data["agregando"]["paso"] = "monto"
            await update.message.reply_text("💰 ¿Cuánto es el monto? (solo número, ej: 1500.50)")
            return

        if paso == "monto":
            try:
                monto = float(texto.replace(",","").replace("S/","").strip())
                context.user_data["agregando"]["monto"] = monto
                context.user_data["agregando"]["paso"] = "categoria"
                await update.message.reply_text(
                    f"Monto: {fmt(monto)}\n\n¿A qué categoría corresponde?",
                    reply_markup=teclado_categorias()
                )
            except:
                await update.message.reply_text("❌ No entendí el monto. Escribí solo el número, ej: 1500.50")
            return

    # Flujo foto: cambiar dato
    if context.user_data.get("foto_cambiar_dato"):
        context.user_data["foto_datos"]["producto"] = texto
        context.user_data.pop("foto_cambiar_dato")
        fd    = context.user_data["foto_datos"]
        match = fd.get("match")
        keyboard = [
            [InlineKeyboardButton("✅ Registrar", callback_data="foto_confirmar")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")],
        ]
        await update.message.reply_text(
            f"📋 *Datos actualizados:*\n\n"
            f"📦 {texto}\n🏢 {fd.get('proveedor','')}\n"
            f"💰 {fmt(fd['monto'])}\n📅 {fd.get('fecha','')}\n\n"
            f"{'✅ Match: *'+match['producto']+'*' if match else 'Se registrará como nuevo egreso.'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Comando rápido: "pagado X"
    if texto.lower().startswith("pagado "):
        buscar = texto[7:].strip().lower()
        try:
            pendientes = [e for e in get_egresos(mes=mes_actual())
                          if e["estado"] == "PENDIENTE"
                          and (buscar in e["producto"].lower() or buscar in e["proveedor"].lower())]
            if pendientes:
                e = pendientes[0]
                marcar_pagado(e["row"], datetime.now().strftime("%d/%m/%Y"), e["monto"])
                await update.message.reply_text(
                    f"✅ *Pagado:* {e['producto']} — {fmt(e['monto'])}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"❌ No encontré '{buscar}' en pendientes.")
        except Exception as ex:
            await update.message.reply_text(f"❌ Error: {ex}")
        return

    await update.message.reply_text(
        "No entendí ese mensaje.\n\n"
        "📸 Mandame una *captura* o usá:\n"
        "/hoy /mes /pendientes /agregar /nuevomes",
        parse_mode="Markdown"
    )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("hoy",        cmd_hoy))
    app.add_handler(CommandHandler("mes",        cmd_mes))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CommandHandler("agregar",    cmd_agregar))
    app.add_handler(CommandHandler("nuevomes",   cmd_nuevomes))
    app.add_handler(MessageHandler(filters.PHOTO, handle_foto))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_texto))
    logger.info("Bot PechuFree v3 iniciado ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
