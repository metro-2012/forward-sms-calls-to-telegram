import logging
import signal
import queue
import time
import serial
from typing import Any, Optional

# Импортируем конфигурацию
from config import (
    TG_TOKEN, TG_CHAT_ID, SERIAL_PORT, BAUD_RATE,
    MODEM_ERROR_TIMEOUT, MODEM_ERROR_REPEAT_TIMEOUT
)
# Импортируем модули
from telegram import TelegramNotifier
from modem import ModemController

logger = logging.getLogger(__name__)

def main() -> None:
    cmd_queue = queue.Queue()
    notifier = TelegramNotifier(TG_TOKEN, TG_CHAT_ID)

    failure_start_time: Optional[float] = None
    last_alert_time: float = 0.0
    alert_sent: bool = False

    def on_connected() -> None:
        nonlocal failure_start_time, last_alert_time, alert_sent
        if alert_sent:
            logger.info("Связь с модемом восстановлена. Отправка уведомления в Telegram...")
            msg = (
                f"✅ *Связь с модемом восстановлена!*\n"
                f"Порт `{SERIAL_PORT}` снова доступен, модем инициализирован и готов к работе."
            )
            notifier.send_message(msg)
            alert_sent = False
        failure_start_time = None
        last_alert_time = 0.0

    modem = ModemController(SERIAL_PORT, BAUD_RATE, notifier, cmd_queue, on_connected=on_connected)

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

    def handle_connection_error(error: Exception) -> None:
        nonlocal failure_start_time, last_alert_time, alert_sent
        now = time.time()
        if failure_start_time is None:
            failure_start_time = now

        elapsed = now - failure_start_time
        should_alert = False

        if not alert_sent and elapsed >= MODEM_ERROR_TIMEOUT:
            should_alert = True
        elif alert_sent and MODEM_ERROR_REPEAT_TIMEOUT > 0 and (now - last_alert_time) >= MODEM_ERROR_REPEAT_TIMEOUT:
            should_alert = True

        if should_alert:
            safe_err = str(error).replace("`", "'")
            msg = (
                f"❌ *Ошибка связи с модемом!*\n"
                f"Не удается восстановить связь по порту `{SERIAL_PORT}` уже {int(elapsed)} сек.\n"
                f"Ошибка: `{safe_err}`"
            )
            logger.error(f"Длительная невозможность связи с модемом ({int(elapsed)} сек). Отправка ошибки в Telegram...")
            if notifier.send_message(msg):
                alert_sent = True
                last_alert_time = now

    while running:
        try:
            modem.run()
        except (serial.SerialException, OSError, BrokenPipeError) as e:
            logger.error(f"Ошибка связи: {e}. Переподключение через 5 сек...")
            handle_connection_error(e)
            modem.disconnect()
            if running: time.sleep(5)
        except Exception as e:
            logger.error(f"Непредвиденная ошибка: {e}")
            handle_connection_error(e)
            modem.disconnect()
            if running: time.sleep(5)

    modem.disconnect()
    logger.info("Работа скрипта завершена.")

if __name__ == "__main__":
    main()