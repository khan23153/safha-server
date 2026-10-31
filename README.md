# safha-server

Free Oracle Cloud server for the [Safha](https://github.com/khan23153/qari) backend.

Oracle's Always Free Ampere A1 in Mumbai often answers **"Out of host capacity"**.
This repo keeps trying automatically, then sets the server up.

| File | What it does |
|---|---|
| `.github/workflows/grab-a1.yml` | Every 5 minutes, tries to create the server. On success opens an issue with the IP and disables itself. |
| `launch_a1.py` | Creates `safha-server` (VM.Standard.A1.Flex, 2 OCPU / 12 GB, 100 GB, Ubuntu 24.04 arm64) in `ap-mumbai-1`, plus its network on first use. Capacity errors are a quiet retry; never creates a duplicate. |
| `cloud-init.sh` | First boot: Docker, swap, clones `qari` and this repo, downloads the live engine (hash-checked) and, with a Kaggle token, the private Whisper models. |
| `deploy.sh` | Starts the backend (compose stack, Caddy edge, Cloudflare Tunnel) and waits for health. |

## Setup

1. Oracle console → profile → **My profile → API keys → Add API key** → generate, download the private key, **Add**.
2. Repo **Settings → Secrets and variables → Actions**, add:
   `OCI_USER_OCID`, `OCI_TENANCY_OCID`, `OCI_FINGERPRINT`, `OCI_PRIVATE_KEY` (the .pem content),
   `OCI_SSH_PUBLIC_KEY`, and optionally `KAGGLE_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   (Telegram: a silent message after every attempt, a ringing one when the server is created or a run fails).
3. Wait for the "Oracle A1 server ready" issue. Then on the server:
   `CLOUDFLARE_TUNNEL_TOKEN=... /opt/safha-server/deploy.sh`

Run the tests: `pip install oci pytest && pytest tests`.
