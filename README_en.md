[Русский](./README.md) | **English**

# Forward SMS & Calls to Telegram

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL_3.0-blue.svg)](LICENSE)

A service for integrating a USB/GSM modem with Telegram. The bot not only automatically forwards incoming SMS and notifies you of incoming calls, but also allows you to **send SMS, make ring-and-drop calls** (pinging), and **send USSD requests** (check balance, etc.) via Telegram commands.

The application communicates via a serial port (using AT commands), which makes it compatible with most popular USB modems (Huawei, ZTE, Quectel, etc.) for building notification systems, SMS gateways, or smart home integrations.

## 🚀 Features
* 📩 **Incoming SMS:** Automatic forwarding of incoming SMS text to Telegram (supports concatenating long messages and Unicode/Cyrillic).
* 📥 **Missed SMS:** Reads unread messages from the modem's memory when the container starts.
* 📞 **Incoming calls:** Notifications for incoming calls (sends the caller's phone number) and automatically drops the call after a specified delay.
* ✉️ **Sending SMS:** Send messages to any number directly from the chat with the bot.
* ☎️ **Outgoing calls:** Command to make a ring-and-drop call (the bot dials the specified number and hangs up after a set time).
* 💰 **USSD requests:** Execute USSD commands directly from chat (e.g., checking balance).
* 🐳 Ready to run in Docker with flexible configuration via environment variables.

---

## 💬 Telegram Bot Commands

Send the following commands to your Telegram bot:

* `/help` or `/start` — Show help for available commands.
* `/call <number>` — Make an outgoing ring-and-drop call. (Example: `/call +79991234567`)
* `/sms <number> <text>` — Send an SMS. (Example: `/sms +79991234567 Hello, this is a test!`)
* `/ussd <code>` — Send a USSD request. (Example: `/ussd *100#` or `/ussd *102#`)
* `/reboot` — Reboot modem via `AT+CFUN=1,1` with automatic COM port reconnection.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TG_TOKEN` | *(required)* | Your Telegram bot token (obtained from [@BotFather](https://t.me/BotFather)). |
| `TG_CHAT_ID` | *(required)* | ID of the chat, group, or user to receive notifications. |
| `SERIAL_PORT` | `/dev/ttyUSB2` | Path to the modem's serial port inside the container (AT port). |
| `BAUD_RATE` | `115200` | Baud rate for the modem port connection. |
| `SIM_PIN` | *(none)* | PIN code for the SIM card (if set). If provided, the script will automatically unlock the SIM card. |
| `OUTGOING_CALL_DURATION` | `5.0` | Duration of an outgoing call in seconds before auto-hangup (`/call`). |
| `INCOMING_CALL_DURATION` | `2.0` | Delay in seconds before the bot drops an incoming call. |
| `CLIP_DEBOUNCE_TIME` | `10.0` | Anti-spam filter (in seconds) for incoming calls from the same number. |
| `MULTIPART_SMS_TIMEOUT` | `15.0` | Timeout (in seconds) for waiting for all parts of a concatenated SMS before sending what was received. |
| `LOG_LEVEL` | `INFO` | Log verbosity level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Set to `DEBUG` to view raw AT commands. |

---

## 🐳 Run via Docker Compose

Create a `docker-compose.yml` file and add the following configuration to it:

```yaml
services:
  forward-sms-calls-to-telegram:
    image: metro2012/forward-sms-calls-to-telegram:latest
    pull_policy: always
    container_name: forward-sms-calls-to-telegram
    restart: unless-stopped
    devices:
      - "/dev/ttyUSB2:/dev/ttyUSB2"
    environment:
      - SERIAL_PORT=/dev/ttyUSB2
      - BAUD_RATE=115200
      - SIM_PIN=1234
      - TG_TOKEN=785432109:AAE1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P
      - TG_CHAT_ID=123456789
      - LOG_LEVEL=INFO
      - OUTGOING_CALL_DURATION=5.0
      - INCOMING_CALL_DURATION=2.0
      - CLIP_DEBOUNCE_TIME=10.0
      - MULTIPART_SMS_TIMEOUT=15.0
```

---

## 📄 License

This project is licensed under the **GNU Affero General Public License v3.0 (GNU AGPLv3)** - see the [LICENSE](LICENSE) file for details.