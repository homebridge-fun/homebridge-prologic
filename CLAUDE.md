# Working with this repo

## Response style
- When giving terminal commands, always provide **complete, runnable blocks**
  that can be copy-pasted in one go — not many small one-line snippets.
- The Homebridge UI terminal runs *inside* the Homebridge process, so
  `sudo systemctl restart homebridge` kills that session. Keep any
  Homebridge-restart command in a **separate block** labeled for an SSH /
  external terminal, distinct from the block run in the Homebridge terminal.

## Deploy layout
- Dev checkout: `/home/greg/development/homebridge-prologic`
- Sidecar runs from `/opt/pool-sidecar/pool_service.py` (systemd: `pool-sidecar`)
- Plugin `dist/` is the same directory as
  `/var/lib/homebridge/node_modules/homebridge-prologic/dist/` (no `cp dist/*`
  needed after `npm run build`).

### Standard deploy
Block 1 — Homebridge terminal:
```bash
cd /home/greg/development/homebridge-prologic
git pull origin claude/gracious-planck-1yz8v9
sudo cp sidecar/pool_service.py /opt/pool-sidecar/pool_service.py
sudo systemctl restart pool-sidecar
npm run build
```
Block 2 — SSH / external terminal:
```bash
sudo systemctl restart homebridge
```
