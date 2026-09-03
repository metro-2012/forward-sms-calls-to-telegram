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

# ================= ИМПОРТЫ ДЛЯ SMS И USSD =================
try:
    from smspdudecoder.easy import read_incoming_sms
    from smspdudecoder.errors import SMSError
except ImportError:
    SMSError = Exception

try:
    from messaging.sms import SmsSubmit
except ImportError:
    print("❌ ОШИБКА: Установите библиотеку python-messaging!", file=sys.stderr)
    sys.exit(1)

try:
    import gsm0338
except ImportError:
    print("❌ ОШИБКА: Установите библиотеку gsm0338 для работы с USSD!", file=sys.stderr)
    print("Выполните: pip install gsm0338", file=sys.stderr)
    sys.exit(1)
# ==========================================================

# ================= НАСТРОЙКИ ИЗ ОКРУЖЕНИЯ =================
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

if not TG_TOKEN or not TG_CHAT_ID:
    print("❌ ОШИБКА: Не заданы обязательные переменные окружения!", file=sys.stderr)
    sys.exit(1)

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB2")
BAUD_RATE = int(os.getenv("BAUD_RATE", "115200"))

CLIP_DEBOUNCE_TIME = float(os.getenv("CLIP_DEBOUNCE_TIME", "10.0"))
OUTGOING_CALL_DURATION = float(os.getenv("OUTGOING_CALL_DURATION", "5.0"))
INCOMING_CALL_DURATION = float(os.getenv("INCOMING_CALL_DURATION", "2.0"))

log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_str, logging.INFO)

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

        # Раздельные сессии для потокобезопасности
        self.poll_session = requests.Session()
        self.send_session = requests.Session()

        retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.poll_session.mount("https://", adapter)
        self.send_session.mount("https://", adapter)

    def send_message(self, message: str) -> bool:
        logger.info(f"Отправка в Telegram: {repr(message)}")
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "Markdown"}
            response = self.send_session.post(url, json=payload, timeout=10)
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
        if self.poll_thread and self.poll_thread.is_alive():
            self.poll_thread.join(timeout=2.0)

    def _poll_updates(self, cmd_queue: queue.Queue):
        offset = None
        while self.running:
            try:
                url = f"{self.base_url}/getUpdates"
                params = {"timeout": 30, "allowed_updates": ["message"]}
                if offset:
                    params["offset"] = offset

                response = self.poll_session.get(url, params=params, timeout=40)
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
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
                logger.debug(f"Сетевая ошибка поллинга (это нормально при перебоях): {e}")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Непредвиденная ошибка при поллинге Telegram: {e}")
                time.sleep(5)


class ModemController:
    """Управление GSM-модемом через AT-команды."""

    def __init__(self, port: str, baud_rate: int, notifier: TelegramNotifier, cmd_queue: queue.Queue):
        self.port = port
        self.baud_rate = baud_rate
        self.notifier = notifier
        self.cmd_queue = cmd_queue
        self.ser: Optional[serial.Serial] = None
        self.last_call_info: Dict[str, Any] = {"number": None, "timestamp": 0.0}
        self.is_running = False

    def connect(self) -> None:
        logger.info(f"Открытие порта {self.port}...")
        self.ser = serial.Serial(self.port, self.baud_rate, timeout=1, rtscts=True, dsrdtr=True)
        time.sleep(2)
        self._init_modem()
        logger.info("Модем инициализирован и готов к работе.")

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

    def stop(self) -> None:
        self.is_running = False

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

            decoded = response.decode("utf-8", errors="ignore")
            logger.debug(f"<<< RX: {repr(decoded)}")
            return decoded
        except serial.SerialException as e:
            logger.warning(f"Ошибка отправки команды: {e}")
            return None

    def _init_modem(self) -> None:
        commands = [
            b"AT\r\n",
            b"ATE0\r\n",
            b"AT+CMGF=0\r\n",  # Только PDU режим
            b"AT+CLIP=1\r\n",
            b"AT+CNMI=2,1,0,0,0\r\n"
        ]
        if log_level != logging.DEBUG:
            commands.append(b"AT^CURC=0\r\n")

        for cmd in commands:
            if self._send_at_command(cmd) is None:
                raise serial.SerialException(f"Модем не отвечает на {cmd}")
        if self.ser:
            self.ser.reset_input_buffer()

    def process_all_unread_sms(self) -> None:
        if not self.ser: return
        self.ser.reset_input_buffer()

        self.ser.write(b"AT+CMGL=4\r\n")
        time.sleep(1.5)
        response = self.ser.read_until(b"OK\r\n").decode("utf-8", errors="ignore")

        lines = response.split("\r\n")
        messages_to_process: List[Tuple[str, str]] = []

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("+CMGL:"):
                match = re.search(r"\+CMGL:\s*(\d+)", line)
                if match:
                    idx = match.group(1)
                    if i + 1 < len(lines):
                        pdu_hex = lines[i + 1].strip()
                        if pdu_hex and all(c in "0123456789ABCDEFabcdef" for c in pdu_hex):
                            messages_to_process.append((idx, pdu_hex))
                            i += 1
            i += 1

        grouped_sms = defaultdict(list)
        for idx, pdu_hex in messages_to_process:
            try:
                sms = read_incoming_sms(pdu_hex)
                sender = sms.get("sender", "Неизвестный")
                text = sms.get("content", "").strip()
                if text:
                    grouped_sms[sender].append((idx, text))
            except Exception as e:
                logger.error(f"Ошибка декодирования PDU (индекс {idx}): {e}")

        # Безопасное удаление: удаляем только если сообщение ушло в ТГ
        for sender, items in grouped_sms.items():
            full_text = "".join([item[1] for item in items])
            indices_to_delete = [item[0] for item in items]

            safe_sender = sender.replace("_", "\\_").replace("*", "\\*")
            safe_text = full_text.replace("_", "\\_").replace("*", "\\*")
            msg = f"📩 *Новое СМС*\n👤 От: `{safe_sender}`\n💬 Текст:\n{safe_text}"

            if self.notifier.send_message(msg):
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

        # Не блокируем основной поток, закидываем команду на сброс в очередь через таймер
        threading.Timer(
            INCOMING_CALL_DURATION,
            lambda: self.cmd_queue.put("/internal_hangup")
        ).start()

    def _send_pdu_to_modem(self, pdu_len: int, pdu_data: str) -> bool:
        self.ser.reset_input_buffer()
        self.ser.write(f"AT+CMGS={pdu_len}\r\n".encode())
        self.ser.timeout = 3.0
        prompt = self.ser.read_until(b"> ")

        if b">" not in prompt:
            logger.error(f"Нет символа '>'. Ответ: {prompt.decode(errors='ignore')}")
            self.ser.write(b"\x1B")
            return False

        time.sleep(0.1)  # Микро-пауза для стабилизации буфера модема
        self.ser.write((pdu_data + "\x1A").encode())
        self.ser.timeout = 15.0

        response = b""
        while True:
            line = self.ser.readline()
            response += line
            if b"OK\r\n" in response:
                return True
            if b"ERROR\r\n" in response or line == b"":
                logger.error(f"Ошибка от модема: {response.decode(errors='ignore')}")
                return False

    def perform_call(self, number: str, duration: float = OUTGOING_CALL_DURATION) -> None:
        self.notifier.send_message(f"⏳ Начинаю набор номера `{number}`...")

        self.ser.reset_input_buffer()
        self.ser.timeout = 1.0
        self.ser.write(f"ATD{number};\r\n".encode())

        call_initiated = False
        start_dial_time = time.time()

        # Ждем 2 секунды быстрых ошибок (занято, нет сети)
        while time.time() - start_dial_time < 2.0:
            line = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if line and any(err in line for err in ["ERROR", "NO CARRIER", "BUSY", "NO DIALTONE"]):
                logger.warning(f"Сбой при попытке позвонить: {line}")
                self.notifier.send_message(f"❌ Сбой вызова на `{number}`. Ответ: `{line}`")
                return

        logger.info(f"Дозвон пошел, ждем {duration} сек...")
        wait_start = time.time()

        while time.time() - wait_start < duration:
            line = self.ser.readline().decode("utf-8", errors="ignore").strip()
            if line and any(err in line for err in ["NO CARRIER", "BUSY"]):
                self.notifier.send_message(f"ℹ️ Абонент `{number}` отклонил/сбросил вызов ({line}).")
                return

        logger.info(f"Сброс звонка на {number} (таймаут вышел).")
        self._send_at_command(b"AT+CHUP\r\n")
        self.notifier.send_message(f"✅ Звонок-сброс на `{number}` выполнен.")

    def send_sms(self, number: str, text: str) -> None:
        self.notifier.send_message(f"⏳ Отправка СМС на `{number}`...")
        try:
            sms = SmsSubmit(number, text)
            pdus = sms.to_pdu()
            total = len(pdus)

            for i, pdu_obj in enumerate(pdus, 1):
                pdu_hex = pdu_obj.pdu.upper()
                if not pdu_hex.startswith("00") and not pdu_hex.startswith("0791"):
                    pdu_hex = "00" + pdu_hex

                smsc_len = int(pdu_hex[:2], 16)
                actual_length = (len(pdu_hex) // 2) - (1 + smsc_len)

                if not self._send_pdu_to_modem(actual_length, pdu_hex):
                    raise Exception(f"Ошибка при отправке части {i} из {total}")

                if i < total: time.sleep(2)

            self.notifier.send_message(f"✅ СМС ({total} ч.) отправлено на `{number}`")
        except Exception as e:
            self.notifier.send_message(f"❌ Ошибка отправки СМС: {e}")
            self._send_at_command(b"\x1B")

    def process_telegram_command(self, cmd_text: str) -> None:
        parts = cmd_text.split(maxsplit=2)
        action = parts[0].lower()

        if action == "/internal_hangup":
            # Вызывается внутренним таймером асинхронно
            self._send_at_command(b"AT+CHUP\r\n")
            if self.ser: self.ser.reset_input_buffer()
        elif action == "/call" and len(parts) >= 2:
            self.perform_call(parts[1])
        elif action == "/sms" and len(parts) >= 3:
            self.send_sms(parts[1], parts[2])
        elif action == "/ussd" and len(parts) >= 2:
            self.notifier.send_message(f"⏳ Отправка USSD запроса `{parts[1]}`...")
            self._send_at_command(f'AT+CUSD=1,"{parts[1]}",15\r\n'.encode())
        elif action in ["/help", "/start"]:
            help_msg = (
                "🤖 *ModemBot*\n\n"
                "📞 `/call +7999...` — Звонок-сброс\n"
                "✉️ `/sms +7999... Текст` — Отправить SMS\n"
                "💰 `/ussd *100#` — Запрос баланса / USSD\n"
            )
            self.notifier.send_message(help_msg)
        else:
            self.notifier.send_message("❌ Команда не распознана. Жми `/help`.")

    def run(self) -> None:
        self.connect()
        self.is_running = True

        while self.is_running and self.ser and self.ser.is_open:
            try:
                while not self.cmd_queue.empty():
                    cmd = self.cmd_queue.get_nowait()
                    self.process_telegram_command(cmd)
            except queue.Empty:
                pass

            try:
                raw_line = self.ser.readline()
                if not raw_line: continue
                line = raw_line.decode("utf-8", errors="ignore").strip()
            except UnicodeDecodeError:
                continue

            if not line: continue
            logger.debug(f"<<< URC: {line}")

            if line.startswith("+CMT:"):
                try:
                    text_raw = self.ser.readline()
                    text = text_raw.decode("utf-8", errors="ignore").strip()
                    sender_match = re.search(r'"([^"]+)"', line)
                    sender = sender_match.group(1) if sender_match else "Неизвестный"
                    self.notifier.send_message(f"📩 *Новое СМС*\n👤 От: `{sender}`\n💬 Текст: {text}")
                except Exception as e:
                    logger.error(f"Ошибка чтения входящего СМС в реальном времени: {e}")

            elif line.startswith("+CMTI:"):
                time.sleep(3)
                self.process_all_unread_sms()

            elif line.startswith("+CLIP:"):
                self.handle_incoming_call(line)

            # Обработка асинхронного ответа на USSD запрос
            elif line.startswith("+CUSD:"):
                # Пример формата: +CUSD: 0,"D9775D0E0A97E965",15 или +CUSD: 0,"04120430...",72
                match = re.search(r'\+CUSD:\s*\d,\s*"([^"]+)"(?:,\s*(\d+))?', line)
                if match:
                    hex_text = match.group(1)
                    dcs = int(match.group(2)) if match.group(2) else 15

                    decoded = hex_text
                    try:
                        # Проверяем, является ли строка HEX-кодом
                        if all(c in "0123456789ABCDEFabcdef" for c in hex_text) and len(hex_text) % 2 == 0:
                            raw_bytes = bytes.fromhex(hex_text)
                            if dcs == 72:  # UCS2 кодировка (обычно кириллица)
                                decoded = raw_bytes.decode('utf-16-be')
                            else:  # GSM-7 (обычно латиница)
                                decoded = raw_bytes.decode('gsm0338')
                    except Exception as e:
                        logger.error(f"Не удалось декодировать USSD: {e}")

                    self.notifier.send_message(f"💰 *USSD Ответ:*\n`{decoded}`")


def main() -> None:
    cmd_queue = queue.Queue()
    notifier = TelegramNotifier(TG_TOKEN, TG_CHAT_ID)
    modem = ModemController(SERIAL_PORT, BAUD_RATE, notifier, cmd_queue)

    running = True

    def signal_handler(sig: int, frame: Any) -> None:
        nonlocal running
        logger.info(f"\nПолучен сигнал {sig}, завершение работы...")
        running = False
        notifier.stop_polling()
        modem.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    notifier.start_polling(cmd_queue)

    while running:
        try:
            modem.run()
        except (serial.SerialException, OSError, BrokenPipeError) as e:
            logger.error(f"Ошибка связи: {e}. Переподключение через 5 сек...")
            modem.disconnect()
            if running: time.sleep(5)
        except Exception as e:
            logger.error(f"Непредвиденная ошибка: {e}")
            modem.disconnect()
            if running: time.sleep(5)

    modem.disconnect()
    logger.info("Работа скрипта завершена.")


if __name__ == "__main__":
    main()