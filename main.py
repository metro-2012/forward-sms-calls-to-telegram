import os
import sys
import time
import re
import logging
import signal
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple

import serial
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from dotenv import load_dotenv

load_dotenv()

try:
    from smspdudecoder.easy import read_incoming_sms
    # Пытаемся импортировать специфичную ошибку, если ее нет - используем базовый Exception
    from smspdudecoder.errors import SMSError
except ImportError:
    SMSError = Exception  # Fallback для совместимости

# ================= НАСТРОЙКИ ИЗ ОКРУЖЕНИЯ =================
# Обязательные переменные
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

if not TG_TOKEN or not TG_CHAT_ID:
    print("❌ ОШИБКА: Не заданы обязательные переменные окружения!", file=sys.stderr)
    print("Установите их перед запуском, например:", file=sys.stderr)
    print("  export TG_TOKEN='ваш_токен_бота'", file=sys.stderr)
    print("  export TG_CHAT_ID='ваш_chat_id'", file=sys.stderr)
    print("Или создайте файл .env и используйте python-dotenv.", file=sys.stderr)
    sys.exit(1)

# Необязательные переменные со значениями по умолчанию
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB2")
try:
    BAUD_RATE = int(os.getenv("BAUD_RATE", "115200"))
except ValueError:
    print("❌ ОШИБКА: Неверное значение BAUD_RATE. Должно быть целым числом.", file=sys.stderr)
    sys.exit(1)

CLIP_DEBOUNCE_TIME = float(os.getenv("CLIP_DEBOUNCE_TIME", "10.0"))
# ==========================================================

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(name)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ModemBot")


class TelegramNotifier:
    """Управление отправкой уведомлений в Telegram с повторными попытками."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{self.token}"

        # Настройка сессии с retry для production-надежности
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
        self.ser = serial.Serial(self.port, self.baud_rate, timeout=5, rtscts=True, dsrdtr=True)
        time.sleep(2)  # Даем модему время на инициализацию после открытия порта
        self._init_modem()
        logger.info("Модем инициализирован и готов к работе.")

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            logger.info("Закрытие порта...")
            try:
                self.ser.close()
            except Exception as e:
                logger.warning(f"Ошибка при закрытии порта: {e}")
        self.ser = None

    def _send_at_command(self, command: bytes, timeout: float = 1.0) -> Optional[str]:
        """Отправляет AT-команду и возвращает ответ."""
        if not self.ser or not self.ser.is_open:
            return None
        try:
            self.ser.write(command)
            time.sleep(0.5)
            response = self.ser.read_all().decode("utf-8", errors="ignore")
            return response
        except (serial.SerialException, OSError, BrokenPipeError) as e:
            logger.warning(f"Ошибка при отправке команды {command}: {e}")
            return None

    def _init_modem(self) -> None:
        commands = [
            b"AT\r\n",
            b"ATE0\r\n",
            b"AT+CMGF=0\r\n",  # PDU режим
            b"AT+CLIP=1\r\n",  # Включить АОН
            b"AT+CNMI=2,1,0,0,0\r\n"  # Уведомления о новых СМС
        ]
        for cmd in commands:
            resp = self._send_at_command(cmd)
            if resp is None:
                raise serial.SerialException("Модем не отвечает при инициализации")
            logger.debug(f"CMD: {cmd.strip()} -> {resp.strip()}")

        if self.ser:
            self.ser.reset_input_buffer()

    def process_all_unread_sms(self) -> None:
        """Читает все непрочитанные сообщения, декодирует, склеивает и удаляет их."""
        if not self.ser:
            return

        self.ser.reset_input_buffer()
        self.ser.write(b"AT+CMGL=0\r\n")
        time.sleep(1.5)
        response = self.ser.read_until(b"OK\r\n").decode("utf-8", errors="ignore")

        if "ERROR" in response:
            logger.warning("Команда AT+CMGL=0 вернула ERROR, пробуем AT+CMGL=4 (ALL)")
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
                match = re.search(r"\+CMGL:\s*(\d+)\s*,\s*(\d+)", line)
                if match:
                    idx, stat = match.group(1), match.group(2)
                    if stat == "0":  # 0 = REC UNREAD
                        if i + 1 < len(lines):
                            pdu_hex = lines[i + 1].strip()
                            if pdu_hex and all(c in "0123456789ABCDEFabcdef" for c in pdu_hex):
                                messages_to_process.append((idx, pdu_hex))
                                i += 1
            i += 1

        if not messages_to_process:
            logger.debug("Непрочитанных сообщений не найдено.")
            return

        decoded_messages = []
        for idx, pdu_hex in messages_to_process:
            try:
                sms = read_incoming_sms(pdu_hex)
                sms["index"] = idx
                decoded_messages.append(sms)
            except SMSError as e:
                logger.error(f"Ошибка декодирования PDU (индекс {idx}): {e}")
            except Exception as e:
                logger.error(f"Неизвестная ошибка при декодировании (индекс {idx}): {type(e).__name__} - {e}")

        grouped_sms = defaultdict(list)
        indices_to_delete = []

        for sms in decoded_messages:
            sender = sms.get("sender", "Неизвестный")
            text = sms.get("content", "").strip()
            if text:
                grouped_sms[sender].append(text)
            indices_to_delete.append(sms.get("index"))

        for sender, texts in grouped_sms.items():
            full_text = "".join(texts)
            safe_sender = sender.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")
            safe_text = full_text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")

            msg = f"📩 *Новое СМС*\n👤 От: `{safe_sender}`\n💬 Текст:\n{safe_text}"
            self.notifier.send_message(msg)
            logger.info(f"Обработано СМС от {sender} ({len(texts)} частей)")

        for idx in indices_to_delete:
            self._send_at_command(f"AT+CMGD={idx}\r\n".encode())
            logger.debug(f"Удалено сообщение с индексом {idx}")

    def handle_incoming_call(self, first_clip_line: str) -> None:
        """Обрабатывает входящий звонок, запрашивая актуальный номер через AT+CLCC."""
        final_sender = None

        try:
            resp = self._send_at_command(b"AT+CLCC\r\n", timeout=1.0)
            if resp:
                for line in resp.split("\r\n"):
                    if line.startswith("+CLCC:"):
                        parts = line.split(",")
                        # <dir>=1 (входящий), <stat>=4 (incoming call)
                        if len(parts) >= 6 and parts[1].strip() == "1" and parts[2].strip() == "4":
                            number_raw = parts[5].strip().strip('"')
                            if number_raw:
                                final_sender = number_raw
                                logger.info(f"✅ Получен актуальный номер через AT+CLCC: {final_sender}")
                                break
        except Exception as e:
            logger.warning(f"Ошибка при запросе AT+CLCC: {e}")

        if not final_sender:
            sender_match = re.search(r'"([^"]+)"', first_clip_line)
            final_sender = sender_match.group(1) if sender_match else "Скрытый номер"
            logger.info(f"⚠️ AT+CLCC не вернул номер, используем данные из первого CLIP: {final_sender}")

        current_time = time.time()
        if final_sender == self.last_call_info["number"] and (
                current_time - self.last_call_info["timestamp"]) < CLIP_DEBOUNCE_TIME:
            logger.debug(f"Игнорируем дубликат звонка от {final_sender}")
            return

        self.last_call_info["number"] = final_sender
        self.last_call_info["timestamp"] = current_time

        msg = f"📞 *Входящий звонок*\n👤 От: `{final_sender}`"
        self.notifier.send_message(msg)

        time.sleep(0.3)
        self._send_at_command(b"AT+CHUP\r\n")
        time.sleep(0.3)

        if self.ser:
            self.ser.reset_input_buffer()

        logger.info(f"Звонок от {final_sender} обработан и сброшен")

    def run(self) -> None:
        """Основной цикл обработки событий модема."""
        self.connect()
        logger.info("Ожидание событий...")

        try:
            while self.ser and self.ser.is_open:
                try:
                    line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                except UnicodeDecodeError:
                    continue

                if not line:
                    continue

                logger.debug(f"RAW MODEM DATA: {repr(line)}")

                if line.startswith("+CMT:"):
                    try:
                        text = self.ser.readline().decode("utf-8", errors="ignore").strip()
                        sender_match = re.search(r'"([^"]+)"', line)
                        sender = sender_match.group(1) if sender_match else "Неизвестный"
                        msg = f"📩 *Новое СМС*\n👤 От: `{sender}`\n💬 Текст: {text}"
                        self.notifier.send_message(msg)
                        logger.info(f"СМС от {sender} (обработано через +CMT)")
                    except Exception as e:
                        logger.error(f"Ошибка чтения СМС (+CMT): {e}")

                elif line.startswith("+CMTI:"):
                    try:
                        time.sleep(5)  # Задержка для склейки длинных СМС
                        self.process_all_unread_sms()
                    except Exception as e:
                        logger.error(f"Критическая ошибка обработки СМС (+CMTI): {e}")

                elif line.startswith("+CLIP:"):
                    try:
                        self.handle_incoming_call(line)
                    except Exception as e:
                        logger.error(f"Ошибка обработки звонка (+CLIP): {e}")

        except (serial.SerialException, OSError, IOError, BrokenPipeError) as e:
            logger.error(f"Потеря связи с модемом: {e}. Переподключение...")
            raise  # Пробрасываем исключение для обработки во внешнем цикле


def main() -> None:
    """Точка входа с обработкой сигналов завершения."""
    notifier = TelegramNotifier(TG_TOKEN, TG_CHAT_ID)
    modem = ModemController(SERIAL_PORT, BAUD_RATE, notifier)

    # Флаг для корректного завершения
    running = True

    def signal_handler(sig: int, frame: Any) -> None:
        nonlocal running
        logger.info(f"\nПолучен сигнал {sig}, завершение работы...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while running:
        try:
            modem.run()
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
    logger.info("Работа скрипта завершена корректно.")


if __name__ == "__main__":
    main()