import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")


if not TG_TOKEN or not TG_CHAT_ID:
    print("❌ ОШИБКА: Не заданы обязательные переменные окружения!", file=sys.stderr)
    sys.exit(1)

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB2")
BAUD_RATE = int(os.getenv("BAUD_RATE", "115200"))

SIM_PIN = os.getenv("SIM_PIN")

CLIP_DEBOUNCE_TIME = float(os.getenv("CLIP_DEBOUNCE_TIME", "10.0"))
OUTGOING_CALL_DURATION = float(os.getenv("OUTGOING_CALL_DURATION", "5.0"))
INCOMING_CALL_DURATION = float(os.getenv("INCOMING_CALL_DURATION", "2.0"))

LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - [%(name)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
