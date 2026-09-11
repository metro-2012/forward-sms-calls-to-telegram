import threading
import queue
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging

logger = logging.getLogger(__name__)

class TelegramNotifier:

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.running = False
        self.poll_thread = None

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
                logger.error(f"Сетевая ошибка поллинга: {e}")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Непредвиденная ошибка при поллинге: {e}")
                time.sleep(5)
