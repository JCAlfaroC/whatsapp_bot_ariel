# --- app.py (Versión final con formato de fecha AAAA-MM-DD y flujo simplificado) ---

# from doctest import NORMALIZE_WHITESPACE
import json
import locale
import os
import re

# from smtplib import SMTP_PORT
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from thefuzz import process

# --- Configuración del Idioma ---
try:
    locale.setlocale(locale.LC_TIME, "es_ES.UTF-8")  # Spanish dates
except (locale.Error, Exception):  # reads .env file
    try:
        locale.setlocale(locale.LC_TIME, "Spanish_Spain.1252")
    except (locale.Error, Exception):
        print("ADVERTENCIA: Locale en español no encontrado.")

# --- Configuración de la Aplicación ---
load_dotenv()
app = Flask(__name__)


@app.route("/test", methods=["GET"])
def test():
    return "OK", 200


EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")  # wahtsapp gateway
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME")
LOLCLI_API_URL = os.getenv("LOLCLI_API_URL")  # clinic system
LOLCLI_ENTIDAD = os.getenv("LOLCLI_ENTIDAD")
LOLCLI_API_TOKEN = os.getenv("LOLCLI_API_TOKEN")

user_sessions = {}  # stores all active conversations in RAM
lista_sedes_global = []  # clinic branches (loaded once at startup)
lista_documentos_global = []  # document types (loaded once at startup)

# --- Configuración de Tiempos de Inactividad ---
INACTIVITY_REMINDER_PERIOD = 5 * 60
SESSION_EXPIRATION_PERIOD = 10 * 60


# --- Tarea en segundo plano y funciones auxiliares ---
def session_cleanup_task():
    while True:
        time.sleep(60)
        current_time = time.time()
        for sender in list(user_sessions.keys()):
            session = user_sessions.get(sender)
            if (
                not session
                or "last_interaction_time" not in session
                or session.get("state") == "START"
            ):
                continue
            inactive_time = current_time - session["last_interaction_time"]
            phone_to_reply = sender.split("@")[0]
            if inactive_time > SESSION_EXPIRATION_PERIOD:
                print(f"INFO: Sesión para {sender} expirada por inactividad.")

                # WARNING (changes for the 7 messages sent before payment confirmation)
                if session.get("invnum_cita"):
                    print(
                        f"ALERTA: Cita registrada sin pago confirmado. "
                        f"invnum={session['invnum_cita']}, paciente={session.get('paciente_nombre', '?')}, "
                        f"teléfono={phone_to_reply}. Requiere cancelación manual en LOLCLI."
                    )

                send_whatsapp_message(
                    phone_to_reply,
                    "⏰ Tu sesión ha cerrado por inactividad. Cuando quieras continuar, solo escríbenos y estaremos listos para ayudarte. 😊",
                )
                user_sessions.pop(sender, None)
                continue
            if inactive_time > INACTIVITY_REMINDER_PERIOD and not session.get(
                "reminder_sent"
            ):
                print(f"INFO: Enviando recordatorio de inactividad a {sender}.")
                send_whatsapp_message(
                    phone_to_reply,
                    "👋 ¡Hola! Notamos que dejaste tu cita a medias. ¿Deseas continuar? Si no respondemos pronto, tu sesión se cerrará automáticamente. 🕐",
                )
                session["reminder_sent"] = True


def save_reminder(session):
    fecha = session.get("fecha_api", "")
    hora = session.get("hora_api", "")
    try:
        apt_datetime = datetime.strptime(fecha + hora, "%Y%m%d%H%M").strftime(
            "%Y-%m-%d %H:%M"
        )
    except (ValueError, TypeError):
        apt_datetime = "Fecha no disponible"

    reminder = {
        "phone": session.get("sender"),
        "email": session.get("email"),
        "patient_name": session.get("paciente_nombre", "Paciente"),
        "doctor_name": session.get("mednam", ""),
        "specialty": session.get("sernam", ""),
        "sede": session.get("establishment_name", ""),
        "appointment_datetime": apt_datetime,
        "reminded": False,
    }

    reminders = []
    if os.path.exists("reminders.json"):
        try:
            with open("reminders.json", "r") as f:
                reminders = json.load(f)
        except Exception:
            reminders = []

    reminders.append(reminder)
    with open("reminders.json", "w") as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)
    print(
        f"INFO: Recordatorio guardado para {reminder['patient_name']} -- {apt_datetime}"
    )


def reminder_task():
    while True:
        time.sleep(3600)  # check every hour
        now = datetime.now()

        if not os.path.exists("reminders.json"):
            continue
        try:
            with open("reminders.json", "r") as f:
                reminders = json.load(f)
        except Exception:
            continue

        updated = False
        for reminder in reminders:
            if reminder.get("reminded"):
                continue
            try:
                apt_dt = datetime.strptime(
                    reminder["appointment_datetime"], "%Y-%m-%d %H:%M"
                )
            except (ValueError, TypeError):
                continue

            hours_until = (apt_dt - now).total_seconds() / 3600

            if 23 <= hours_until <= 25:
                whatsapp_msg = (
                    f"🔔 *Recordatorio de cita -- ARIE*\n\n"
                    f"Hola {reminder['patient_name']}, le recordamos su cita de mañana:\n\n"
                    f"👨‍⚕️ *Médico:* {reminder['doctor_name']}\n"
                    f"🩺 *Especialidad:* {reminder['specialty']}\n"
                    f"🏥 *Sede:* {reminder['sede']}\n"
                    f"⏰ *Hora:* {reminder['appointment_datetime']}\n"
                    f"Por favor, preséntese 15 minutos antes. 😊"
                )
                send_whatsapp_message(reminder["phone"], whatsapp_msg)
                reminder["reminded"] = True
                updated = True

        if updated:
            with open("reminders.json", "w") as f:
                json.dump(reminders, f, ensure_ascii=False, indent=2)


def preload_global_lists():
    global lista_sedes_global, lista_documentos_global
    headers = {
        "Authorization": f"Basic {LOLCLI_API_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        response_sedes = requests.post(
            f"{LOLCLI_API_URL}/ListaEstablecimientos",
            json={"entidad": LOLCLI_ENTIDAD},
            headers=headers,
            timeout=5,
        )
        if response_sedes.ok:
            lista_sedes_global = response_sedes.json().get("establecimientos", [])
            print(f"INFO: Se han cargado {len(lista_sedes_global)} sedes.")
        response_docs = requests.post(
            f"{LOLCLI_API_URL}/ListaTipoDocumentoElolcli",
            json={},
            headers=headers,
            timeout=5,
        )
        if response_docs.ok:
            docs_filtrados = [
                doc
                for doc in response_docs.json().get("tipoDocumentos", [])
                if doc["tidcod"] in ["01", "02", "03", "04"]
            ]
            lista_documentos_global = docs_filtrados
            print(
                f"INFO: Se han cargado {len(lista_documentos_global)} tipos de documento."
            )
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Fallo en la conexión con la API al pre-cargar listas: {e}")


def normalize_text(text):
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    text = text.replace(".", "").replace(",", "").replace("-", " ")
    text = text.replace(" s a c", "").replace(" sac", "")
    return " ".join(text.split())


def send_whatsapp_message(phone_number, text):
    time.sleep(1.5)
    headers = {"apikey": EVOLUTION_API_KEY}
    payload = {"number": phone_number, "text": text}
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE_NAME}"
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        print(f"Mensaje enviado con éxito a {phone_number}.")
    except requests.exceptions.RequestException as e:
        print(f"ERROR AL ENVIAR MENSAJE: {e}")


def format_menu(title, items, key_id, key_name):
    menu_text = f"{title}\n\n"
    formatted_items = []
    for i, item in enumerate(items, 1):
        display_name = item.get(key_name, "")
        if key_id == "citdat":
            try:
                date_obj = datetime.strptime(item.get(key_id, ""), "%Y%m%d")
                display_name = format_date_es(date_obj)
            except (ValueError, TypeError):
                display_name = item.get(key_id, "Fecha inválida")
        menu_text += f"*{i}.* {display_name}\n"
        item_data = {"id": i, "data": item}
        formatted_items.append(item_data)
    menu_text += "\n_Escribe el número o el nombre de tu elección._\n_También puedes escribir *'retroceder'* o *'salir'*._"
    return menu_text, formatted_items


def process_user_choice(user_input, options, key_name=None):
    try:
        choice_index = int(user_input) - 1
        if 0 <= choice_index < len(options):
            return options[choice_index]["data"]
    except (ValueError, IndexError):
        if not key_name:
            return None
        normalized_input = normalize_text(user_input)
        for opt in options:
            item_text = opt["data"].get(key_name, "")
            if normalize_text(item_text) == normalized_input:
                return opt["data"]
        option_names = [opt["data"].get(key_name, "") for opt in options]
        best_match, score = process.extractOne(user_input, option_names)
        if score > 75:
            for opt in options:
                if opt["data"].get(key_name, "") == best_match:
                    return opt["data"]
    return None


def replay_state_prompt(state, session, phone_to_reply, headers):
    lolcli_headers = headers
    print(f"Retrocediendo al estado: {state}")
    if state == "AWAITING_ESTABLISHMENT":
        response = requests.post(
            f"{LOLCLI_API_URL}/ListaEstablecimientos",
            json={"entidad": LOLCLI_ENTIDAD},
            headers=headers,
        )
        options = response.json().get("establecimientos", [])
        reply, formatted_options = format_menu(
            "Claro, volvamos a elegir. ¿En cuál de nuestras sedes te gustaría atenderte?",
            options,
            "siscod",
            "sisent",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    elif state == "AWAITING_SPECIALTY":
        response = requests.post(
            f"{LOLCLI_API_URL}/ListaServiciosWsp",
            json={"siscod": session["siscod"]},
            headers=headers,
        )
        options = response.json().get("servicios", [])
        reply, formatted_options = format_menu(
            "No hay problema. Dime de nuevo, ¿para qué especialidad necesitas la cita?",
            options,
            "sercod",
            "serdes",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    elif state == "AWAITING_DOCTOR":
        payload_medicos = {"siscod": session["siscod"], "sercod": session["sercod"]}
        response_medicos = requests.post(
            f"{LOLCLI_API_URL}/ListaMedicos",
            json=payload_medicos,
            headers=lolcli_headers,
        )
        medicos = response_medicos.json().get("medicos", [])
        reply, formatted_options = format_menu(
            "Ok, volvamos a la selección de doctor. ¿Con quién deseas atenderte?",
            medicos,
            "medcod",
            "mednam",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    elif state == "AWAITING_AVAILABLE_DATE":
        today_str = date.today().strftime("%Y%m%d")
        payload = {
            "siscod": session["siscod"],
            "sercod": session["sercod"],
            "medcod": session["medcod"],
            "fecha": today_str,
        }
        response = requests.post(
            f"{LOLCLI_API_URL}/ListaCuposDisponibles",
            json=payload,
            headers=lolcli_headers,
        )
        fechas_disponibles = response.json().get("cupos", [])
        reply, formatted_options = format_menu(
            "Entendido. Elige nuevamente una de las fechas disponibles:",
            fechas_disponibles,
            "citdat",
            "citdat",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    else:
        send_whatsapp_message(
            phone_to_reply,
            "↩️ Te hemos llevado al inicio. Cuando estés listo/a, escríbenos hola y empezamos de nuevo. 😊",
        )
        session.clear()
        session["state"] = "START"


def generate_payment_link_and_send(session, phone_to_reply, headers):
    try:
        # Obtenemos el número de orden/cita y aseguramos que sea Entero (integer) para la API
        invnum_val = session.get("invnum_cita")
        invnum = int(invnum_val) if invnum_val else 0

        payload_pago = {
            "cliente": "arie_pruebas",
            "invnum": invnum,
            "paydat": datetime.now().strftime("%d-%m-%Y %H:%M:%S.000"),
        }

        url_pago = f"{LOLCLI_API_URL}/GenerarLinkPagoCita"
        print(f"INFO: Generando link de pago con payload: {payload_pago}")

        response_link = requests.post(url_pago, json=payload_pago, headers=headers)
        response_link.raise_for_status()
        data_link = response_link.json()

        if data_link.get("status") == "success" and data_link.get("payment_link"):
            payment_url = data_link["payment_link"]
            # FASE DE PRUEBAS: LOLCLI aún devuelve el dominio de producción
            # (qullana.com) en "payment_link". Para que el pago se procese en el
            # entorno de pruebas de Niubiz se antepone "qa-pacientes." al host.
            # Quitar este bloque cuando el bot pase a producción real.
            parsed_url = urlparse(payment_url)
            if not parsed_url.netloc.startswith("qa-pacientes."):
                parsed_url = parsed_url._replace(netloc=f"qa-pacientes.{parsed_url.netloc}")
                payment_url = urlunparse(parsed_url)
            try:
                token = payment_url.split("/")[-1]
                session["payment_token"] = token
            except Exception:
                session["payment_token"] = None
            costo_total = session.get("costo_total", 0.0)
            send_whatsapp_message(
                phone_to_reply,
                f"Para completar tu reserva, realiza el pago de *S/ {costo_total:.2f}* en el siguiente enlace:\n\n{payment_url}\n\nCuando hayas completado el pago en la página, regresa aquí y escríbeme *'listo'* para confirmar tu cita. ✅",
            )
            session["state"] = "AWAITING_PAYMENT_CONFIRMATION"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Tuvimos un problema al generar tu enlace de pago. Por favor, intenta de nuevo en un momento.",
            )
            session["state"] = "AWAITING_CONFIRMATION"

    except requests.exceptions.HTTPError as err:
        print(
            f"ERROR HTTP en generate_payment_link_and_send: {err.response.status_code} - {err.response.text}"
        )
        send_whatsapp_message(
            phone_to_reply,
            "😔 Tuvimos un problema de comunicación al preparar tu pago. Por favor, intenta de nuevo en unos momentos. 🙏",
        )
    except Exception as e:
        print(f"ERROR en generate_payment_link_and_send: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al preparar tu pago. Por favor, intenta de nuevo o contáctanos. 🙏",
        )


# Derecho de reprogramación de citas: oricod/tarcod son fijos para este
# concepto de cobro (confirmados por LOLIMSA junto con el payload de
# GenerarLinkPagoOrdenPrefactura), no varían por cita ni por paciente.
RESCHEDULE_FEE_ORICOD = "PM"
RESCHEDULE_FEE_TARCOD = "001001"


def generate_reschedule_payment_link_and_send(session, phone_to_reply, headers):
    try:
        payload_pago = {
            "oricod": RESCHEDULE_FEE_ORICOD,
            "tarcod": RESCHEDULE_FEE_TARCOD,
            "pachis": session.get("pachis"),
            "cliente": "arie_pruebas",
        }
        url_pago = f"{LOLCLI_API_URL}/GenerarLinkPagoOrdenPrefactura"
        print(f"INFO: Generando link de pago de reprogramación con payload: {payload_pago}")

        response_link = requests.post(url_pago, json=payload_pago, headers=headers)
        response_link.raise_for_status()
        data_link = response_link.json()

        if data_link.get("status") == "success" and data_link.get("payment_link"):
            payment_url = data_link["payment_link"]
            # FASE DE PRUEBAS: ver nota idéntica en generate_payment_link_and_send.
            parsed_url = urlparse(payment_url)
            if not parsed_url.netloc.startswith("qa-pacientes."):
                parsed_url = parsed_url._replace(netloc=f"qa-pacientes.{parsed_url.netloc}")
                payment_url = urlunparse(parsed_url)
            session["reschedule_payment_token"] = data_link.get("token")
            monto = data_link.get("monto", 15)
            send_whatsapp_message(
                phone_to_reply,
                f"Para confirmar tu reprogramación, realiza el pago del derecho de reprogramación de citas de "
                f"*S/ {monto:.2f}* en el siguiente enlace:\n\n{payment_url}\n\nCuando hayas completado el pago "
                "en la página, regresa aquí y escríbeme *'listo'* para confirmar tu reprogramación. ✅",
            )
            session["state"] = "AWAITING_RESCHEDULE_PAYMENT_CONFIRMATION"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Tuvimos un problema al generar tu enlace de pago. Por favor, intenta de nuevo en un momento.",
            )
            session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"

    except requests.exceptions.HTTPError as err:
        print(
            f"ERROR HTTP en generate_reschedule_payment_link_and_send: {err.response.status_code} - {err.response.text}"
        )
        send_whatsapp_message(
            phone_to_reply,
            "😔 Tuvimos un problema de comunicación al preparar tu pago. Por favor, intenta de nuevo en unos momentos. 🙏",
        )
        session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"
    except Exception as e:
        print(f"ERROR en generate_reschedule_payment_link_and_send: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al preparar tu pago. Por favor, intenta de nuevo o contáctanos. 🙏",
        )
        session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"


def continue_appointment_flow(session, phone_to_reply, lolcli_headers):
    send_whatsapp_message(
        phone_to_reply,
        "✅ ¡Excelente! Ya tenemos tus datos. Ahora continuemos con los detalles de tu cita. 😊",
    )
    response_est = requests.post(
        f"{LOLCLI_API_URL}/ListaEstablecimientos",
        json={"entidad": LOLCLI_ENTIDAD},
        headers=lolcli_headers,
    )
    establecimientos = response_est.json().get("establecimientos", [])
    reply, opts = format_menu(
        "Para empezar, ¿en cuál de nuestras sedes te gustaría atenderte?",
        establecimientos,
        "siscod",
        "sisent",
    )
    session["options"] = opts
    session["state"] = "AWAITING_ESTABLISHMENT"
    send_whatsapp_message(phone_to_reply, reply)


def present_specialty_or_force_reeval(session, phone_to_reply, lolcli_headers, servicios, intro_text):
    if session.get("flow") == "reeval":
        match = next(
            (s for s in servicios if REEVAL_SERVICE_NAME in normalize_text(s.get("serdes", ""))),
            None,
        )
        if not match:
            send_whatsapp_message(
                phone_to_reply,
                f"😔 *{session['establishment_name']}* no ofrece reevaluación médica de Medicina Física y "
                "Rehabilitación. Escribe retroceder para elegir otra sede o salir para cancelar.",
            )
            if session.get("history"):
                session["history"].pop()
            return
        session["sercod"] = match["sercod"]
        session["sernam"] = match["serdes"]
        fetch_and_prompt_doctors(session, phone_to_reply, lolcli_headers)
        return

    reply, formatted_options = format_menu(intro_text, servicios, "sercod", "serdes")
    session["options"] = formatted_options
    session["state"] = "AWAITING_SPECIALTY"
    send_whatsapp_message(phone_to_reply, reply)


def fetch_and_prompt_doctors(session, phone_to_reply, lolcli_headers):
    payload_medicos = {"siscod": session["siscod"], "sercod": session["sercod"]}
    response_medicos = requests.post(
        f"{LOLCLI_API_URL}/ListaMedicos",
        json=payload_medicos,
        headers=lolcli_headers,
    )
    medicos = response_medicos.json().get("medicos", [])
    if not medicos:
        send_whatsapp_message(
            phone_to_reply,
            f"Lo sentimos, no hay doctores para *{session['sernam']}* en esta sede.",
        )
        send_whatsapp_message(
            phone_to_reply,
            "Escribe retroceder para elegir otra especialidad o salir para cancelar.",
        )
        if session.get("history"):
            session["history"].pop()
    else:
        reply, formatted_options = format_menu(
            "Estos son los doctores con espacio:", medicos, "medcod", "mednam"
        )
        session["options"] = formatted_options
        session["state"] = "AWAITING_DOCTOR"
        send_whatsapp_message(phone_to_reply, reply)


def show_final_summary(session, phone_to_reply):
    patient_name = session.get("paciente_nombre")

    summary = (
        f"¡Casi listo! ✨ Por favor, revisa que todo esté correcto:\n\n"
        f"👤 *Paciente:* {patient_name}\n"
        f"🏥 *Sede:* {session['establishment_name']}\n"
        f"🩺 *Especialidad:* {session['sernam']}\n"
        f"👨‍⚕️ *Médico:* {session['mednam']}\n"
        f"🗓️ *Fecha:* {session['fecha_user']}\n"
        f"⏰ *Hora:* {session['hora_user']}\n"
        f"🏷️ *Tarifa:* {session['tardes']}\n\n"
        f"Si todo está bien, escribe *'Sí'* para confirmar tu cita."
    )

    send_whatsapp_message(phone_to_reply, summary)
    session["state"] = "AWAITING_CONFIRMATION"


DAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MONTHS_ES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
             "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

PRESET_HORARIOS = [
    {"hora": "0800"}, {"hora": "0830"}, {"hora": "0900"}, {"hora": "0930"},
    {"hora": "1000"}, {"hora": "1030"}, {"hora": "1100"}, {"hora": "1130"},
    {"hora": "1400"}, {"hora": "1430"}, {"hora": "1500"}, {"hora": "1530"},
    {"hora": "1600"}, {"hora": "1700"},
]


def format_date_es(date_obj):
    return f"{DAYS_ES[date_obj.weekday()]}, {date_obj.day:02d} de {MONTHS_ES[date_obj.month]}"


# Tipos de cita que ARIE no considera "citas" para efectos de consulta/reprogramación.
INFORME_SERVICE_KEYWORDS = ("informe médico general", "informe médico integral", "informe de evaluación")

CONSULT_TOP_N = 10

# Reglas de reprogramación confirmadas en el SP SEL_API_LISTAR_CITAS_PACIENTES_WSP:
# minimo 24h de anticipacion, ventana de 30 dias, y una sola reprogramacion por cita.
RESCHEDULE_POLICY_NOTE = (
    "_ℹ️ Recuerda: cada cita solo puede reprogramarse una vez, con un mínimo de "
    "24 horas de anticipación y dentro de los próximos 30 días._"
)

# Servicio forzado para el flujo "Agendar reevaluación médica" (menú opción 4).
REEVAL_SERVICE_NAME = "medicina fisica y rehabilitacion"

# TODO: confirmar con LOLIMSA si estos son sub-servicios (ListaServiciosWsp) o
# tarifas (ListaTarifarioWsp) dentro de "Medicina Física y Rehabilitación" --
# se asume que son entradas de tarifario, ya que así aparecían nombradas en el
# comprobante de requerimientos (p.ej. "Aplicación TB - 1").
REEVAL_EXCLUDED_TARIFA_KEYWORDS = (
    "neuropediatria",
    "psiquiatria infantil",
    "informe",
    "valoracion espastica",
    "aplicacion tb",
    "post aplicacion tb",
)


def _is_informe_type(cita):
    servicio = normalize_text(cita.get("servicio", ""))
    return any(normalize_text(kw) in servicio for kw in INFORME_SERVICE_KEYWORDS)


def format_appointments_list(citas, title, mode="consult"):
    # ListarCitasPacientesWsp a veces devuelve [{}] (un objeto vacío) en vez de []
    # cuando no hay citas -- se descartan las entradas sin datos reales.
    citas = [c for c in citas if c]

    if mode == "consult":
        # TODO: confirmar con LOLIMSA si el filtro de "Informes" y el tope de
        # Top-10/mes-actual ya se aplican del lado del servidor en
        # ListarCitasPacientesWsp (tipo=C); por ahora se filtra/limita aquí.
        citas = [c for c in citas if not _is_informe_type(c)]
        citas = citas[:CONSULT_TOP_N]

    msg = f"{title}\n\n"
    formatted = []
    today = date.today()
    for i, cita in enumerate(citas, 1):
        fecha_raw = cita.get("fecha", "")
        try:
            # ListarCitasPacientesWsp ha devuelto dos formatos distintos:
            # "2026-07-07 09:00:00.000" y, más recientemente, ISO 8601
            # "2026-07-07T09:00:00.000Z". Se normaliza antes de parsear; la
            # "Z" se descarta (no se convierte de UTC) porque la hora ya
            # viene en hora local de la clínica, igual que el formato anterior.
            fecha_normalizada = fecha_raw.replace("T", " ").rstrip("Z")
            date_obj = datetime.strptime(fecha_normalizada[:19], "%Y-%m-%d %H:%M:%S")
            fecha = format_date_es(date_obj)
            hora = date_obj.strftime("%H:%M")
        except (ValueError, TypeError):
            date_obj = None
            fecha = fecha_raw or "Fecha no disponible"
            hora = "Hora no disponible"

        es_hoy = bool(date_obj) and date_obj.date() == today
        fecha_hora_line = f"🗓️ {fecha} — ⏰ {hora}"
        if es_hoy:
            fecha_hora_line = f"*{fecha_hora_line} (HOY)*"

        if mode == "consult":
            modalidad = {"P": "Presencial", "V": "Teleconsulta"}.get(cita.get("cittip", ""), "")
            estado_pago = cita.get("pagado", "")

            msg += (
                f"*{i}.* {fecha_hora_line}\n"
                f"   🏥 {cita.get('establecimiento', '')}\n"
                f"   🩺 {cita.get('servicio', '')}\n"
                f"   👨‍⚕️ {cita.get('medico', '')}\n"
                f"   🏷️ {cita.get('tardes', '')}"
                + (f" — {modalidad}" if modalidad else "")
                + "\n"
                + (f"   💳 Estado de pago: {estado_pago}\n" if estado_pago else "")
                + "\n"
            )
        else:
            msg += (
                f"*{i}.* {fecha_hora_line}\n"
                f"   🩺 {cita.get('servicio', '')}\n"
                f"   👨‍⚕️ {cita.get('medico', '')}\n\n"
            )
        formatted.append({"id": i, "data": cita})
    return msg, formatted


def show_main_menu(phone_to_reply, session):
    menu = (
        "¿Qué deseas hacer hoy?\n\n"
        "*1.* 📅 Agendar una nueva cita\n"
        "*2.* 🔍 Consultar mis citas\n"
        "*3.* 🔄 Reprogramar una cita\n\n"
        "_Escribe el número de tu elección._"
    )
    session["state"] = "AWAITING_MAIN_MENU"
    send_whatsapp_message(phone_to_reply, menu)


@app.route("/webhook", methods=["POST"])
def webhook_handler():
    data = request.json
    try:
        sender = data["data"]["key"]["remoteJid"].split("@")[0]
        if data["data"]["key"]["fromMe"]:
            return jsonify({"status": "ignored_from_me"}), 200
        msg = data["data"]["message"]
        message_text = (
            msg.get("conversation")
            or msg.get("extendedTextMessage", {}).get("text", "")
        ).strip()
    except (KeyError, TypeError):
        return jsonify({"status": "ignored_format"}), 200

    print(f"Mensaje de {sender}: '{message_text}'")
    session = user_sessions.get(sender, {"state": "START"})
    session["sender"] = sender
    phone_to_reply = sender
    lolcli_headers = {
        "Authorization": f"Basic {LOLCLI_API_TOKEN}",
        "Content-Type": "application/json",
    }

    session["last_interaction_time"] = time.time()
    session["reminder_sent"] = False

    if message_text.lower() in ["salir", "cancelar"]:
        user_sessions.pop(sender, None)
        send_whatsapp_message(
            phone_to_reply,
            "✅ Entendido. Hemos cancelado el proceso. Si deseas agendar una cita luego, aquí estaremos. ¡Que tengas un excelente día! 🌟",
        )
        return jsonify({"status": "cancelled"})

    if message_text.lower() == "retroceder" and session.get("state") != "START":
        history = session.get("history", [])
        if len(history) > 1:
            history.pop()
            previous_state = history[-1] if history else "START"
            session["state"] = previous_state
            replay_state_prompt(previous_state, session, phone_to_reply, lolcli_headers)
            user_sessions[sender] = session
            return jsonify({"status": "reverted"})
        else:
            send_whatsapp_message(
                phone_to_reply,
                "🔄 Ya estás en el primer paso, no hay pasos anteriores. Escribe salir si deseas cancelar o continúa con tu selección. 😊",
            )
            return jsonify({"status": "at_start"})

    state = session.get("state")

    if state == "START":
        session.clear()
        session["history"] = ["START"]
        send_whatsapp_message(
            phone_to_reply,
            "👋 ¡Hola! Bienvenido/a a ARIE. Soy tu asistente virtual y estoy aquí para ayudarte. 😊",
        )
        show_main_menu(phone_to_reply, session)
    elif state == "AWAITING_MAIN_MENU":
        # Technical error only appear if the LOLCLI API is still unreachable when the suer picks an option
        # To allow the list loaded fine at startup, the retry does nothing (instant check)
        if not lista_documentos_global:
            preload_global_lists()
        if not lista_documentos_global:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Lo sentimos, tenemos dificultades técnicas. Por favor, intenta en unos minutos o llámanos directamente. 🙏",
            )
            user_sessions[sender] = session
            return jsonify({"status": "error_loading_lists"})

        choice = message_text.strip().lower()
        if choice in ["1", "agendar", "nueva cita", "nueva"]:
            reply, opts = format_menu(
                "Para empezar, por favor, selecciona tu tipo de documento:",
                lista_documentos_global,
                "tidcod",
                "tiddes",
            )
            session["options"] = opts
            session["state"] = "AWAITING_DOC_TYPE"
            send_whatsapp_message(phone_to_reply, reply)

        elif choice in ["2", "consultar", "mis citas"]:
            reply, opts = format_menu(
                "Para consultar tus citas, selecciona tu tipo de documento:",
                lista_documentos_global,
                "tidcod",
                "tiddes",
            )
            session["options"] = opts
            session["state"] = "AWAITING_DOC_TYPE_FOR_CONSULT"
            send_whatsapp_message(phone_to_reply, reply)

        elif choice in ["3", "reprogramar", "cambiar cita"]:
            session["tidcod"] = "03"
            session["tiddes"] = "D.N.I."
            session["state"] = "AWAITING_DOC_NUMBER_FOR_RESCHEDULE"
            send_whatsapp_message(phone_to_reply, "🔄 Para reprogramar tu cita, ingresa tu número de D.N.I.")

        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ Por favor, escribe *1*, *2* o *3* para elegir una opción. 😊",
            )

    elif state == "AWAITING_DOC_TYPE":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "tiddes"
        )
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            session.setdefault("history", []).append("AWAITING_DOC_TYPE")
            send_whatsapp_message(
                phone_to_reply,
                f"Entendido. Ahora, por favor, ingresa tu número de {selected_option['tiddes']}.",
            )
            session["state"] = "AWAITING_DOC_NUMBER"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección de la lista. 🙏",
            )

    elif state == "AWAITING_DOC_NUMBER":
        doc_number = message_text.strip()
        tidcod = session.get("tidcod")

        if tidcod == "03" and (not doc_number.isdigit() or len(doc_number) != 8):
            send_whatsapp_message(
                phone_to_reply,
                "⚠️ El DNI ingresado no es válido. Debe tener exactamente 8 dígitos numéricos. ¿Puedes verificarlo e intentarlo de nuevo? 🙏",
            )
            user_sessions[sender] = session
            return jsonify({"status": "invalid_dni"})

        session["pacdoc"] = doc_number
        session.setdefault("history", []).append("AWAITING_DOC_NUMBER")

        try:
            payload = {"tidcod": tidcod, "pacdoc": doc_number}
            response = requests.post(
                f"{LOLCLI_API_URL}/ValidarPacienteWsp",
                json=payload,
                headers=lolcli_headers,
            )
            pacientes = response.json().get("paciente", [])

            if pacientes and pacientes[0].get("valido") == "S":
                paciente = pacientes[0]
                session.update(
                    {
                        "pachis": paciente["pachis"],
                        "paciente_nombre": paciente["pacpmn"],
                    }
                )
                send_whatsapp_message(
                    phone_to_reply, f"¡Hola de nuevo, {paciente['pacpmn']}!"
                )
                continue_appointment_flow(session, phone_to_reply, lolcli_headers)
            elif pacientes:
                # Paciente registrado (ValidarPacienteWsp lo encontró), pero
                # "valido": "N" -> no tiene citas atendidas en los últimos 10
                # días, por lo que no puede agendar por este medio. Al estar
                # registrado, se le saluda por su nombre en la respuesta.
                paciente = pacientes[0]
                send_whatsapp_message(
                    phone_to_reply,
                    f"🔍 Hola {paciente['pacpmn']}, encontramos tu registro, pero no cuentas con citas "
                    "atendidas en los últimos 10 días, por lo que no es posible agendar una nueva cita "
                    "por este medio. Por favor, acércate personalmente a tu sede. 📞",
                )
                user_sessions.pop(sender, None)
            else:
                # ARIE no permite crear pacientes nuevos por WhatsApp: si no está
                # registrado en la clínica, se le pide acercarse presencialmente.
                send_whatsapp_message(
                    phone_to_reply,
                    "🔍 No encontramos ningún paciente registrado con ese documento. "
                    "Para agendar una cita, por favor acércate personalmente a tu sede para registrarte. 📞",
                )
                user_sessions.pop(sender, None)
        except Exception as e:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Tuvimos un inconveniente al verificar tu documento. Por favor, intenta de nuevo. 🙏",
            )
            print(f"Error en AWAITING_DOC_NUMBER: {e}")

    # ── REEVALUACIÓN MÉDICA FLOW ─────────────────────────────────────────────
    # Agendamiento de cita de reevaluación (Medicina Física y Rehabilitación),
    # con cobro previo. Reutiliza los mismos estados de sede/médico/fecha/hora
    # que el flujo genérico (AWAITING_ESTABLISHMENT en adelante se ramifica
    # por session["flow"] == "reeval").

    elif state == "AWAITING_DOC_TYPE_FOR_REEVAL":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "tiddes"
        )
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            session.setdefault("history", []).append("AWAITING_DOC_TYPE_FOR_REEVAL")
            send_whatsapp_message(
                phone_to_reply,
                f"Entendido. Ahora, por favor, ingresa tu número de {selected_option['tiddes']}.",
            )
            session["state"] = "AWAITING_DOC_NUMBER_FOR_REEVAL"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección de la lista. 🙏",
            )

    elif state == "AWAITING_DOC_NUMBER_FOR_REEVAL":
        doc_number = message_text.strip()
        tidcod = session.get("tidcod")

        if tidcod == "03" and (not doc_number.isdigit() or len(doc_number) != 8):
            send_whatsapp_message(
                phone_to_reply,
                "⚠️ El DNI ingresado no es válido. Debe tener exactamente 8 dígitos numéricos. ¿Puedes verificarlo e intentarlo de nuevo? 🙏",
            )
            user_sessions[sender] = session
            return jsonify({"status": "invalid_dni"})

        session["pacdoc"] = doc_number
        session.setdefault("history", []).append("AWAITING_DOC_NUMBER_FOR_REEVAL")

        try:
            response = requests.post(
                f"{LOLCLI_API_URL}/ValidarPacienteWsp",
                json={"tidcod": tidcod, "pacdoc": doc_number},
                headers=lolcli_headers,
            )
            pacientes = response.json().get("paciente", [])

            if pacientes and pacientes[0].get("valido") == "S":
                paciente = pacientes[0]
                session.update(
                    {
                        "pachis": paciente["pachis"],
                        "paciente_nombre": paciente["pacpmn"],
                    }
                )
                send_whatsapp_message(
                    phone_to_reply, f"¡Hola de nuevo, {paciente['pacpmn']}!"
                )
                continue_appointment_flow(session, phone_to_reply, lolcli_headers)
            elif pacientes:
                paciente = pacientes[0]
                send_whatsapp_message(
                    phone_to_reply,
                    f"🔍 Hola {paciente['pacpmn']}, encontramos tu registro, pero no cuentas con citas "
                    "atendidas en los últimos 10 días, por lo que no es posible agendar una nueva cita "
                    "por este medio. Por favor, acércate personalmente a tu sede. 📞",
                )
                user_sessions.pop(sender, None)
            else:
                send_whatsapp_message(
                    phone_to_reply,
                    "🔍 No encontramos ningún paciente registrado con ese documento. "
                    "Para agendar una cita, por favor acércate personalmente a tu sede para registrarte. 📞",
                )
                user_sessions.pop(sender, None)
        except Exception as e:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Tuvimos un inconveniente al verificar tu documento. Por favor, intenta de nuevo. 🙏",
            )
            print(f"Error en AWAITING_DOC_NUMBER_FOR_REEVAL: {e}")

    elif state == "AWAITING_ESTABLISHMENT_CLARIFICATION":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "sisent"
        )
        if selected_option:
            session.setdefault("history", []).append("AWAITING_ESTABLISHMENT")
            session["siscod"] = selected_option["siscod"]
            session["establishment_name"] = selected_option["sisent"]
            response = requests.post(
                f"{LOLCLI_API_URL}/ListaServiciosWsp",
                json={"siscod": session["siscod"]},
                headers=lolcli_headers,
            )
            servicios = response.json().get("servicios", [])
            present_specialty_or_force_reeval(
                session, phone_to_reply, lolcli_headers, servicios,
                f"¡Perfecto! Ahora, para la sede *{session['establishment_name']}*, ¿qué especialidad necesitas?",
            )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección de la lista. 🙏",
            )

    elif state == "AWAITING_ESTABLISHMENT":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "sisent"
        )
        if selected_option:
            session.setdefault("history", []).append("AWAITING_ESTABLISHMENT")
            session["siscod"] = selected_option["siscod"]
            session["establishment_name"] = selected_option["sisent"]
            response = requests.post(
                f"{LOLCLI_API_URL}/ListaServiciosWsp",
                json={"siscod": session["siscod"]},
                headers=lolcli_headers,
            )
            servicios = response.json().get("servicios", [])
            present_specialty_or_force_reeval(
                session, phone_to_reply, lolcli_headers, servicios,
                f"Entendido. Ahora, ¿para qué especialidad en *{session['establishment_name']}* necesitas la cita?",
            )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa sede. Por favor, escribe el número de la sede que prefieres. 🏥",
            )

    elif state == "AWAITING_SPECIALTY":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "serdes"
        )
        if selected_option:
            session.setdefault("history", []).append("AWAITING_SPECIALTY")
            session["sercod"] = selected_option["sercod"]
            session["sernam"] = selected_option["serdes"]
            fetch_and_prompt_doctors(session, phone_to_reply, lolcli_headers)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa especialidad. Por favor, escribe el número de la especialidad que necesitas. 🩺",
            )

    elif state == "AWAITING_DOCTOR":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "mednam"
        )
        if selected_option:
            session.setdefault("history", []).append("AWAITING_DOCTOR")
            session["medcod"] = selected_option["medcod"]
            session["mednam"] = selected_option["mednam"]
            send_whatsapp_message(
                phone_to_reply,
                f"Perfecto, con el Dr(a). {session['mednam']}. Veamos sus fechas...",
            )
            today_str = date.today().strftime("%Y%m%d")
            payload = {
                "siscod": session["siscod"],
                "sercod": session["sercod"],
                "medcod": session["medcod"],
                "fecha": today_str,
            }
            response = requests.post(
                f"{LOLCLI_API_URL}/ListaCuposDisponibles",
                json=payload,
                headers=lolcli_headers,
            )
            all_cupos = response.json().get("cupos", [])
            session["all_cupos"] = all_cupos
            seen = set()
            unique_fechas = []
            for c in all_cupos:
                d = c.get("citdat", "")
                if d and d not in seen:
                    seen.add(d)
                    unique_fechas.append(c)
            reply, formatted_options = format_menu(
                "📅 Estas son sus próximas fechas disponibles:",
                unique_fechas,
                "citdat",
                "citdat",
            )
            session["options"] = formatted_options
            session["state"] = "AWAITING_AVAILABLE_DATE"
            send_whatsapp_message(phone_to_reply, reply)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No encontré ese médico. Por favor, escribe el número del médico de tu preferencia. 👨‍⚕️",
            )

    elif state == "AWAITING_AVAILABLE_DATE":
        selected_option = process_user_choice(message_text, session.get("options", []))
        if selected_option:
            session.setdefault("history", []).append("AWAITING_AVAILABLE_DATE")
            session["fecha_api"] = selected_option["citdat"]
            date_obj = datetime.strptime(selected_option["citdat"], "%Y%m%d")
            session["fecha_user"] = format_date_es(date_obj)
            send_whatsapp_message(
                phone_to_reply,
                f"Excelente, para el *{session['fecha_user']}*. Viendo las horas libres...",
            )
            try:
                horarios_raw = requests.post(
                    f"{LOLCLI_API_URL}/ListaCuposDetalle",
                    json={
                        "siscod": int(session["siscod"]),
                        "sercod": session["sercod"],
                        "medcod": session["medcod"],
                        "fecha": session["fecha_api"],
                        "invnum": 0,
                    },
                    headers=lolcli_headers,
                ).json().get("horarios", [])
                horarios = [h for h in horarios_raw if h.get("estado") == "D"] or PRESET_HORARIOS
            except Exception as e:
                print(f"ERROR ListaCuposDetalle (schedule): {e}")
                horarios = PRESET_HORARIOS
            reply = "⏰ Elige el horario de tu preferencia:\n\n"
            formatted_options = []
            for i, h in enumerate(horarios, 1):
                hora_fmt = datetime.strptime(h["hora"], "%H%M").strftime("%H:%M")
                reply += f"*{i}.* {hora_fmt}\n"
                formatted_options.append({"id": i, "data": h})
            reply += "\n_Elige la hora (solo el número). ¡Ya casi terminamos!_"
            session["options"] = formatted_options
            session["state"] = "AWAITING_TIME"
            send_whatsapp_message(phone_to_reply, reply)
        else:
            send_whatsapp_message(
                phone_to_reply, "No reconocí esa fecha. Elige una de la lista."
            )

    elif state == "AWAITING_TIME":
        try:
            choice = int(message_text) - 1
            selected_option = session["options"][choice]["data"]
            session.setdefault("history", []).append("AWAITING_TIME")
            session["hora_api"] = selected_option["hora"]
            time_obj = datetime.strptime(selected_option["hora"], "%H%M")
            session["hora_user"] = time_obj.strftime("%H:%M")
            reply = "¡Anotado! Para finalizar, ¿la cita será *Presencial* (1) o *Virtual* (2)?"
            send_whatsapp_message(phone_to_reply, reply)
            session["state"] = "AWAITING_APPOINTMENT_TYPE"
        except (ValueError, IndexError):
            send_whatsapp_message(
                phone_to_reply,
                "⏰ Por favor, escribe solo el número del horario que prefieres de la lista.",
            )

    elif state == "AWAITING_APPOINTMENT_TYPE":
        choice = message_text.lower()
        if choice in ["1", "presencial"]:
            session.setdefault("history", []).append("AWAITING_APPOINTMENT_TYPE")
            session["cittip"], session["cittip_name"] = "P", "Presencial"
        elif choice in ["2", "virtual"]:
            session.setdefault("history", []).append("AWAITING_APPOINTMENT_TYPE")
            session["cittip"], session["cittip_name"] = "V", "Virtual"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ Por favor, escribe 1 para Presencial 🏥 o 2 para Virtual 💻.",
            )
            user_sessions[sender] = session
            return jsonify({"status": "processed"})

        if session.get("cittip"):
            send_whatsapp_message(phone_to_reply, "Buscando tarifas, un momento... 🔍")
            payload = {
                "siscod": int(session["siscod"]),
                "sercod": session["sercod"],
                "medcod": session["medcod"],
                "cittip": session["cittip"],
            }
            response = requests.post(
                f"{LOLCLI_API_URL}/ListaTarifarioWsp", json=payload, headers=lolcli_headers
            )
            try:
                response.raise_for_status()
                tarifas = response.json().get("tarifas", [])
            except (
                requests.exceptions.HTTPError,
                requests.exceptions.JSONDecodeError,
            ) as e:
                print(
                    f"ERROR: La API de tarifas ({response.url}) falló. Status: {response.status_code}, Error: {e}"
                )
                tarifas = []

            if session.get("flow") == "reeval":
                # TODO: confirmar con LOLIMSA el mecanismo real de categoría social
                # -- ListaTarifarioWsp no acepta ningún parámetro de paciente/categoría.
                tarifas = [
                    t for t in tarifas
                    if not any(kw in normalize_text(t.get("tardes", "")) for kw in REEVAL_EXCLUDED_TARIFA_KEYWORDS)
                ]

            if not tarifas:
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 No encontramos tarifas para este tipo de consulta. Escribe retroceder para intentar con otra modalidad. 🙏",
                )
                session["history"].pop()
            else:
                reply, formatted_options = format_menu(
                    "Estas son las tarifas disponibles:", tarifas, "tarcod", "tardes"
                )
                session["options"] = formatted_options
                session["state"] = "AWAITING_TARIFF"
                send_whatsapp_message(phone_to_reply, reply)

    elif state == "AWAITING_TARIFF":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "tardes"
        )
        if selected_option:
            session.setdefault("history", []).append("AWAITING_TARIFF")
            session["tarcod"] = selected_option["tarcod"]
            session["tardes"] = selected_option["tardes"]
            send_whatsapp_message(phone_to_reply, f"Ok, elegiste *'{session['tardes']}'*.")
            show_final_summary(session, phone_to_reply)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa tarifa. Por favor, escribe el número de la tarifa que deseas. 🙏",
            )

    elif state == "AWAITING_CONFIRMATION":
        if message_text.lower() in ["sí", "si"]:
            try:
                send_whatsapp_message(
                    phone_to_reply,
                    "¡Excelente! Registrando tu cita, un momento por favor...",
                )
                fecref_str = datetime.strptime(
                    session["fecha_api"] + session["hora_api"], "%Y%m%d%H%M"
                ).strftime("%d-%m-%Y %H:%M")

                payload_cita = {
                    "siscod": int(session["siscod"]),
                    "medcod": session["medcod"],
                    "sercod": session["sercod"],
                    "fecref": fecref_str,
                    "pachis": session["pachis"],
                    "cittip": session["cittip"],
                    "tarcod": session["tarcod"],
                    "totnet": 0.0,
                    "totimp": 0.0,
                    "seccit": 0,
                    # TODO: confirmar con LOLIMSA si prgori/plnnum son constantes
                    # fijas de LOLCLI o códigos específicos del tenant anterior
                    # que deben cambiar para ARIE (no se cambia a ciegas porque
                    # un valor incorrecto podría romper el registro de la cita).
                    "prgori": "QU",
                    "plnnum": "161003",
                }

                response = requests.post(
                    f"{LOLCLI_API_URL}/RegistroCita",
                    json=payload_cita,
                    headers=lolcli_headers,
                )
                response_data = response.json()

                if response_data.get("status") == "success":
                    session["invnum_cita"] = response_data.get("invnum")
                    session["prfnum_cita"] = response_data.get("prfnum")

                    costo_final = 0.0
                    if session["prfnum_cita"]:
                        time.sleep(2)
                        payload_pagos = {"pachis": session["pachis"]}
                        response_pagos = requests.post(
                            f"{LOLCLI_API_URL}/ListaPagosPendientes",
                            json=payload_pagos,
                            headers=lolcli_headers,
                        )
                        if response_pagos.ok:
                            for pago in response_pagos.json().get("pendientes", []):
                                if str(pago.get("prfnum")) == str(
                                    session["prfnum_cita"]
                                ):
                                    costo_final = float(pago.get("prfppac", 0.0))
                                    break

                    session["costo_total"] = costo_final
                    send_whatsapp_message(
                        phone_to_reply,
                        f"¡Tu cita ha sido agendada con la reserva *{session['invnum_cita']}*! 🎉\nAhora, estoy generando tu enlace de pago por *S/ {costo_final:.2f}*.",
                    )

                    generate_payment_link_and_send(
                        session, phone_to_reply, lolcli_headers
                    )
                else:
                    error_msg = response_data.get("message", "un error del sistema.")
                    send_whatsapp_message(
                        phone_to_reply,
                        f"No se pudo registrar la cita: {error_msg}. Escribe 'salir' y vuelve a intentarlo.",
                    )
                    user_sessions.pop(sender, None)
            except Exception as e:
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Lo sentimos, ocurrió un error al registrar tu cita. Por favor, intenta de nuevo o llámanos directamente. 🙏",
                )
                print(f"Error en AWAITING_CONFIRMATION (RegistroCita): {e}")
                user_sessions.pop(sender, None)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "🤔 Sin problema. Escribe retroceder si deseas corregir algún dato, o salir si prefieres cancelar. 😊",
            )

    elif state == "AWAITING_PAYMENT_CONFIRMATION":
        payment_id_prefix = "¡ya he completado mi pago!, el id de pago es:"
        message_lower = message_text.lower()
        token_to_check = None

        if message_lower.startswith(payment_id_prefix):
            token_to_check = message_text[len(payment_id_prefix) :].strip()
        elif message_lower in ["listo", "pagado", "ya pagué", "ya pague"]:
            token_to_check = session.get("payment_token")
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Para confirmar tu cita, por favor envíanos el mensaje completo de confirmación que recibiste al pagar (debe incluir el ID de pago). 📋",
            )
            user_sessions[sender] = session
            return jsonify({"status": "awaiting_proper_confirmation"})

        if token_to_check:
            try:
                send_whatsapp_message(
                    phone_to_reply,
                    "✅ Recibido. Estamos verificando el estado de tu pago, un momento por favor... 🔍",
                )
                payload_consulta = {"token": token_to_check}
                url_consulta = f"{LOLCLI_API_URL}/ConsultarLinkPago"
                response_consulta = requests.post(
                    url_consulta, json=payload_consulta, headers=lolcli_headers
                )

                if response_consulta.status_code == 404:
                    print(f"ERROR 404: El endpoint '{url_consulta}' no fue encontrado.")
                    send_whatsapp_message(
                        phone_to_reply,
                        "😔 No pudimos verificar tu pago. Por favor, contacta a nuestro equipo de soporte técnico. 🙏",
                    )
                    return jsonify({"status": "error_404_consulting_payment"})

                response_consulta.raise_for_status()
                data_consulta = response_consulta.json()
                payment_data = data_consulta.get("data", {})

                if (
                    data_consulta.get("status") == "success"
                    and payment_data.get("estado_pago") == "COMPLETADO"
                ):
                    send_whatsapp_message(
                        phone_to_reply,
                        f"¡Pago confirmado! ✅\n\nTu cita está 100% confirmada.\n\n¡Gracias por preferir ARIE! Te esperamos.",
                    )
                    save_reminder(session)
                    send_whatsapp_message(
                        phone_to_reply,
                        "Gracias. Escribe *'continuar'* si deseas realizar otra consulta o *'salir'* para terminar la sesión. 😊",
                    )
                    session["state"] = "AWAITING_POST_FLOW"
                    user_sessions[sender] = session
                    return jsonify({"status": "completed_and_session_cleared"})
                else:
                    current_status = payment_data.get("estado_pago", "desconocido")
                    print(
                        f"El estado del pago aún no es 'COMPLETADO'. Estado actual: {current_status}"
                    )
                    send_whatsapp_message(
                        phone_to_reply,
                        "⏳ Aún no podemos confirmar tu pago. Asegúrate de haber completado la transacción y envíanos el mensaje de confirmación en unos minutos. 🙏",
                    )

            except Exception as e:
                print(f"ERROR Inesperado al consultar pago: {e}")
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Ocurrió un error al verificar tu pago. Por favor, intenta nuevamente o contáctanos. 🙏",
                )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No encontramos un pago pendiente. Por favor, envíanos el mensaje completo de confirmación que recibiste al pagar. 📋",
            )

    # ── CONSULT FLOW ─────────────────────────────────────────────────────────

    elif state == "AWAITING_DOC_TYPE_FOR_CONSULT":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "tiddes"
        )
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            send_whatsapp_message(
                phone_to_reply, f"Ingresa tu número de {selected_option['tiddes']}."
            )
            session["state"] = "AWAITING_DOC_NUMBER_FOR_CONSULT"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección. 🙏",
            )

    elif state == "AWAITING_DOC_NUMBER_FOR_CONSULT":
        doc_number = message_text.strip()
        tidcod = session.get("tidcod")
        if tidcod == "03" and (not doc_number.isdigit() or len(doc_number) != 8):
            send_whatsapp_message(
                phone_to_reply, "⚠️ El DNI debe tener exactamente 8 dígitos numéricos."
            )
            user_sessions[sender] = session
            return jsonify({"status": "invalid_dni"})
        try:
            pacientes = (
                requests.post(
                    f"{LOLCLI_API_URL}/ValidarPacienteWsp",
                    json={"tidcod": tidcod, "pacdoc": doc_number},
                    headers=lolcli_headers,
                )
                .json()
                .get("paciente", [])
            )
            if not pacientes:
                send_whatsapp_message(
                    phone_to_reply,
                    "🔍 No encontramos ningún paciente registrado con ese documento. 🙏",
                )
                user_sessions.pop(sender, None)
                return jsonify({"status": "patient_not_found"})
            paciente = pacientes[0]
            send_whatsapp_message(
                phone_to_reply,
                f"Un momento, consultando tus citas, {paciente['pacpmn']}... 🔍",
            )
            response_citas = requests.post(
                f"{LOLCLI_API_URL}/ListarCitasPacientesWsp",
                json={"nro_documento": doc_number, "tipo": "C"},
                headers=lolcli_headers,
            )
            data_citas = response_citas.json()
            print(f"INFO: ListarCitasPacientesWsp (consult, doc={doc_number}) respuesta: {data_citas}")
            server_error = response_citas.status_code >= 500 or data_citas.get("code") == 500
            citas = [c for c in data_citas.get("citas", []) if c] if not server_error else []

            if server_error:
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Tuvimos un problema técnico al consultar tus citas. Por favor, intenta de nuevo en unos minutos o contáctanos directamente. 🙏",
                )
            elif not citas:
                send_whatsapp_message(
                    phone_to_reply, "📋 No tienes citas agendadas en este momento. 😊"
                )
            else:
                msg, _ = format_appointments_list(
                    citas, f"📋 *Tus citas agendadas, {paciente['pacpmn']}:*", mode="consult"
                )
                msg += "_ℹ️ Para reprogramar una cita, selecciona la opción *3* en el menú principal._"
                send_whatsapp_message(phone_to_reply, msg)
            send_whatsapp_message(
                phone_to_reply,
                "Gracias. Escribe *'continuar'* si deseas realizar otra consulta o *'salir'* para terminar la sesión. 😊",
            )
            session["state"] = "AWAITING_POST_FLOW"
            user_sessions[sender] = session
            return jsonify({"status": "consult_done"})
        except Exception as e:
            print(f"ERROR en AWAITING_DOC_NUMBER_FOR_CONSULT: {e}")
            send_whatsapp_message(
                phone_to_reply,
                "😔 Ocurrió un error al consultar tus citas. Por favor, intenta de nuevo. 🙏",
            )

    # ── RESCHEDULE FLOW ──────────────────────────────────────────────────────

    elif state == "AWAITING_DOC_TYPE_FOR_RESCHEDULE":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "tiddes"
        )
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            send_whatsapp_message(
                phone_to_reply, f"Ingresa tu número de {selected_option['tiddes']}."
            )
            session["state"] = "AWAITING_DOC_NUMBER_FOR_RESCHEDULE"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección. 🙏",
            )

    elif state == "AWAITING_DOC_NUMBER_FOR_RESCHEDULE":
        doc_number = message_text.strip()
        tidcod = session.get("tidcod")
        if tidcod == "03" and (not doc_number.isdigit() or len(doc_number) != 8):
            send_whatsapp_message(
                phone_to_reply, "⚠️ El DNI debe tener exactamente 8 dígitos numéricos."
            )
            user_sessions[sender] = session
            return jsonify({"status": "invalid_dni"})
        try:
            send_whatsapp_message(
                phone_to_reply, "Un momento, buscando tus citas... 🔍"
            )

            # ListarCitasPacientesWsp no devuelve "pachis" (código interno del
            # paciente), pero se necesita para el pago del derecho de
            # reprogramación (GenerarLinkPagoOrdenPrefactura). Se consulta
            # ValidarPacienteWsp solo para obtenerlo -- su campo "valido"
            # (regla de 10 días para citas nuevas) no aplica aquí, así que no
            # se usa para bloquear el acceso a reprogramar.
            try:
                response_paciente = requests.post(
                    f"{LOLCLI_API_URL}/ValidarPacienteWsp",
                    json={"tidcod": tidcod, "pacdoc": doc_number},
                    headers=lolcli_headers,
                )
                pacientes = response_paciente.json().get("paciente", [])
                if pacientes:
                    session["pachis"] = pacientes[0].get("pachis")
            except Exception as e:
                print(f"ERROR ValidarPacienteWsp (reschedule, pachis lookup): {e}")

            response_citas = requests.post(
                f"{LOLCLI_API_URL}/ListarCitasPacientesWsp",
                json={"nro_documento": doc_number, "tipo": "R"},
                headers=lolcli_headers,
            )
            data_citas = response_citas.json()
            print(f"INFO: ListarCitasPacientesWsp (reschedule, doc={doc_number}) respuesta: {data_citas}")
            server_error = response_citas.status_code >= 500 or data_citas.get("code") == 500
            citas = [c for c in data_citas.get("citas", []) if c] if not server_error else []

            if server_error:
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Tuvimos un problema técnico al buscar tus citas. Por favor, intenta de nuevo en unos minutos o contáctanos directamente. 🙏",
                )
                user_sessions.pop(sender, None)
                return jsonify({"status": "server_error"})
            if not citas:
                # La lista de reprogramables vino vacía. Antes de asumir que el
                # paciente no tiene ninguna cita, se consulta tipo=C para
                # distinguir "no tiene ninguna cita" de "tiene citas, pero ya
                # ninguna es reprogramable" (ya reprogramada una vez o fuera de
                # la ventana de 24h/30 días).
                response_todas = requests.post(
                    f"{LOLCLI_API_URL}/ListarCitasPacientesWsp",
                    json={"nro_documento": doc_number, "tipo": "C"},
                    headers=lolcli_headers,
                )
                data_todas = response_todas.json()
                todas_server_error = response_todas.status_code >= 500 or data_todas.get("code") == 500
                citas_todas = [c for c in data_todas.get("citas", []) if c] if not todas_server_error else []

                if citas_todas:
                    send_whatsapp_message(
                        phone_to_reply,
                        "📋 Tienes citas agendadas, pero ninguna puede reprogramarse en este momento. 😊\n\n"
                        + RESCHEDULE_POLICY_NOTE
                        + "\n\nSi ya reprogramaste una cita antes, o está fuera de ese rango, ya no se puede volver a reprogramar.",
                    )
                else:
                    send_whatsapp_message(
                        phone_to_reply, "📋 No tienes ninguna cita agendada en este momento. 😊"
                    )
                user_sessions.pop(sender, None)
                return jsonify({"status": "no_appointments"})
            msg, formatted = format_appointments_list(
                citas, "¿Cuál cita deseas reprogramar?", mode="reschedule"
            )
            msg += RESCHEDULE_POLICY_NOTE
            session["options"] = formatted
            session["state"] = "AWAITING_APPOINTMENT_TO_RESCHEDULE"
            send_whatsapp_message(phone_to_reply, msg)
        except Exception as e:
            print(f"ERROR en AWAITING_DOC_NUMBER_FOR_RESCHEDULE: {e}")
            send_whatsapp_message(
                phone_to_reply,
                "😔 Ocurrió un error al buscar tus citas. Por favor, intenta de nuevo. 🙏",
            )

    elif state == "AWAITING_APPOINTMENT_TO_RESCHEDULE":
        # NOTA: la bifurcación por tipo de cita (reevaluación médica: mismo
        # médico/sede, ventana de 4 semanas, S/.15 -- vs. terapia: mismo
        # terapeuta/sede, ventana de 30 días, motivo "falta del niño", lista de
        # exclusión) NO está implementada todavía: requiere confirmar con
        # LOLIMSA qué campo de la fila de ListarCitasPacientesWsp distingue
        # ambos tipos, y probarla contra una cita real (no disponible aún en
        # el sandbox). Por ahora el flujo de reprogramación sigue siendo único
        # y confía en que ReagendarCitaWsp rechace del lado del servidor los
        # casos fuera de regla (ver bloque de confirmación más abajo).
        selected_option = process_user_choice(message_text, session.get("options", []))
        if selected_option:
            session["citid_to_reschedule"] = selected_option.get("secuencia")
            # TODO: confirmar el nombre real del campo de sede/siscod en una fila
            # de ListarCitasPacientesWsp -- LOLCLI_ENTIDAD es un código de
            # entidad, no de sede, y usarlo como siscod es probablemente
            # incorrecto salvo que coincidan por casualidad.
            session["siscod"] = selected_option.get("siscod", os.getenv("LOLCLI_ENTIDAD", "000000001"))
            session["sercod"] = selected_option.get("sercod")
            session["medcod"] = selected_option.get("medcod")
            session["mednam"] = selected_option.get("medico", "")
            session["sernam"] = selected_option.get("servicio", "")
            session["establishment_name"] = selected_option.get("sede", selected_option.get("establecimiento", ""))
            session["cittip"] = selected_option.get("cittip", "P")
            session["tarcod"] = selected_option.get("tarcod", "")
            send_whatsapp_message(
                phone_to_reply,
                f"Buscando fechas disponibles para el Dr(a). {session['mednam']}... 📅",
            )
            today_str = date.today().strftime("%Y%m%d")
            all_cupos = (
                requests.post(
                    f"{LOLCLI_API_URL}/ListaCuposDisponibles",
                    json={
                        "siscod": session["siscod"],
                        "sercod": session["sercod"],
                        "medcod": session["medcod"],
                        "fecha": today_str,
                    },
                    headers=lolcli_headers,
                )
                .json()
                .get("cupos", [])
            )
            if not all_cupos:
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 No hay fechas disponibles para ese médico en este momento. Escribe *salir* para cancelar.",
                )
            else:
                # Store all cupos so we can filter by date when user picks one
                session["all_cupos"] = all_cupos
                # Show only unique dates
                seen = set()
                unique_fechas = []
                for c in all_cupos:
                    d = c.get("citdat", "")
                    if d and d not in seen:
                        seen.add(d)
                        unique_fechas.append(c)
                reply, opts = format_menu(
                    "📅 Elige la nueva fecha:", unique_fechas, "citdat", "citdat"
                )
                session["options"] = opts
                session["state"] = "AWAITING_NEW_DATE_RESCHEDULE"
                send_whatsapp_message(phone_to_reply, reply)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de la cita que deseas reprogramar.",
            )

    elif state == "AWAITING_NEW_DATE_RESCHEDULE":
        selected_option = process_user_choice(message_text, session.get("options", []))
        if selected_option:
            session["new_fecha_api"] = selected_option["citdat"]
            date_obj = datetime.strptime(selected_option["citdat"], "%Y%m%d")
            session["new_fecha_user"] = format_date_es(date_obj)
            send_whatsapp_message(
                phone_to_reply,
                f"Perfecto, para el *{session['new_fecha_user']}*. Viendo horarios disponibles... ⏰",
            )
            try:
                horarios_raw = requests.post(
                    f"{LOLCLI_API_URL}/ListaCuposDetalle",
                    json={
                        "siscod": int(session["siscod"]),
                        "sercod": session["sercod"],
                        "medcod": session["medcod"],
                        "fecha": session["new_fecha_api"],
                        "invnum": int(session["citid_to_reschedule"]),
                    },
                    headers=lolcli_headers,
                ).json().get("horarios", [])
                horarios = [h for h in horarios_raw if h.get("estado") == "D"] or PRESET_HORARIOS
            except Exception as e:
                print(f"ERROR ListaCuposDetalle (reschedule): {e}")
                horarios = PRESET_HORARIOS
            reply = "⏰ Elige el nuevo horario de tu preferencia:\n\n"
            opts = []
            for i, h in enumerate(horarios, 1):
                hora_fmt = datetime.strptime(h["hora"], "%H%M").strftime("%H:%M")
                reply += f"*{i}.* {hora_fmt}\n"
                opts.append({"id": i, "data": h})
            reply += "\n_Elige el número del horario._"
            session["options"] = opts
            session["state"] = "AWAITING_NEW_TIME_RESCHEDULE"
            send_whatsapp_message(phone_to_reply, reply)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa fecha. Por favor, elige el número de la lista.",
            )

    elif state == "AWAITING_NEW_TIME_RESCHEDULE":
        try:
            choice = int(message_text) - 1
            selected = session["options"][choice]["data"]
            session["new_hora_api"] = selected["hora"]
            session["new_hora_user"] = datetime.strptime(
                selected["hora"], "%H%M"
            ).strftime("%H:%M")
            summary = (
                f"🔄 *Confirmación de reprogramación:*\n\n"
                f"🩺 *Especialidad:* {session['sernam']}\n"
                f"👨‍⚕️ *Médico:* {session['mednam']}\n"
                f"🗓️ *Nueva fecha:* {session['new_fecha_user']}\n"
                f"⏰ *Nueva hora:* {session['new_hora_user']}\n\n"
                f"¿Confirmas el cambio? Escribe *'Sí'* para confirmar o *'salir'* para cancelar."
            )
            session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"
            send_whatsapp_message(phone_to_reply, summary)
        except (ValueError, IndexError):
            send_whatsapp_message(
                phone_to_reply,
                "⏰ Por favor, escribe solo el número del horario de la lista.",
            )

    elif state == "AWAITING_RESCHEDULE_CONFIRMATION":
        if message_text.lower() in ["sí", "si"]:
            try:
                fecref_str = datetime.strptime(
                    session["new_fecha_api"] + session["new_hora_api"], "%Y%m%d%H%M"
                ).strftime("%d-%m-%Y %H:%M")

                payload_actualizar = {
                    "xxinvnum": int(session["citid_to_reschedule"]),
                    "xxmedcod": session["medcod"],
                    "xxsercod": session["sercod"],
                    "xxfecref": fecref_str,
                    "xxcittip": session.get("cittip", "P"),
                    "usecod": 1,
                    # TODO: confirmar con LOLIMSA si usenam/usecod identifican
                    # al usuario/sistema que ejecuta la acción (no cambiar sin
                    # confirmar) o si es texto de marca visible al paciente
                    # (en cuyo caso debería decir "ARIE").
                    "usenam": "LOLIMSA",
                }
                # TODO: si la cita reprogramada es de terapia (recuperación), ARIE
                # requiere marcarla con un valor de "tipo de citado" -- el propio
                # requerimiento del cliente lo deja como "por consultar", así que
                # no se puede completar este campo todavía. Cuando LOLIMSA lo
                # confirme, agregar aquí p.ej. payload_actualizar["xxtipcit"] = "R".
                if session.get("cittip") == "V" and session.get("zoom_link"):
                    payload_actualizar["xxcitzoomlink"] = session["zoom_link"]

                # El cambio de cita solo se ejecuta (ReagendarCitaWsp) una vez
                # confirmado el pago del derecho de reprogramación -- ver
                # AWAITING_RESCHEDULE_PAYMENT_CONFIRMATION más abajo.
                session["reschedule_payload"] = payload_actualizar
                send_whatsapp_message(
                    phone_to_reply,
                    "Antes de confirmar el cambio, es necesario abonar el derecho de reprogramación de citas. "
                    "Generando tu enlace de pago... 💳",
                )
                generate_reschedule_payment_link_and_send(session, phone_to_reply, lolcli_headers)
            except Exception as e:
                print(f"ERROR en AWAITING_RESCHEDULE_CONFIRMATION: {e}")
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Ocurrió un error al preparar tu reprogramación. Por favor, intenta de nuevo o contáctanos. 🙏",
                )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Entendido. Escribe *'salir'* si deseas cancelar o continúa eligiendo. 😊",
            )

    elif state == "AWAITING_RESCHEDULE_PAYMENT_CONFIRMATION":
        payment_id_prefix = "¡ya he completado mi pago!, el id de pago es:"
        message_lower = message_text.lower()
        token_to_check = None

        if message_lower.startswith(payment_id_prefix):
            token_to_check = message_text[len(payment_id_prefix):].strip()
        elif message_lower in ["listo", "pagado", "ya pagué", "ya pague"]:
            token_to_check = session.get("reschedule_payment_token")
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Para confirmar tu reprogramación, por favor envíanos el mensaje completo de confirmación que "
                "recibiste al pagar (debe incluir el ID de pago), o escribe *'listo'* si ya completaste el pago. 📋",
            )
            user_sessions[sender] = session
            return jsonify({"status": "awaiting_proper_confirmation"})

        if token_to_check:
            try:
                send_whatsapp_message(
                    phone_to_reply,
                    "✅ Recibido. Estamos verificando el estado de tu pago, un momento por favor... 🔍",
                )
                response_consulta = requests.post(
                    f"{LOLCLI_API_URL}/ConsultarLinkPagoOrdenPrefactura",
                    json={"token": token_to_check},
                    headers=lolcli_headers,
                )

                if response_consulta.status_code == 404:
                    print(
                        "ERROR 404: El endpoint 'ConsultarLinkPagoOrdenPrefactura' no fue encontrado."
                    )
                    send_whatsapp_message(
                        phone_to_reply,
                        "😔 No pudimos verificar tu pago. Por favor, contacta a nuestro equipo de soporte técnico. 🙏",
                    )
                    return jsonify({"status": "error_404_consulting_payment"})

                response_consulta.raise_for_status()
                data_consulta = response_consulta.json()
                payment_data = data_consulta.get("data", {})

                if (
                    data_consulta.get("status") == "success"
                    and payment_data.get("estado_pago") == "COMPLETADO"
                ):
                    send_whatsapp_message(
                        phone_to_reply,
                        "¡Pago confirmado! ✅ Procesando el cambio de tu cita, un momento...",
                    )
                    payload_actualizar = session.get("reschedule_payload", {})
                    print(f"INFO ReagendarCitaWsp payload: {payload_actualizar}")
                    resp = requests.post(
                        f"{LOLCLI_API_URL}/ReagendarCitaWsp",
                        json=payload_actualizar,
                        headers=lolcli_headers,
                    )
                    result = resp.json()
                    print(f"INFO ReagendarCitaWsp response {resp.status_code}: {result}")
                    if resp.ok and result.get("status") == "success":
                        send_whatsapp_message(
                            phone_to_reply,
                            f"✅ ¡Tu cita ha sido reprogramada exitosamente!\n\n"
                            f"🗓️ *Nueva fecha:* {session['new_fecha_user']}\n"
                            f"⏰ *Nueva hora:* {session['new_hora_user']}\n\n"
                            f"¡Te esperamos! 😊",
                        )
                        send_whatsapp_message(
                            phone_to_reply,
                            "Gracias. Escribe *'continuar'* si deseas realizar otra consulta o *'salir'* para terminar la sesión. 😊",
                        )
                        session["state"] = "AWAITING_POST_FLOW"
                        user_sessions[sender] = session
                        return jsonify({"status": "rescheduled"})
                    else:
                        raise Exception(
                            result.get(
                                "xxmessage", result.get("message", "error desconocido")
                            )
                        )
                else:
                    current_status = payment_data.get("estado_pago", "desconocido")
                    print(
                        f"El estado del pago de reprogramación aún no es 'COMPLETADO'. Estado actual: {current_status}"
                    )
                    send_whatsapp_message(
                        phone_to_reply,
                        "⏳ Aún no podemos confirmar tu pago. Asegúrate de haber completado la transacción y "
                        "envíanos el mensaje de confirmación en unos minutos. 🙏",
                    )
            except Exception as e:
                print(f"ERROR Inesperado al consultar pago de reprogramación / reprogramar: {e}")
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Ocurrió un error al verificar tu pago o al reprogramar tu cita. Por favor, intenta "
                    "nuevamente o contáctanos. 🙏",
                )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No encontramos un pago pendiente. Por favor, envíanos el mensaje completo de confirmación que recibiste al pagar. 📋",
            )

    elif state == "AWAITING_POST_FLOW":
        if message_text.lower() in ["continuar", "continue"]:
            session.clear()
            show_main_menu(phone_to_reply, session)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Escribe *'continuar'* para volver al menú o *'salir'* para terminar la sesión. 😊",
            )

    user_sessions[sender] = session
    return jsonify({"status": "processed"})


preload_global_lists()
cleanup_thread = threading.Thread(target=session_cleanup_task, daemon=True)
cleanup_thread.start()
reminder_thread = threading.Thread(target=reminder_task, daemon=True)
reminder_thread.start()

if __name__ == "__main__":
    from waitress import serve
    port = int(os.getenv("PORT", 5001))
    serve(app, host="0.0.0.0", port=port)
