[Русский](./README.md) | **English**

# Forward SMS & Calls to Telegram

A service for automatically forwarding incoming SMS messages and call notifications from a USB/GSM modem directly to Telegram.

The application works via a serial port (AT commands), allowing you to use most popular USB modems (Huawei, ZTE, Quectel, etc.) to build notification systems, SMS gateways, or integrate with smart home systems.

## 🚀 Features
* 📩 Forwarding the text of incoming SMS messages.
* 📞 Incoming call notifications (sends the phone number).
* 🐳 Ready to run in Docker.
* ⚙️ Flexible configuration via environment variables.

---

## ⚠️ Host System Preparation (Important for Some Modems)

Many modern 3G/4G USB modems, when plugged into a computer, are detected as a virtual CD-ROM with drivers rather than as a modem. To switch the device into modem mode and expose the required serial ports (e.g., `/dev/ttyUSB0`, `/dev/ttyUSB2`), special utilities must be installed on the host machine.

If your modem is not detected correctly, run the following on the host machine:

```bash
sudo apt update
sudo apt install usb-modeswitch usb-modeswitch-data
```

*After installing the utilities, unplug and re-plug the USB modem. Verify that the ports have appeared with the command `ls /dev/ttyUSB*`.*

---

## ⚙️ Environment Variables

| Variable | Description | Example |
| :--- | :--- | :--- |
| `SERIAL_PORT` | Path to the modem's serial port inside the container. Typically, this is the port handling AT commands (often `ttyUSB2` or `ttyUSB0`). | `/dev/ttyUSB2` |
| `TG_TOKEN` | Your Telegram bot token (obtained from [@BotFather](https://t.me/BotFather)). | `785432109:AAE1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P` |
| `TG_CHAT_ID` | ID of the chat, group, or user where SMS and calls will be forwarded. | `123456789` |
| `LOG_LEVEL` | Log verbosity level. Available values: `DEBUG`, `INFO`, `WARNING`, `ERROR`. | `DEBUG` |

---

## 🐳 Running via Docker Compose

Create a `docker-compose.yml` file and add the following configuration:

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
      - TG_TOKEN=785432109:AAE1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P
      - TG_CHAT_ID=123456789
      - LOG_LEVEL=DEBUG
```

### Running:
```bash
# Run in detached mode
docker compose up -d

# View logs
docker compose logs -f forward-sms-calls-to-telegram
```

---

## 📱 How to Get TG_TOKEN and TG_CHAT_ID?

1. **Bot Token (`TG_TOKEN`):**
   * Find the [@BotFather](https://t.me/BotFather) bot on Telegram.
   * Send the `/newbot` command and follow the instructions.
   * Copy the token you receive (format: `123456789:ABCdefGHI...`).

2. **Chat ID (`TG_CHAT_ID`):**
   * Find the [@userinfobot](https://t.me/userinfobot) or [@getmyid_bot](https://t.me/getmyid_bot) bot on Telegram.
   * Send it any message, and it will reply with your personal ID.
   * *To send messages to a group:* add the bot to the group, send a message in the group, and the bot will reply with the group's ID (usually starts with a minus, e.g., `-1001234567890`).

---

## 🛠️ Troubleshooting

1. **Container crashes with `Permission denied` or `device not found`:**
   * Make sure the modem is connected and the port exists (`ls /dev/ttyUSB*`).
   * Verify that the `root` user (or whoever runs Docker) has read/write permissions for the device. As a last resort, you can add `privileged: true` to `docker-compose.yml`, but mapping a specific device (`devices`) is safer.

2. **Modem returns errors to AT commands:**
   * Different modems may have AT commands on different ports. If `ttyUSB2` doesn't respond, try changing `SERIAL_PORT` to `/dev/ttyUSB0` or `/dev/ttyUSB1` (don't forget to update the `devices` mapping in the compose file).
   * The AT command port is usually identified via `dmesg | grep ttyUSB` (look for the port detected as "Modem" or "AT Port").

3. **SMS aren't arriving even though the modem is online:**
   * Make sure the SIM card is active, has balance, and the SMS memory isn't full (on some modems, you need to manually clear the storage).
   * Enable `LOG_LEVEL=DEBUG` and check the output of `docker compose logs` to see the modem's raw response to AT commands.