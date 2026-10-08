# Getting started

Your first Telegram alert in about ten minutes. This is the short, happy path: one
camera, person detection, Telegram. Nothing optional. When it works, add features one at
a time from the [configuration guide](configuration.md).

## What you need

- A supported Tapo camera (the [C560WS](../README.md#tested-cameras) is the reference; a
  C260 or C545D with an SD card also works) on the same network as the machine you run on.
- **An SD card in the camera.** The default detection source (`getEvents`) reads the
  camera's own detection list off the SD card; without one you get
  `STORAGE_NOT_EXIST` and no events. (Battery/hub cameras use a different path — see
  [battery cameras on a hub](battery-cameras-on-a-hub.md).)
- Python 3.10+ and `ffmpeg` on the host.
- A Telegram bot token and your chat id (create a bot with [@BotFather], send it a
  message, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`).
- Camera credentials created in the Tapo app. **This is the step newcomers get wrong** —
  read [credentials](credentials.md) first; it is five minutes that saves an afternoon
  (and a 30-minute login lockout).

## 1. Install

```bash
python3 -m venv ~/tapo-monitor/.venv
source ~/tapo-monitor/.venv/bin/activate
pip install tapo-monitor
```

## 2. Configuration

Start from the **minimal** config, not the full example:

```bash
curl -fsSLo cameras.yaml \
  https://raw.githubusercontent.com/PeterkoCZ91/tapo-monitoring/main/cameras.minimal.yaml
$EDITOR cameras.yaml      # set `host:` to your camera's IP and `name:` to a label
```

The file names environment variables; it never holds secrets. See
[`cameras.minimal.yaml`](../cameras.minimal.yaml).

## 3. Secrets

Put the real values in your shell (or a git-ignored `.env`). Which value is which is
explained in [credentials](credentials.md).

```bash
export TELEGRAM_TOKEN=...          # from @BotFather
export TELEGRAM_CHAT_ID=...        # your chat id
export CAM_USER=...                # Tapo "Camera Account" username
export CAM_PASSWORD=...            # Tapo "Camera Account" password
export CAM_RTSP_USER=...           # usually the same Camera Account
export CAM_RTSP_PASSWORD=...
```

## 4. Check, then run

```bash
tapo-monitor check cameras.yaml    # validates config + that every named secret is set;
                                   # changes nothing on the camera
tapo-monitor run   cameras.yaml    # starts the daemon in the foreground
```

`check` is safe to run as often as you like. Only run `run` once `check` is green —
repeated failed logins can trip a temporary camera lockout.

Walk in front of the camera. Within a few seconds you should get a Telegram message with
a snapshot. That is the whole core loop: the camera detects, the daemon picks the frame
where you are in view, and sends it.

## When it works

- Keep it running under systemd instead of a terminal: [operations](operations.md).
- Cut false alarms, add day/night, weather, PTZ tracking, local scoring, watch zones:
  the [configuration guide](configuration.md). Add one block at a time.
- Something not firing? [troubleshooting](troubleshooting.md) covers lockouts, missing
  frames and firmware quirks.

## If nothing arrives

- `tapo-monitor check cameras.yaml` is green? If not, it names the missing piece.
- Telegram silent but `check` passed: re-check `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`
  (send the bot a message first; a bot cannot start a chat with you).
- `STORAGE_NOT_EXIST` in the log: the camera has no SD card, so `getEvents` has nothing
  to read. Insert one, or see [battery cameras on a hub](battery-cameras-on-a-hub.md).
- `Invalid authentication data` or a lockout: wrong account or password — stop and read
  [credentials](credentials.md); do not keep retrying.

[@BotFather]: https://t.me/BotFather
