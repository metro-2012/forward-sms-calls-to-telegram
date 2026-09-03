import os
import sys
import time
import re
import logging
import signal
import threading
import queue
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple
import serial
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

# ================= ИМПОРТЫ ДЛЯ SMS =================
try:
    from smspdudecoder.easy import read_incoming_sms
    from smspdudecoder.errors import SMSError
except ImportError:
    SMSError = Exception  # Fallback

try:
    from messaging.sms import SmsSubmit
except ImportError:
    print("❌ ОШИБКА: Установите библиотеку для генерации PDU!", file=sys.stderr)
    print("Выполните: pip install python-messaging", file=sys.stderr)
    sys.exit(1)
# ===================================================

# ================= НАСТРОЙКИ ИЗ ОКРУЖЕНИЯ =================
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

if not TG_TOKEN or not TG_CHAT_ID:
    print("❌ ОШИБКА: Не заданы обязательные переменные окружения!", file=sys.stderr)
    sys.exit(1)

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB2")
try:
    BAUD_RATE = int(os.getenv("BAUD_RATE", "115200"))
except ValueError:
    print("❌ ОШИБКА: Неверное значение BAUD_RATE.", file=sys.stderr)
    sys.exit(1)

CLIP_DEBOUNCE_TIME = float(os.getenv("CLIP_DEBOUNCE_TIME", "10.0"))

# Настройки продолжительности звонков (в секундах)
try:
    OUTGOING_CALL_DURATION = float(os.getenv("OUTGOING_CALL_DURATION", "5.0"))
except ValueError:
    print("❌ ОШИБКА: Неверное значение OUTGOING_CALL_DURATION.", file=sys.stderr)
    sys.exit(1)

try:
    INCOMING_CALL_DURATION = float(os.getenv("INCOMING_CALL_DURATION", "2"))
except ValueError:
    print("❌ ОШИБКА: Неверное значение INCOMING_CALL_DURATION.", file=sys.stderr)
    sys.exit(1)
# ==========================================================

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL
}
log_level = LOG_LEVELS.get(log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(levelname)s - [%(name)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ModemBot")


class TelegramNotifier:
    """Управление отправкой уведомлений и получением команд из Telegram."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.running = False
        self.poll_thread = None

        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)

    def send_message(self, message: str) -> bool:
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }
            response = self.session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False

    def start_polling(self, cmd_queue: queue.Queue):
        self.running = True
        self.poll_thread = threading.Thread(target=self._poll_updates, args=(cmd_queue,), daemon=True)
        self.poll_thread.start()
        logger.info("Telegram Polling запущен.")

    def stop_polling(self):
        self.running = False

    def _poll_updates(self, cmd_queue: queue.Queue):
        offset = None
        while self.running:
            try:
                url = f"{self.base_url}/getUpdates"
                params = {"timeout": 30, "allowed_updates": ["message"]}
                if offset:
                    params["offset"] = offset

                response = self.session.get(url, params=params, timeout=40)
                if response.status_code == 200:
                    data = response.json()
                    for result in data.get("result", []):
                        offset = result["update_id"] + 1
                        message = result.get("message", {})

                        chat_id = str(message.get("chat", {}).get("id", ""))
                        text = message.get("text", "").strip()

                        if chat_id == self.chat_id and text.startswith("/"):
                            logger.info(f"Получена команда из Telegram: {text}")
                            cmd_queue.put(text)
                else:
                    time.sleep(2)
            except Exception as e:
                logger.debug(f"Ошибка при поллинге Telegram: {e}")
                time.sleep(5)


class ModemController:
    """Управление GSM-модемом через AT-команды."""

    def __init__(self, port: str, baud_rate: int, notifier: TelegramNotifier):
        self.port = port
        self.baud_rate = baud_rate
        self.notifier = notifier
        self.ser: Optional[serial.Serial] = None
        self.last_call_info: Dict[str, Any] = {"number": None, "timestamp": 0.0}

    def connect(self) -> None:
        logger.info(f"Открытие порта {self.port}...")
        self.ser = serial.Serial(self.port, self.baud_rate, timeout=1, rtscts=True, dsrdtr=True)
        time.sleep(2)
        self._init_modem()
        logger.info("Модем инициализирован и готов к работе.")

        # Проверка непрочитанных СМС при старте
        try:
            logger.info("Проверка пропущенных СМС в памяти...")
            self.process_all_unread_sms()
        except Exception as e:
            logger.error(f"Ошибка при стартовой проверке СМС: {e}")

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            logger.info("Закрытие порта...")
            try:
                self.ser.close()
            except Exception as e:
                logger.warning(f"Ошибка при закрытии порта: {e}")
        self.ser = None

    def _send_at_command(self, command: bytes, timeout: float = 1.0) -> Optional[str]:
        if not self.ser or not self.ser.is_open:
            return None
        try:
            logger.debug(f">>> TX: {command}")
            self.ser.write(command)
            self.ser.timeout = timeout

            response = b""
            start_time = time.time()
            while time.time() - start_time < timeout:
                line = self.ser.readline()
                response += line
                if b"OK\r\n" in response or b"ERROR\r\n" in response:
                    break

            decoded_response = response.decode("utf-8", errors="ignore")
            logger.debug(f"<<< RX: {repr(decoded_response)}")
            return decoded_response
        except (serial.SerialException, OSError, BrokenPipeError) as e:
            logger.warning(f"Ошибка при отправке команды {command}: {e}")
            return None

    def _init_modem(self) -> None:
        commands = [
            b"AT\r\n",
            b"ATE0\r\n",
            b"AT+CMGF=0\r\n",  # Только PDU режим
            b"AT+CLIP=1\r\n",
            b"AT+CNMI=2,1,0,0,0\r\n"
        ]
        for cmd in commands:
            resp = self._send_at_command(cmd)
            if resp is None:
                raise serial.SerialException("Модем не отвечает при инициализации")
        if self.ser:
            self.ser.reset_input_buffer()

    def process_all_unread_sms(self) -> None:
        if not self.ser: return
        self.ser.reset_input_buffer()

        logger.debug(f">>> TX: b'AT+CMGL=4\\r\\n'")
        self.ser.write(b"AT+CMGL=4\r\n")
        time.sleep(1.5)
        response = self.ser.read_until(b"OK\r\n").decode("utf-8", errors="ignore")
        logger.debug(f"<<< RX (CMGL): {repr(response)}")

        lines = response.split("\r\n")
        messages_to_process: List[Tuple[str, str]] = []
        indices_to_delete = set()

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("+CMGL:"):
                match = re.search(r"\+CMGL:\s*(\d+)\s*,\s*(\d+)", line)
                if match:
                    idx = match.group(1)
                    indices_to_delete.add(idx)

                    if i + 1 < len(lines):
                        pdu_hex = lines[i + 1].strip()
                        if pdu_hex and all(c in "0123456789ABCDEFabcdef" for c in pdu_hex):
                            messages_to_process.append((idx, pdu_hex))
                            i += 1  # Пропускаем строку с PDU
            i += 1

        decoded_messages = []
        for idx, pdu_hex in messages_to_process:
            try:
                sms = read_incoming_sms(pdu_hex)
                sms["index"] = idx
                decoded_messages.append(sms)
            except Exception as e:
                logger.error(f"Ошибка декодирования PDU (индекс {idx}): {e}. Сообщение будет удалено.")

        grouped_sms = defaultdict(list)

        for sms in decoded_messages:
            sender = sms.get("sender", "Неизвестный")
            text = sms.get("content", "").strip()
            if text:
                grouped_sms[sender].append(text)

        # Отправляем уведомления
        for sender, texts in grouped_sms.items():
            full_text = "".join(texts)
            safe_sender = sender.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")
            safe_text = full_text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")
            msg = f"📩 *Новое СМС*\n👤 От: `{safe_sender}`\n💬 Текст:\n{safe_text}"
            self.notifier.send_message(msg)

        # Удаляем обработанные сообщения
        for idx in indices_to_delete:
            self._send_at_command(f"AT+CMGD={idx}\r\n".encode())

    def handle_incoming_call(self, first_clip_line: str) -> None:
        final_sender = None
        try:
            resp = self._send_at_command(b"AT+CLCC\r\n", timeout=1.0)
            if resp:
                for line in resp.split("\r\n"):
                    if line.startswith("+CLCC:") and "1,4" in line:
                        parts = line.split(",")
                        if len(parts) >= 6:
                            final_sender = parts[5].strip().strip('"')
                            break
        except Exception:
            pass

        if not final_sender:
            sender_match = re.search(r'"([^"]+)"', first_clip_line)
            final_sender = sender_match.group(1) if sender_match else "Скрытый номер"

        current_time = time.time()
        if final_sender == self.last_call_info["number"] and (
                current_time - self.last_call_info["timestamp"]) < CLIP_DEBOUNCE_TIME:
            return

        self.last_call_info["number"] = final_sender
        self.last_call_info["timestamp"] = current_time

        msg = f"📞 *Входящий звонок*\n👤 От: `{final_sender}`"
        self.notifier.send_message(msg)

        time.sleep(INCOMING_CALL_DURATION)
        self._send_at_command(b"AT+CHUP\r\n")
        if self.ser: self.ser.reset_input_buffer()

    # ================= ЛОГИКА ОТПРАВКИ PDU =================

    def _send_pdu_to_modem(self, pdu_len: int, pdu_data: str) -> bool:
        self.ser.reset_input_buffer()
        cmd = f"AT+CMGS={pdu_len}\r\n".encode()
        logger.debug(f">>> TX: {cmd}")
        self.ser.write(cmd)

        self.ser.timeout = 3.0
        prompt = self.ser.read_until(b"> ")
        logger.debug(f"<<< RX: {prompt}")

        if b">" not in prompt:
            logger.error(f"Нет символа '>' от модема. Ответ: {prompt.decode(errors='ignore')}")
            self.ser.write(b"\x1B")  # ESC
            return False

        logger.debug(f">>> TX (PDU DATA): {pdu_data}<CTRL-Z>")
        self.ser.write((pdu_data + "\x1A").encode())

        self.ser.timeout = 15.0
        response = b""
        while True:
            line = self.ser.readline()
            response += line
            if b"OK\r\n" in response:
                logger.debug(f"<<< RX: {repr(response.decode('utf-8', errors='ignore'))}")
                return True
            if b"ERROR\r\n" in response or line == b"":  # пустая строка = таймаут
                logger.error(f"Ошибка/таймаут от модема: {response.decode(errors='ignore')}")
                return False

    def perform_call(self, number: str, duration: float = OUTGOING_CALL_DURATION) -> None:
        self.notifier.send_message(f"⏳ Начинаю звонок на `{number}`...")
        logger.info(f"Исходящий звонок на {number}...")
        self._send_at_command(f"ATD{number};\r\n".encode())
        time.sleep(duration)
        self._send_at_command(b"AT+CHUP\r\n")
        logger.info(f"Звонок на {number} завершен.")
        self.notifier.send_message(f"✅ Звонок-сброс на `{number}` завершен.")

    def send_sms(self, number: str, text: str) -> None:
        self.notifier.send_message(f"⏳ Формирование PDU и отправка на `{number}`...")

        try:
            sms = SmsSubmit(number, text)
            pdus = sms.to_pdu()
            total = len(pdus)

            for i, pdu_obj in enumerate(pdus, 1):
                logger.info(f"Отправка части SMS {i}/{total} на номер {number}...")

                pdu_hex = pdu_obj.pdu.upper()

                if not pdu_hex.startswith("00") and not pdu_hex.startswith("0791"):
                    pdu_hex = "00" + pdu_hex

                smsc_len_bytes = int(pdu_hex[:2], 16)
                total_pdu_bytes = len(pdu_hex) // 2
                actual_length = total_pdu_bytes - (1 + smsc_len_bytes)

                logger.info(f"PDU ({len(pdu_hex)} chars): {pdu_hex}")

                success = self._send_pdu_to_modem(actual_length, pdu_hex)

                if not success:
                    raise Exception(f"Ошибка при отправке части {i} из {total}")

                if i < total:
                    time.sleep(2)

            self.notifier.send_message(f"✅ СМС ({total} частей) успешно отправлено на `{number}`")
        except Exception as e:
            logger.error(f"Критическая ошибка при отправке SMS: {e}")
            self.notifier.send_message(f"❌ Ошибка отправки СМС: {e}")
            self._send_at_command(b"\x1B")

    def process_telegram_command(self, cmd_text: str) -> None:
        parts = cmd_text.split(maxsplit=2)
        action = parts[0].lower()

        if action == "/call" and len(parts) >= 2:
            number = parts[1]
            self.perform_call(number)

        elif action == "/sms" and len(parts) >= 3:
            number = parts[1]
            text = parts[2]
            self.send_sms(number, text)

        elif action == "/help" or action == "/start":
            help_msg = (
                "🤖 *ModemBot Готов к работе!*\n\n"
                "*Доступные команды:*\n"
                "📞 `/call +79991234567` — Звонок-сброс\n"
                "✉️ `/sms +79991234567 Привет!` — Отправить SMS (любая длина, кириллица)\n"
            )
            self.notifier.send_message(help_msg)

        else:
            self.notifier.send_message("❌ Неизвестная команда. Введите `/help`.")

    def run(self, cmd_queue: queue.Queue) -> None:
        self.connect()
        logger.info("Ожидание событий...")

        try:
            while self.ser and self.ser.is_open:
                try:
                    while not cmd_queue.empty():
                        cmd = cmd_queue.get_nowait()
                        self.process_telegram_command(cmd)
                except queue.Empty:
                    pass

                try:
                    raw_line = self.ser.readline()
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                except UnicodeDecodeError:
                    continue

                if not line:
                    continue

                # Логируем все непустые строки, поступающие от модема вне опросов (URC)
                logger.debug(f"<<< URC: {line}")

                if line.startswith("+CMT:"):
                    try:
                        text_raw = self.ser.readline()
                        text = text_raw.decode("utf-8", errors="ignore").strip()
                        logger.debug(f"<<< URC (CMT payload): {text}")
                        sender_match = re.search(r'"([^"]+)"', line)
                        sender = sender_match.group(1) if sender_match else "Неизвестный"
                        self.notifier.send_message(f"📩 *Новое СМС*\n👤 От: `{sender}`\n💬 Текст: {text}")
                    except Exception as e:
                        logger.error(f"Ошибка чтения СМС: {e}")

                elif line.startswith("+CMTI:"):
                    try:
                        time.sleep(3)
                        self.process_all_unread_sms()
                    except Exception as e:
                        logger.error(f"Критическая ошибка обработки СМС: {e}")

                elif line.startswith("+CLIP:"):
                    try:
                        self.handle_incoming_call(line)
                    except Exception as e:
                        logger.error(f"Ошибка обработки звонка: {e}")

        except (serial.SerialException, OSError, IOError, BrokenPipeError) as e:
            logger.error(f"Потеря связи с модемом: {e}. Переподключение...")
            raise


def main() -> None:
    notifier = TelegramNotifier(TG_TOKEN, TG_CHAT_ID)
    modem = ModemController(SERIAL_PORT, BAUD_RATE, notifier)

    cmd_queue = queue.Queue()
    running = True

    def signal_handler(sig: int, frame: Any) -> None:
        nonlocal running
        logger.info(f"\nПолучен сигнал {sig}, завершение работы...")
        running = False
        notifier.stop_polling()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    notifier.start_polling(cmd_queue)

    while running:
        try:
            modem.run(cmd_queue)
        except (serial.SerialException, OSError, IOError, BrokenPipeError) as e:
            logger.error(f"Ошибка связи: {e}. Переподключение через 5 секунд...")
            modem.disconnect()
            if running:
                time.sleep(5)
        except Exception as e:
            logger.error(f"Непредвиденная ошибка: {e}")
            modem.disconnect()
            if running:
                time.sleep(5)

    modem.disconnect()
    notifier.stop_polling()
    logger.info("Работа скрипта завершена корректно.")


if __name__ == "__main__":
    main()