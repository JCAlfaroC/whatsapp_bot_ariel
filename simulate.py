"""
Interactive CLI simulator for the WhatsApp chatbot.
Uses the real LOLCLI and RENIEC APIs from .env.
Bot replies are printed to the terminal instead of sent via WhatsApp.
"""

import sys, os, types

# Force UTF-8 so emojis print correctly on Windows
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Stub thefuzz if not installed ────────────────────────────────────────────
try:
    from thefuzz import process
except ImportError:
    thefuzz_mod = types.ModuleType("thefuzz")
    process_mod = types.ModuleType("thefuzz.process")
    process_mod.extractOne = lambda q, choices: (choices[0] if choices else q, 90)
    thefuzz_mod.process = process_mod
    sys.modules["thefuzz"] = thefuzz_mod
    sys.modules["thefuzz.process"] = process_mod

# ── Stub dotenv ───────────────────────────────────────────────────────────────
dotenv_mod = types.ModuleType("dotenv")
dotenv_mod.load_dotenv = lambda: None
sys.modules["dotenv"] = dotenv_mod

# ── Load .env manually ───────────────────────────────────────────────────────
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)

# Dummy Evolution vars (not used in simulator)
os.environ.setdefault("EVOLUTION_API_URL", "http://unused")
os.environ.setdefault("EVOLUTION_API_KEY", "unused")
os.environ.setdefault("EVOLUTION_INSTANCE_NAME", "unused")

# ── Import the chatbot app ───────────────────────────────────────────────────
import app_improved as chatbot

# ── Replace send_whatsapp_message with terminal output ───────────────────────
CYAN  = "\033[96m"
RESET = "\033[0m"
GREEN = "\033[92m"
GRAY  = "\033[90m"

def terminal_send(phone, text):
    print()
    for line in text.split("\n"):
        # Render *bold* markers as-is (WhatsApp markdown)
        print(f"  {CYAN}🤖 {line}{RESET}")

chatbot.send_whatsapp_message = terminal_send
chatbot.time.sleep = lambda *a: None   # skip throttle delays

# ── Preload global lists using real API ──────────────────────────────────────
print(f"\n{GRAY}Conectando con la API LOLCLI...{RESET}")
try:
    chatbot.preload_global_lists()
    if chatbot.lista_documentos_global:
        print(f"{GRAY}✓ {len(chatbot.lista_documentos_global)} tipos de documento cargados{RESET}")
    else:
        print(f"{GRAY}⚠ No se pudieron cargar los tipos de documento (API no responde){RESET}")
except Exception as e:
    print(f"{GRAY}⚠ Error al conectar con la API: {e}{RESET}")

# ── Simulator loop ───────────────────────────────────────────────────────────
SENDER = "51999999999@s.whatsapp.net"

print(f"\n{'─'*55}")
print(f"  SIMULADOR DE CONVERSACIÓN — chatbot-medico")
print(f"  Escribe tu mensaje y presiona Enter.")
print(f"  Escribe  'salir'  o  Ctrl+C  para terminar.")
print(f"{'─'*55}")
print(f"{GRAY}  (Los mensajes se envían como si vinieras del número 51999999999){RESET}\n")

def drive_webhook(text):
    payload = {
        "data": {
            "key": {"remoteJid": SENDER, "fromMe": False},
            "message": {"conversation": text}
        }
    }
    with chatbot.app.test_request_context(
        "/webhook", method="POST", json=payload,
        content_type="application/json"
    ):
        chatbot.app.preprocess_request()
        chatbot.webhook_handler()

# Kick off the conversation automatically
drive_webhook("Hola")

while True:
    try:
        user_input = input(f"\n  {GREEN}Tú:{RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{GRAY}Simulación terminada.{RESET}\n")
        break

    if not user_input:
        continue

    drive_webhook(user_input)

    if user_input.lower() in ["salir", "cancelar"] and SENDER not in chatbot.user_sessions:
        print(f"\n{GRAY}Sesión finalizada.{RESET}\n")
        break
