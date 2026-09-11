import logging
import signal
import queue
import time
import serial
from typing import Any

# Импортируем конфигурацию
from config import (
    TG_TOKEN, TG_CHAT_ID, SERIAL_PORT, BAUD_RATE
)
# Импортируем модули
from telegram import TelegramNotifier
from modem import ModemController

logger = logging.getLogger(__name__)

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