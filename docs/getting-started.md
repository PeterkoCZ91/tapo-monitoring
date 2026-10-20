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

Export the real values in your shell. A git-ignored `.env` can store them, but the
CLI does not load that file automatically; load it into the environment before running.
Which value is which is
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
tapo-monitor check cameras.yaml    # validates YAML configuration; no camera requests
tapo-monitor selfcheck cameras.yaml # checks host prerequisites and control credentials
tapo-monitor run   cameras.yaml    # starts the daemon in the foreground
```

`check` validates the schema; it does not verify secret values. `selfcheck` checks
host prerequisites and configured control credentials, but does not authenticate
Telegram or RTSP. Confirm those values before starting `run`; repeated failed
camera logins can trip a temporary lockout.

Walk in front of the camera and wait for a Telegram snapshot. Delay depends on event
polling, frame availability and the delivery path; card follow-ups can take minutes.
That is the whole core loop: the camera detects, the daemon picks the frame
where you are in view, and sends it.

## When it works

- Keep it running under systemd instead of a terminal: [operations](operations.md).
- Cut false alarms, add day/night, weather, PTZ tracking, local scoring, watch zones:
  the [configuration guide](configuration.md). Add one block at a time.
- Something not firing? [troubleshooting](troubleshooting.md) covers lockouts, missing
  frames and firmware quirks.

## If nothing arrives

- `tapo-monitor check cameras.yaml` validates the configuration; `selfcheck` identifies
  missing host prerequisites or control credentials. A green result is not an
  end-to-end connectivity test.
- Telegram silent but `check` passed: re-check `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`
  (send the bot a message first; a bot cannot start a chat with you).
- `STORAGE_NOT_EXIST` in the log: the camera has no SD card, so `getEvents` has nothing
  to read. Insert one, or see [battery cameras on a hub](battery-cameras-on-a-hub.md).
- `Invalid authentication data` or a lockout: wrong account or password, or a camera that
  switched to the newer TPAP login — stop and read [credentials](credentials.md)
  and [troubleshooting](troubleshooting.md#invalid-authentication-data); do not keep retrying.

[@BotFather]: https://t.me/BotFather
