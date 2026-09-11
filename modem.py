import sys
import time
import re
import logging
import threading
import queue
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple
import serial

from config import (
    SIM_PIN, CLIP_DEBOUNCE_TIME, OUTGOING_CALL_DURATION, INCOMING_CALL_DURATION, LOG_LEVEL
)
from telegram import TelegramNotifier

logger = logging.getLogger(__name__)

from messaging.sms import SmsSubmit, SmsDeliver
import gsm0338  # noqa: F401
# ==========================================================

def encode_ussd_7bit(text: str) -> str:
    """Упаковывает ASCII строку (например '*102#') в GSM 7-bit PDU HEX-строку."""
    septets = [ord(c) & 0x7F for c in text]
    octets = []
    carry = 0
    carry_bits = 0

    for septet in septets:
        take_bits = 8 - carry_bits

        if take_bits == 8:
            carry = septet
            carry_bits = 7
        else:
            octet = carry | ((septet << carry_bits) & 0xFF)
            octets.append(octet)

            carry = septet >> take_bits
            carry_bits = 7 - take_bits

    if carry_bits > 0:
        octets.append(carry)

    return "".join([f"{b:02X}" for b in octets])

class ModemController:

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
        try:
            self.ser = serial.Serial(self.port, self.baud_rate, timeout=1, rtscts=True, dsrdtr=True)
            time.sleep(2)
            if not self._wait_for_modem_ready(timeout=25):
                raise serial.SerialException("Модем не прошел проверку готовности SIM/SMS.")
            self._init_modem()
            logger.info("Модем инициализирован и готов к работе.")

            try:
                logger.info("Проверка пропущенных СМС в памяти...")
                self.process_all_unread_sms()
            except Exception as e:
                logger.error(f"Ошибка при стартовой проверке СМС: {e}")
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            logger.info("Закрытие порта...")
            try:
                self.ser.close()
            except Exception as e:
                logger.warning(f"Ошибка при закрытии порта: {e}")
        self.ser = None

    def reboot(self) -> bool:
        logger.info("Инициирован перезапуск модема командой AT+CFUN=1,1...")
        self.notifier.send_message("♻️ Перезагрузка модема (`AT+CFUN=1,1`)... Ожидание переподключения COM-порта.")

        try:
            if self.ser and self.ser.is_open:
                self.ser.write(b"AT+CFUN=1,1\r\n")
                try:
                    self.ser.flush()
                except Exception:
                    pass
                time.sleep(1)
        except Exception as e:
            logger.warning(f"Ошибка отправки AT+CFUN=1,1 (возможно, порт уже закрыт): {e}")

        self.disconnect()
        return self._wait_for_reconnect(timeout=60)

    def _wait_for_reconnect(self, timeout: int = 60) -> bool:
        logger.info(f"Ожидание восстановления порта {self.port} (таймаут {timeout} сек)...")
        time.sleep(5)

        start_time = time.time()
        while time.time() - start_time < timeout:
            if not self.is_running:
                return False
            try:
                logger.debug(f"Попытка переподключения к {self.port}...")
                self.ser = serial.Serial(self.port, self.baud_rate, timeout=1, rtscts=True, dsrdtr=True)
                time.sleep(1)

                resp = self._send_at_command(b"AT\r\n", timeout=1.0)
                if resp and "OK" in resp:
                    logger.info("Модем ответил OK на команду AT.")
                    time.sleep(1)
                    if not self._wait_for_modem_ready(timeout=25):
                        raise serial.SerialException("Модем не прошел проверку готовности SIM/SMS после перезагрузки.")
                    self._init_modem()
                    logger.info("Модем успешно переподключен и готов к работе.")

                    try:
                        self.process_all_unread_sms()
                    except Exception as e:
                        logger.error(f"Ошибка проверки СМС после перезагрузки: {e}")

                    self.notifier.send_message("✅ Модем успешно перезагружен и готов к работе!")
                    return True
                else:
                    logger.debug("Порт открыт, но модем ещё не отвечает OK на AT.")
                    self.disconnect()
            except (serial.SerialException, OSError) as e:
                logger.debug(f"Порт {self.port} пока недоступен: {e}")
                self.disconnect()

            time.sleep(2)

        logger.error(f"Не удалось восстановить связь с модемом за {timeout} секунд.")
        self.notifier.send_message(f"❌ Не удалось восстановить связь с модемом по порту `{self.port}` после перезагрузки.")
        raise serial.SerialException(f"Таймаут восстановления связи после перезагрузки ({timeout}с)")

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
        except (serial.SerialException, OSError) as e:
            logger.warning(f"Ошибка отправки команды: {e}")
            return None

    def _wait_for_modem_ready(self, timeout: int = 25) -> bool:
        logger.info("Проверка готовности SIM-карты (AT+CPIN?)...")
        start = time.time()

        sim_ready = False
        while time.time() - start < timeout:
            resp = self._send_at_command(b"AT+CPIN?\r\n", timeout=1.0)
            if resp and "+CPIN: READY" in resp:
                sim_ready = True
                logger.info("SIM-карта готова (+CPIN: READY).")
                break
            elif resp and "SIM PIN" in resp:
                if SIM_PIN:
                    logger.info(f"SIM-карта требует PIN-код. Вводим PIN: {SIM_PIN}")
                    pin_resp = self._send_at_command(f'AT+CPIN="{SIM_PIN}"\r\n'.encode(), timeout=2.0)
                    if pin_resp and "OK" in pin_resp:
                        logger.info("PIN-код отправлен. Ожидаем готовности...")
                        time.sleep(2)
                    else:
                        logger.error("Ошибка при вводе PIN-кода! Неверный PIN.")
                        if getattr(self, 'notifier', None):
                            self.notifier.send_message("❌ ФАТАЛЬНАЯ ОШИБКА: Неверный PIN-код. Бот остановлен.")
                        sys.exit("ФАТАЛЬНАЯ ОШИБКА: Неверный PIN-код. Остановка работы во избежание блокировки SIM-карты.")
                else:
                    logger.error("SIM-карта требует ввода PIN-кода, но переменная окружения SIM_PIN не задана!")
                    sys.exit("ФАТАЛЬНАЯ ОШИБКА: Переменная окружения SIM_PIN не задана. Остановка.")
            time.sleep(1)

        if not sim_ready:
            logger.warning("SIM-карта не перешла в состояние READY за отведенное время.")

        logger.info("Проверка готовности подсистемы SMS (AT+CPMS?)...")
        sms_ready = False
        while time.time() - start < timeout:
            resp = self._send_at_command(b"AT+CPMS?\r\n", timeout=1.0)
            if resp and "+CPMS:" in resp and "ERROR" not in resp:
                sms_ready = True
                logger.info(f"Подсистема SMS готова: {resp.strip()}")
                break
            time.sleep(1)

        if not sms_ready:
            logger.warning("Подсистема SMS не ответила на AT+CPMS? вовремя.")

        return sim_ready and sms_ready

    def _setup_sms_storage(self) -> None:
        resp = self._send_at_command(b'AT+CPMS="ME","ME","ME"\r\n', timeout=2.0)
        if resp and "OK" in resp:
            logger.info("Установлено хранилище SMS: ME (память модема).")
        else:
            logger.info("Хранилище ME недоступно или не поддерживается, переключаем на SM (SIM-карта)...")
            self._send_at_command(b'AT+CPMS="SM","SM","SM"\r\n', timeout=2.0)

    def _init_modem(self) -> None:
        commands = [
            b"AT\r\n",
            b"ATE0\r\n",
            b'AT^STSF=0\r\n',
            b"AT+CMGF=0\r\n",
            b"AT+CLIP=1\r\n",
            b"AT+CNMI=2,1,0,0,0\r\n"
        ]
        if LOG_LEVEL != logging.DEBUG:
            commands.append(b"AT^CURC=0\r\n")
        else:
            commands.append(b"AT^CURC=1\r\n")

        for cmd in commands:
            if self._send_at_command(cmd) is None:
                raise serial.SerialException(f"Модем не отвечает на {cmd}")

        self._setup_sms_storage()

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
                sms = SmsDeliver(pdu_hex).data
                sender = sms.get("number", "Неизвестный")
                text = sms.get("text", "")
                if text:
                    grouped_sms[sender].append((idx, text))
            except Exception as e:
                logger.error(f"Ошибка декодирования PDU (индекс {idx}): {e}")

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

        time.sleep(0.1)
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
            self._send_at_command(b"AT+CHUP\r\n")
            if self.ser: self.ser.reset_input_buffer()

        elif action == "/call" and len(parts) >= 2:
            self.perform_call(parts[1])

        elif action == "/sms" and len(parts) >= 3:
            self.send_sms(parts[1], parts[2])

        elif action == "/ussd" and len(parts) >= 2:
            ussd_code = parts[1]
            self.notifier.send_message(f"⏳ Отправка USSD запроса `{ussd_code}`...")
            packed_ussd = encode_ussd_7bit(ussd_code)
            self._send_at_command(b'AT+CSCS="GSM"\r\n', timeout=0.5)
            resp = self._send_at_command(f'AT+CUSD=1,"{packed_ussd}",15\r\n'.encode(), timeout=2.0)

            if resp and "ERROR" in resp:
                self.notifier.send_message("❌ Ошибка USSD: Модем отверг запрос (ERROR).")

        elif action == "/reboot":
            self.reboot()

        elif action in ["/help", "/start"]:
            help_msg = (
                "🤖 *ModemBot*\n\n"
                "📞 `/call +7999...` — Звонок-сброс\n"
                "✉️ `/sms +7999... Текст` — Отправить SMS\n"
                "💰 `/ussd *100#` — Запрос баланса / USSD\n"
                "♻️ `/reboot` — Перезапуск модема (AT+CFUN=1,1)\n"
            )
            self.notifier.send_message(help_msg)
        else:
            self.notifier.send_message("❌ Команда не распознана. Жми /help")

    def _handle_realtime_sms(self, line: str) -> None:
        try:
            text_raw = self.ser.readline()
            text = text_raw.decode("utf-8", errors="ignore").strip()
            sender_match = re.search(r'"([^"]+)"', line)
            sender = sender_match.group(1) if sender_match else "Неизвестный"
            self.notifier.send_message(f"📩 *Новое СМС*\n👤 От: `{sender}`\n💬 Текст: {text}")
        except Exception as e:
            logger.error(f"Ошибка чтения входящего СМС в реальном времени: {e}")

    def _handle_ussd_response(self, line: str) -> None:
        if line.strip().startswith("+CUSD: 2"):
            self.notifier.send_message(
                "❌ *Сеть отклонила USSD запрос (+CUSD: 2)*\n"
                "Возможные причины:\n"
                "1. Модем работает в режиме 'Только 4G' (USSD требует 3G/2G).\n"
                "2. Оператор временно недоступен или запрос не поддерживается."
            )
            return

        match = re.search(r'\+CUSD:\s*\d,\s*"([^"]+)"(?:,\s*(\d+))?', line)
        if match:
            hex_text = match.group(1)
            dcs = int(match.group(2)) if match.group(2) else 15

            decoded = hex_text
            try:
                if all(c in "0123456789ABCDEFabcdef" for c in hex_text) and len(hex_text) % 2 == 0:
                    raw_bytes = bytes.fromhex(hex_text)
                    if dcs == 72:
                        decoded = raw_bytes.decode('utf-16-be', errors='ignore')
                    else:
                        try:
                            decoded = raw_bytes.decode('gsm0338')
                        except Exception:
                            decoded = raw_bytes.decode('ascii', errors='ignore')
            except Exception as e:
                logger.error(f"Не удалось декодировать USSD: {e}")

            self.notifier.send_message(f"💰 *USSD Ответ:*\n`{decoded}`")

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
                self._handle_realtime_sms(line)
            elif line.startswith("+CMTI:"):
                time.sleep(3)
                self.process_all_unread_sms()
            elif line.startswith("+CLIP:"):
                self.handle_incoming_call(line)
            elif line.startswith("+CUSD:"):
                self._handle_ussd_response(line)
