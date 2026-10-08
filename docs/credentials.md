# Camera accounts and credentials

Getting the credentials right is the single most common thing newcomers get wrong, and a
wrong guess can trip a **temporary login lockout**. Spend five minutes here first.

## A Tapo camera has two separate logins

| Login | What it is | Set where | Used for |
| --- | --- | --- | --- |
| **TP-Link cloud account** | the email + password you sign into the Tapo app with | tplinkcloud.com / the app | binding the camera, and the "cloud password" some flows need (e.g. the SD-card download) |
| **Camera Account** (a.k.a. third-party / ONVIF account) | a username + password **you create on the camera** | Tapo app → camera → **Settings → Advanced Settings → Camera Account** (enable *Third-Party Compatibility* if the model asks) | the local control API (pytapo), RTSP and ONVIF — i.e. everything this daemon uses day to day |

They are **not** the same account. The daemon logs in with the **Camera Account**; the
cloud account only comes back for the optional SD-card download.

> On many models the Camera Account username is effectively fixed as `admin` and only the
> password is yours to set; newer firmware lets you choose the username. If a login fails,
> try the username exactly as shown in the app, then `admin`.

## Which value goes in which variable

The config file (`cameras.yaml`) holds environment-variable **names**; the real secrets
live in your shell or a git-ignored `.env`. Default variable names:

| Env var | Put the… | Notes |
| --- | --- | --- |
| `CAM_USER` / `CAM_PASSWORD` | Camera Account username / password | the daemon's control login |
| `CAM_RTSP_USER` / `CAM_RTSP_PASSWORD` | Camera Account username / password | for RTSP snapshots; usually the **same** as above |
| `CAM_CLOUD_PASSWORD` | TP-Link **cloud** account password | optional; only for the SD-card follow-up. Omit it and it defaults to `CAM_PASSWORD` — fine unless the SD download returns an auth error |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | bot token / chat id | not a camera credential |

More than one camera on different accounts? Give each its own variable names in the
config (`user_env:`, `password_env:`, … per camera) and export each one.

## Verify before you run — the lockout rule

Repeated failed logins trigger a firmware lockout. On some models it is an explicit
**"Temporary Suspension: try again in 1800 seconds"** (30 minutes) on the control API; the
C560WS can also lock by **source IP** after frequent re-logins (which is why the daemon's
`control_interval` keeps logins infrequent — see [configuration](configuration.md)).

So:

1. Enable third-party compatibility and create the Camera Account in the app.
2. Confirm the camera is reachable: `ping <camera-ip>`.
3. Run `tapo-monitor check cameras.yaml` — it validates that every named secret is set
   **without** hammering the camera.
4. If a login fails, **stop and fix the account/password — do not retry in a loop.** One
   or two careful attempts are fine; a dozen wrong ones buy you a 30-minute wait.

## RTSP, quickly

A quick way to confirm the RTSP (Camera Account) credentials outside the daemon:

Set `CAM_RTSP_URL` in your local environment to the camera's authenticated stream
URL. Keep that value out of configuration examples, logs and shell history.

```bash
ffprobe -rtsp_transport tcp \
  "$CAM_RTSP_URL"
```

`401 Unauthorized` means wrong RTSP username or password (remember: the Camera Account,
not the cloud login). A clean stream description means you are set.

## See also

- [Getting started](getting-started.md) — the end-to-end first-alert walkthrough.
- [Troubleshooting](troubleshooting.md) — lockouts, missing frames, firmware quirks.
- [Local API reference](tapo-local-api.md) — error codes (`-40209` invalid login, etc.)
  and the calls that take a camera's API down.
