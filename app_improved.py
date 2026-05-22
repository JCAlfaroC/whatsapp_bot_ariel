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
DNI_API_URL = "https://my.apidev.pro/api/dni"
DNI_API_TOKEN = os.getenv("DNI_API_TOKEN")  # RENIEC lookup

user_sessions = {}  # stores all active conversations in RAM
lista_sedes_global = []  # clinic branches (loaded once at startup)
lista_documentos_global = []  # document types (loaded once at startup)

# --- Configuración de Tiempos de Inactividad ---
INACTIVITY_REMINDER_PERIOD = 5 * 60
SESSION_EXPIRATION_PERIOD = 10 * 60


# --- Funciones de Consulta a APIs Externas ---
def consultar_reniec(dni):
    if not DNI_API_TOKEN:
        print("ADVERTENCIA: DNI_API_TOKEN no está configurado.")
        return {"success": False}
    headers = {"Authorization": f"Bearer {DNI_API_TOKEN}"}
    payload = {"dni": dni}
    print(f"INFO: Consultando DNI {dni} en la API externa.")
    try:
        response = requests.post(DNI_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        dni_data = data.get("data", {})
        if data.get("success") and dni_data and dni_data.get("nombres"):
            return {
                "success": True,
                "pacpat": dni_data.get("apellido_paterno"),
                "pacmat": dni_data.get("apellido_materno"),
                "pacnam": dni_data.get("nombres"),
                "pacfen": dni_data.get("fecha_nacimiento"),
                "sexcod": "MA"
                if dni_data.get("sexo", "").upper() == "MASCULINO"
                else "FE",
            }
        else:
            print(
                f"ADVERTENCIA: API de DNI no encontró datos para {dni}. Respuesta: {data}"
            )
            return {"success": False}
    except Exception as e:
        print(f"ERROR: La consulta a la API de DNI falló: {e}")
        return {"success": False}


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


def send_mail_reminder(reminder):
    import smtplib
    from email.mime.miltipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_server = os.getenv("EMAIL_SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", 587))
    smtp_user = os.getenv("EMAIL_USER")
    smtp_password = os.getenv("EMAIL_PASSWORD")

    if not smtp_user or not smtp_password:
        print("ADVERTENCIA: Credenciales de email no configuradas")
        return

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = reminder["email"]
    msg["Subject"] = "Recordatorio de tu cita mėdica - LOLIMSA"

    body = (
        f"Estimado/a {reminder['patient_name']}, \n\n"
        f"Le recordamos que tiene una cita programada para mañana:\n\n"
        f" 👨‍⚕️ Mėdico:          {reminder['doctor_name']}\n"
        f" 🩺 Especialidad:    {reminder['speciality']}\n"
        f" 🏥 Sede:            {reminder['sede']}\n"
        f" 🗓️ Fecha y hora     {reminder['appointment_datetime']}\n\n"
        f"Por favor, presėntese 15 minutos antes.\n\n"
        f"Saludos,\nLOLIMSA"
    )

    msg.attach(MIMEText(body, "plain"))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, reminder["email"], msg.as_string())
        server.quit()
        print(f"INFO: Email de recordatorio enviado a {reminder['email']}")
    except Exception as e:
        print(f"ERROR al enviar email de recordatorio: {e}")


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
                    f"🔔 *Recordatorio de cita -- LOLIMSA*\n\n"
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
            f"{LOLCLI_API_URL}/ListaServicios",
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

        # Nuevo payload con los campos estrictamente solicitados por la API
        payload_pago = {
            "cliente": "consultoria",
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


def register_new_patient(session, phone_to_reply, headers):
    try:
        data = session["new_patient_data"]

        # El campo pacfen ya debería estar en formato AAAA-MM-DD
        fecha_nac_original = data.get("pacfen")

        payload_registro = {
            "tidcod": data.get("tidcod"),
            "pacdoc": data.get("pacdoc"),
            "pacpat": data.get("pacpat"),
            "pacmat": data.get("pacmat"),
            "pacnam": data.get("pacnam"),
            "pacfen": fecha_nac_original,
            "sexcod": data.get("sexcod"),
            "pactel": data.get("pactel"),
            "pacdir": data.get("pacdir"),
            "pacmail": data.get("pacmail"),
            "codtas": "TI",
            "ubicod": "150137",
            "siscod_fil": 2,
        }

        print(f"INFO: Registrando nuevo paciente con payload: {payload_registro}")
        response_registro = requests.post(
            f"{LOLCLI_API_URL}/RegistroPaciente", json=payload_registro, headers=headers
        )

        if response_registro.ok and response_registro.json().get("status") == "success":
            time.sleep(1)
            payload_validacion = {
                "tidcod": data.get("tidcod"),
                "pacdoc": data.get("pacdoc"),
            }
            response_validacion = requests.post(
                f"{LOLCLI_API_URL}/ValidarPaciente",
                json=payload_validacion,
                headers=headers,
            )
            pacientes = response_validacion.json().get("paciente", [])

            if pacientes:
                paciente = pacientes[0]
                session["pachis"] = paciente["pachis"]
                session["paciente_nombre"] = paciente["pacpmn"]
                session.pop("new_patient_data", None)
                return True
            else:
                raise Exception(
                    "No se pudo obtener el historial del paciente recién registrado."
                )
        else:
            error_msg = response_registro.json().get("message", "Error desconocido")
            send_whatsapp_message(
                phone_to_reply, f"Hubo un problema al registrarte: {error_msg}."
            )
            user_sessions.pop(session["sender"], None)
            return False
    except Exception as e:
        print(f"ERROR en register_new_patient: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error inesperado durante tu registro. Por favor, contacta a nuestro equipo de soporte. 🙏",
        )
        user_sessions.pop(session["sender"], None)
        return False


def show_final_summary(session, phone_to_reply):
    patient_name = session.get("paciente_nombre")
    if not patient_name and "new_patient_data" in session:
        p_data = session["new_patient_data"]
        patient_name = f"{p_data.get('pacnam', '')} {p_data.get('pacpat', '')} {p_data.get('pacmat', '')}".strip()

    domicilio = session.get("pacdir")
    if not domicilio and "new_patient_data" in session:
        domicilio = session["new_patient_data"].get("pacdir", "")

    summary = (
        f"¡Casi listo! ✨ Por favor, revisa que todo esté correcto:\n\n"
        f"👤 *Paciente:* {patient_name}\n"
        f"🏥 *Sede:* {session['establishment_name']}\n"
        f"🩺 *Especialidad:* {session['sernam']}\n"
        f"👨‍⚕️ *Médico:* {session['mednam']}\n"
        f"🗓️ *Fecha:* {session['fecha_user']}\n"
        f"⏰ *Hora:* {session['hora_user']}\n"
        f"🏷️ *Tarifa:* {session['tardes']}\n\n"
        f"🧾 *Comprobante:* {session['tdofac_name']}\n"
    )

    if session.get("tdofac_name") == "Factura":
        summary += (
            f" *RUC:* {session.get('ruc', '')}\n"
            f" *Razón Social:* {session.get('razon_social', '')}\n"
            f" *Dirección Fiscal:* {session.get('direccion_fiscal', '')}\n\n"
        )
    elif domicilio:
        summary += f" *Domicilio:* {domicilio}\n\n"

    summary += "Si todo está bien, escribe *'Sí'* para confirmar tu cita."

    send_whatsapp_message(phone_to_reply, summary)
    session["state"] = "AWAITING_CONFIRMATION"


# --- TODO: Confirm endpoint names with LOLCLI team ---
# LIST_CITAS_ENDPOINT   = "ListaCitasPaciente"  payload: { "pachis": str }
# ANULAR_CITA_ENDPOINT  = "AnularCita"           payload: { "citid": str }
# INVNUM_INFO_ENDPOINT  = "<pending>"            payload: { "invnum": int }  ← payment receipt info


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


def format_appointments_list(citas, title):
    msg = f"{title}\n\n"
    formatted = []
    for i, cita in enumerate(citas, 1):
        fecha_raw = cita.get("fecha", "")
        hora_raw = cita.get("hora", "")
        date_obj = None
        try:
            date_obj = datetime.strptime(fecha_raw[:19], "%Y-%m-%dT%H:%M:%S")
            fecha = format_date_es(date_obj)
        except (ValueError, TypeError):
            try:
                date_obj = datetime.strptime(fecha_raw, "%Y%m%d")
                fecha = format_date_es(date_obj)

            except (ValueError, TypeError):
                fecha = fecha_raw or "Fecha no disponible"
        if hora_raw:
            try:
                hora = datetime.strptime(hora_raw, "%H%M").strftime("%H:%M")
            except (ValueError, TypeError):
                hora = hora_raw
        elif date_obj and "T" in fecha_raw:
            hora = date_obj.strftime("%H:%M")
        else:
            hora = "Hora no disponible"

        msg += (
            f"*{i}.* 🗓️ {fecha} — ⏰ {hora}\n"
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
        "*3.* 🔄 Reprogramar una cita\n"
        "*4.* 💳 Pagar cita pendiente\n\n"
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
            "👋 ¡Hola! Bienvenido/a a LOLIMSA. Soy tu asistente virtual y estoy aquí para ayudarte. 😊",
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

        elif choice in ["4", "pagar", "pago pendiente"]:
            reply, opts = format_menu(
                "Para pagar tu cita, selecciona tu tipo de documento:",
                lista_documentos_global,
                "tidcod",
                "tiddes",
            )
            session["options"] = opts
            session["state"] = "AWAITING_DOC_TYPE_FOR_PAYMENT"
            send_whatsapp_message(phone_to_reply, reply)

        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ Por favor, escribe *1*, *2*, *3* o *4* para elegir una opción. 😊",
            )

    elif state == "AWAITING_DOC_TYPE_FOR_PAYMENT":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "tiddes"
        )
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            send_whatsapp_message(
                phone_to_reply,
                f"Entendido. Ahora, por favor, ingresa tu número de {selected_option['tiddes']}.",
            )
            session["state"] = "AWAITING_DOC_NUMBER_FOR_PAYMENT"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección de la lista. 🙏",
            )

    elif state == "AWAITING_DOC_NUMBER_FOR_PAYMENT":
        doc_number = message_text.strip()
        tidcod = session.get("tidcod")

        if tidcod == "03" and (not doc_number.isdigit() or len(doc_number) != 8):
            send_whatsapp_message(
                phone_to_reply,
                "⚠️ El DNI debe tener exactamente 8 dígitos numéricos. ¿Puedes verificarlo e intentarlo de nuevo? 🙏",
            )
            user_sessions[sender] = session
            return jsonify({"status": "invalid_dni_for_payment"})

        try:
            payload_paciente = {"tidcod": tidcod, "pacdoc": doc_number}
            response_paciente = requests.post(
                f"{LOLCLI_API_URL}/ValidarPaciente",
                json=payload_paciente,
                headers=lolcli_headers,
            )
            pacientes = response_paciente.json().get("paciente", [])

            if not pacientes:
                send_whatsapp_message(
                    phone_to_reply,
                    "🔍 No encontramos ningún paciente registrado con ese documento. Por favor, verifica que el número sea correcto o comunícate con nosotros. 📞",
                )
                user_sessions.pop(sender, None)
                return jsonify({"status": "patient_not_found_for_payment"})

            paciente = pacientes[0]
            session.update(
                {
                    "pachis": paciente["pachis"],
                    "paciente_nombre": paciente["pacpmn"],
                    "pacdoc": doc_number,
                    "pacdir": paciente.get("pacdir", "DIRECCIÓN NO ESPECIFICADA"),
                }
            )
            send_whatsapp_message(
                phone_to_reply,
                f"Gracias, {paciente['pacpmn']}. Un momento mientras consulto tus citas... 🔍",
            )
            payload_pagos = {"pachis": paciente["pachis"]}
            response_pagos = requests.post(
                f"{LOLCLI_API_URL}/ListaPagosPendientes",
                json=payload_pagos,
                headers=lolcli_headers,
            )

            if response_pagos.ok:
                pendientes = response_pagos.json().get("pendientes", [])
                if pendientes:
                    pago_reciente = pendientes[-1]
                    session["prfnum_cita"] = pago_reciente.get("prfnum")
                    session["invnum_cita"] = pago_reciente.get("invnum")
                    session["costo_total"] = float(pago_reciente.get("prfppac", 0.0))

                    send_whatsapp_message(
                        phone_to_reply,
                        f"Encontré una reserva pendiente de pago por *S/ {session['costo_total']:.2f}*.\n\nEscribe *'Pagar'* para generar un nuevo enlace de pago.",
                    )
                    session["state"] = "PENDING_PAYMENT_ACTION"
                else:
                    send_whatsapp_message(
                        phone_to_reply,
                        "¡Buenas noticias! No encontré ninguna cita *pendiente de pago* a tu nombre.\n\nEsto puede significar que no tienes citas agendadas o que ya están todas pagadas. 😊",
                    )
                    user_sessions.pop(sender, None)
            else:
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Tuvimos un inconveniente al consultar tus pagos. Por favor, intenta en unos minutos. 🙏",
                )
        except Exception as e:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Ocurrió un error inesperado. Por favor, intenta de nuevo en unos momentos. 🙏",
            )
            print(f"Error en AWAITING_DOC_NUMBER_FOR_PAYMENT: {e}")

    elif state == "PENDING_PAYMENT_ACTION":
        if "pagar" in message_text.lower():
            session["tdofac"] = "BO"
            send_whatsapp_message(
                phone_to_reply,
                "💌 Perfecto. Para enviarte el comprobante, necesitamos tu correo electrónico. Por favor, escríbelo a continuación.",
            )
            session["state"] = "AWAITING_EMAIL_FOR_PENDING_PAYMENT"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "💳 Cuando estés listo/a, escribe Pagar para obtener tu enlace de pago. Si prefieres cancelar, escribe salir. 😊",
            )

    elif state == "AWAITING_EMAIL_FOR_PENDING_PAYMENT":
        email = message_text.strip()
        if "@" in email and "." in email:
            session["email"] = email
            send_whatsapp_message(
                phone_to_reply,
                "⏳ Gracias. Estamos generando tu enlace de pago personalizado, un momento por favor...",
            )
            generate_payment_link_and_send(session, phone_to_reply, lolcli_headers)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "📧 El correo ingresado no parece ser válido. Por favor, verifica que tenga el formato correcto (ejemplo: nombre@correo.com).",
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
                f"{LOLCLI_API_URL}/ValidarPaciente",
                json=payload,
                headers=lolcli_headers,
            )
            pacientes = response.json().get("paciente", [])

            if pacientes:
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
            else:
                send_whatsapp_message(
                    phone_to_reply,
                    "📋 Veo que es tu primera vez con nosotros. ¡Bienvenido/a! Vamos a crear tu ficha de paciente, es muy rápido. 😊",
                )
                session["new_patient_data"] = {"tidcod": tidcod, "pacdoc": doc_number}

                if tidcod == "01":
                    send_whatsapp_message(
                        phone_to_reply,
                        "🔍 Consultando tu información para agilizar el registro... Un momento, por favor.",
                    )
                    reniec_data = consultar_reniec(doc_number)
                    if reniec_data.get("success"):
                        session["new_patient_data"].update(reniec_data)
                        full_name = f"{reniec_data.get('pacnam', '')} {reniec_data.get('pacpat', '')} {reniec_data.get('pacmat', '')}".strip()
                        send_whatsapp_message(
                            phone_to_reply, f"Encontramos a: *{full_name}*."
                        )

                        if not reniec_data.get("pacfen"):
                            send_whatsapp_message(
                                phone_to_reply,
                                "Para continuar, por favor, confírmame tu fecha de nacimiento. 🎂\n\n_Usa el formato AAAA-MM-DD, por ejemplo: 1981-05-18_",
                            )
                            session["state"] = "AWAITING_REG_BIRTHDATE"
                        else:
                            send_whatsapp_message(
                                phone_to_reply,
                                "📱 Casi listo. Por favor, indícanos tu número de celular.",
                            )
                            session["state"] = "AWAITING_REG_PHONE"
                    else:
                        send_whatsapp_message(
                            phone_to_reply,
                            "📝 No pudimos validar tu DNI automáticamente, pero no te preocupes. Te haré unas preguntas rápidas. Comenzemos: Cual es tu apellido paterno?",
                        )
                        session["state"] = "AWAITING_MANUAL_PATPAT"
                else:
                    send_whatsapp_message(
                        phone_to_reply,
                        "📝 Para registrarte necesitamos algunos datos basicos. Comencemos: cual es tu apellido paterno?",
                    )
                    session["state"] = "AWAITING_MANUAL_PATPAT"
        except Exception as e:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Tuvimos un inconveniente al verificar tu documento. Por favor, intenta de nuevo. 🙏",
            )
            print(f"Error en AWAITING_DOC_NUMBER: {e}")

    elif state == "AWAITING_REG_BIRTHDATE":
        session["new_patient_data"]["pacfen"] = message_text.strip()
        send_whatsapp_message(
            phone_to_reply,
            "✅ Gracias por confirmar tu fecha de nacimiento. Ahora, por favor, indícanos tu número de celular. 📱",
        )
        session["state"] = "AWAITING_REG_PHONE"

    elif state == "AWAITING_MANUAL_PATPAT":
        session["new_patient_data"]["pacpat"] = message_text.strip().upper()
        send_whatsapp_message(
            phone_to_reply, "✅ Gracias. Ahora, ¿cuál es tu apellido materno?"
        )
        session["state"] = "AWAITING_MANUAL_PACMAT"

    elif state == "AWAITING_MANUAL_PACMAT":
        session["new_patient_data"]["pacmat"] = message_text.strip().upper()
        send_whatsapp_message(
            phone_to_reply, "✅ Entendido. Por favor, escribe tus nombres completos."
        )
        session["state"] = "AWAITING_MANUAL_PACNAM"

    elif state == "AWAITING_MANUAL_PACNAM":
        session["new_patient_data"]["pacnam"] = message_text.strip().upper()
        send_whatsapp_message(
            phone_to_reply,
            "¡Casi listo! Para continuar, por favor, ¿cuál es tu fecha de nacimiento? 🎂\n\n_Usa el formato AAAA-MM-DD, por ejemplo: 1981-05-18_",
        )
        session["state"] = "AWAITING_MANUAL_PACFEN"

    elif state == "AWAITING_MANUAL_PACFEN":
        session["new_patient_data"]["pacfen"] = message_text.strip()
        reply, opts = format_menu(
            "Gracias. Por favor, selecciona tu sexo:",
            [
                {"id": 1, "code": "MA", "name": "Masculino"},
                {"id": 2, "code": "FE", "name": "Femenino"},
            ],
            "code",
            "name",
        )
        session["options"] = opts
        session["state"] = "AWAITING_MANUAL_SEXCOD"
        send_whatsapp_message(phone_to_reply, reply)

    elif state == "AWAITING_MANUAL_SEXCOD":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "name"
        )
        if selected_option:
            session["new_patient_data"]["sexcod"] = selected_option["code"]
            send_whatsapp_message(
                phone_to_reply,
                "✅ Perfecto. Ahora, por favor, escribe tu número de celular. 📱",
            )
            session["state"] = "AWAITING_REG_PHONE"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ Por favor, escribe 1 para Masculino o 2 para Femenino.",
            )

    elif state == "AWAITING_REG_PHONE":
        phone = message_text.strip()
        if phone.isdigit() and len(phone) >= 9:
            session["new_patient_data"]["pactel"] = phone
            send_whatsapp_message(
                phone_to_reply,
                "✅ Gracias. Ahora, por favor, escribe tu correo electrónico. 📧",
            )
            session["state"] = "AWAITING_REG_EMAIL"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "⚠️ El número ingresado no parece ser válido. Por favor, escribe un número de celular con al menos 9 dígitos. 📱",
            )

    elif state == "AWAITING_REG_EMAIL":
        email = message_text.strip()
        if "@" in email and "." in email:
            session["new_patient_data"]["pacmail"] = email
            send_whatsapp_message(
                phone_to_reply,
                "✅ Casi listo. Por último, necesitamos tu dirección de domicilio. 🏠",
            )
            session["state"] = "AWAITING_REG_ADDRESS"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "📧 El correo ingresado no es valido. Por favor, verifica que tenga el formato correcto. Ejemplo: nombre@correo.com",
            )

    elif state == "AWAITING_REG_ADDRESS":
        address = message_text.strip()
        session["new_patient_data"]["pacdir"] = address
        continue_appointment_flow(session, phone_to_reply, lolcli_headers)

    elif state == "AWAITING_ESTABLISHMENT_CLARIFICATION":
        selected_option = process_user_choice(
            message_text, session.get("options", []), "sisent"
        )
        if selected_option:
            session.setdefault("history", []).append("AWAITING_ESTABLISHMENT")
            session["siscod"] = selected_option["siscod"]
            session["establishment_name"] = selected_option["sisent"]
            response = requests.post(
                f"{LOLCLI_API_URL}/ListaServicios",
                json={"siscod": session["siscod"]},
                headers=lolcli_headers,
            )
            servicios = response.json().get("servicios", [])
            reply, formatted_options = format_menu(
                f"¡Perfecto! Ahora, para la sede *{session['establishment_name']}*, ¿qué especialidad necesitas?",
                servicios,
                "sercod",
                "serdes",
            )
            session["options"] = formatted_options
            session["state"] = "AWAITING_SPECIALTY"
            send_whatsapp_message(phone_to_reply, reply)
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
                f"{LOLCLI_API_URL}/ListaServicios",
                json={"siscod": session["siscod"]},
                headers=lolcli_headers,
            )
            servicios = response.json().get("servicios", [])
            reply, formatted_options = format_menu(
                f"Entendido. Ahora, ¿para qué especialidad en *{session['establishment_name']}* necesitas la cita?",
                servicios,
                "sercod",
                "serdes",
            )
            session["options"] = formatted_options
            session["state"] = "AWAITING_SPECIALTY"
            send_whatsapp_message(phone_to_reply, reply)
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
                session["history"].pop()
            else:
                reply, formatted_options = format_menu(
                    "Estos son los doctores con espacio:", medicos, "medcod", "mednam"
                )
                session["options"] = formatted_options
                session["state"] = "AWAITING_DOCTOR"
                send_whatsapp_message(phone_to_reply, reply)
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
                f"{LOLCLI_API_URL}/ListaTarifario", json=payload, headers=lolcli_headers
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
            send_whatsapp_message(
                phone_to_reply,
                f"Ok, elegiste *'{session['tardes']}'*.\n\n¿El comprobante será *Boleta* (1) o *Factura* (2)?",
            )
            session["state"] = "AWAITING_RECEIPT_TYPE"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa tarifa. Por favor, escribe el número de la tarifa que deseas. 🙏",
            )

    elif state == "AWAITING_RECEIPT_TYPE":
        choice = message_text.lower()
        if choice in ["1", "boleta"]:
            session.setdefault("history", []).append("AWAITING_RECEIPT_TYPE")
            session["tdofac"], session["tdofac_name"] = "BO", "Boleta"
            if "new_patient_data" in session:
                show_final_summary(session, phone_to_reply)
            else:
                send_whatsapp_message(
                    phone_to_reply,
                    "🧾 Perfecto, será boleta. Para completar el registro, necesitamos tu dirección de domicilio. 🏠",
                )
                session["state"] = "AWAITING_ADDRESS"

        elif choice in ["2", "factura"]:
            session.setdefault("history", []).append("AWAITING_RECEIPT_TYPE")
            session["tdofac"], session["tdofac_name"] = "FA", "Factura"
            send_whatsapp_message(
                phone_to_reply,
                "🧾 Entendido, será factura. Por favor, ingresa el RUC de la empresa (11 dígitos).",
            )
            session["state"] = "AWAITING_RUC"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ Por favor, escribe 1 para Boleta o 2 para Factura. 🧾",
            )

    elif state == "AWAITING_ADDRESS":
        session["pacdir"] = message_text.strip()
        show_final_summary(session, phone_to_reply)

    elif state == "AWAITING_RUC":
        if message_text.isdigit() and len(message_text) == 11:
            session["ruc"] = message_text
            send_whatsapp_message(
                phone_to_reply,
                "✅ Gracias. Ahora, por favor escribe la Razón Social de la empresa.",
            )
            session["state"] = "AWAITING_RAZON_SOCIAL"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "⚠️ El RUC debe tener exactamente 11 dígitos. Por favor, verifica e inténtalo de nuevo.",
            )

    elif state == "AWAITING_RAZON_SOCIAL":
        session["razon_social"] = message_text.title()
        send_whatsapp_message(
            phone_to_reply,
            "✅ Casi listo. Por último, escribe la Dirección Fiscal de la empresa. 🏢",
        )
        session["state"] = "AWAITING_FISCAL_ADDRESS"

    elif state == "AWAITING_FISCAL_ADDRESS":
        session["direccion_fiscal"] = message_text.title()
        show_final_summary(session, phone_to_reply)

    elif state == "AWAITING_CONFIRMATION":
        if message_text.lower() in ["sí", "si"]:
            if "new_patient_data" in session:
                send_whatsapp_message(
                    phone_to_reply, "Un momento, estoy creando tu ficha de paciente..."
                )
                registration_successful = register_new_patient(
                    session, phone_to_reply, lolcli_headers
                )
                if not registration_successful:
                    return jsonify({"status": "registration_failed"})

            send_whatsapp_message(
                phone_to_reply,
                "Perfecto. Ahora, por favor, indícame tu correo electrónico para enviarte el comprobante de pago.",
            )
            session["state"] = "AWAITING_EMAIL_FOR_PAYMENT"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "🤔 Sin problema. Escribe retroceder si deseas corregir algún dato, o salir si prefieres cancelar. 😊",
            )

    elif state == "AWAITING_EMAIL_FOR_PAYMENT":
        email = message_text.strip()
        if "@" in email and "." in email:
            session["email"] = email
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
                print(f"Error en AWAITING_EMAIL_FOR_PAYMENT (RegistroCita): {e}")
                user_sessions.pop(sender, None)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "El formato del correo no parece correcto. Por favor, ingrésalo de nuevo.",
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
                # WARNING (only for test, delete in production entire function : if token_to_check == ... )
                if token_to_check == "TEST_BYPASS":
                    send_whatsapp_message(
                        phone_to_reply,
                        f"¡Pago confirmado! ✅\n\nTu cita está 100% confirmada.\n\n¡Gracias por preferir LOLIMSA! Te esperamos.",
                    )
                    save_reminder(session)
                    user_sessions.pop(sender, None)
                    return jsonify({"status": "completed_and_session_cleared"})

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
                        f"¡Pago confirmado! ✅\n\nTu cita está 100% confirmada.\n\n¡Gracias por preferir LOLIMSA! Te esperamos.",
                    )
                    save_reminder(session)
                    user_sessions.pop(sender, None)
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
                    f"{LOLCLI_API_URL}/ValidarPaciente",
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
            # TODO: Replace "ListaCitasPaciente" with the confirmed endpoint name
            citas = (
                requests.post(
                    f"{LOLCLI_API_URL}/ListaCitasPacientes",
                    json={"nro_documento": doc_number},
                    headers=lolcli_headers,
                )
                .json()
                .get("citas", [])
            )
            if not citas:
                send_whatsapp_message(
                    phone_to_reply, "📋 No tienes citas agendadas en este momento. 😊"
                )
            else:
                msg, _ = format_appointments_list(
                    citas, f"📋 *Tus citas agendadas, {paciente['pacpmn']}:*"
                )
                msg += "_ℹ️ Para reprogramar una cita, selecciona la opción *3* en el menú principal._"
                send_whatsapp_message(phone_to_reply, msg)
            user_sessions.pop(sender, None)
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
            citas = (
                requests.post(
                    f"{LOLCLI_API_URL}/ListaCitasPacientes",
                    json={"nro_documento": doc_number},
                    headers=lolcli_headers,
                )
                .json()
                .get("citas", [])
            )
            if not citas:
                send_whatsapp_message(
                    phone_to_reply, "📋 No tienes citas agendadas para reprogramar. 😊"
                )
                user_sessions.pop(sender, None)
                return jsonify({"status": "no_appointments"})
            msg, formatted = format_appointments_list(
                citas, "¿Cuál cita deseas reprogramar?"
            )
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
        selected_option = process_user_choice(message_text, session.get("options", []))
        if selected_option:
            session["citid_to_reschedule"] = selected_option.get("secuencia")
            session["siscod"] = os.getenv("LOLCLI_ENTIDAD", "000000001")
            session["sercod"] = selected_option.get("sercod")
            session["medcod"] = selected_option.get("medcod")
            session["mednam"] = selected_option.get("medico", "")
            session["sernam"] = selected_option.get("servicio", "")
            session["establishment_name"] = ""
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
                send_whatsapp_message(
                    phone_to_reply, "⏳ Procesando el cambio de tu cita, un momento..."
                )
                # TODO: Replace "AnularCita" with the confirmed endpoint name
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
                    "usenam": "LOLIMSA",
                }
                if session.get("cittip") == "V" and session.get("zoom_link"):
                    payload_actualizar["xxcitzoomlink"] = session["zoom_link"]

                print(f"INFO ActualizarCitaProtocolo payload: {payload_actualizar}")
                resp = requests.post(
                    f"{LOLCLI_API_URL}/ActualizarCitaProtocolo",
                    json=payload_actualizar,
                    headers=lolcli_headers,
                )
                result = resp.json()
                print(f"INFO ActualizarCitaProtocolo response {resp.status_code}: {result}")
                if resp.ok and result.get("status") == "success":
                    send_whatsapp_message(
                        phone_to_reply,
                        f"✅ ¡Tu cita ha sido reprogramada exitosamente!\n\n"
                        f"🗓️ *Nueva fecha:* {session['new_fecha_user']}\n"
                        f"⏰ *Nueva hora:* {session['new_hora_user']}\n\n"
                        f"¡Te esperamos! 😊",
                    )
                    user_sessions.pop(sender, None)
                    return jsonify({"status": "rescheduled"})
                else:
                    raise Exception(
                        result.get(
                            "xxmessage", result.get("message", "error desconocido")
                        )
                    )

            except Exception as e:
                print(f"ERROR en AWAITING_RESCHEDULE_CONFIRMATION: {e}")
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Ocurrió un error al reprogramar tu cita. Por favor, intenta de nuevo o contáctanos. 🙏",
                )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Entendido. Escribe *'salir'* si deseas cancelar o continúa eligiendo. 😊",
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
