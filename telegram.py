import threading
import queue
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional, List
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

    @staticmethod
    def _split_message(text: str, max_len: int = 4000) -> List[str]:
        if len(text) <= max_len:
            return [text]

        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            split_pos = remaining.rfind("\n", 0, max_len)
            if split_pos <= 0:
                split_pos = remaining.rfind(" ", 0, max_len)
            if split_pos <= 0:
                split_pos = max_len

            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos:].lstrip("\r\n")
        return chunks

    def _send_chunk(self, text: str, parse_mode: Optional[str]) -> bool:
        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = self.send_session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except requests.exceptions.HTTPError as e:
            if parse_mode and e.response is not None and e.response.status_code == 400:
                logger.warning(f"Ошибка разметки Telegram ({e}), повторная отправка без разметки...")
                try:
                    plain_payload = {"chat_id": self.chat_id, "text": text}
                    fb_resp = self.send_session.post(url, json=plain_payload, timeout=10)
                    fb_resp.raise_for_status()
                    return True
                except requests.exceptions.RequestException as fb_e:
                    logger.error(f"Ошибка отправки чистым текстом: {fb_e}")
                    return False
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False

    def send_message(self, message: str, parse_mode: Optional[str] = "Markdown") -> bool:
        logger.info(f"Отправка в Telegram: {repr(message)}")
        chunks = self._split_message(message)
        for chunk in chunks:
            if not self._send_chunk(chunk, parse_mode):
                return False
        return True

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
