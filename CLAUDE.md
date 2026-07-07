# Working with this repo

## Response style
- When giving terminal commands, always provide **complete, runnable blocks**
  that can be copy-pasted in one go — not many small one-line snippets.
- The Homebridge UI terminal runs *inside* the Homebridge process, so
  `sudo systemctl restart homebridge` kills that session. That's fine as the
  **last command** in a block — put the restart at the end and it still
  completes. Only use a **second block** when something must run *after* the
  restart (those commands wouldn't survive the killed session); label that
  second block for an SSH / external terminal.

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
git stash
git pull --rebase origin claude/gracious-planck-1yz8v9
sudo cp sidecar/pool_service.py /opt/pool-sidecar/pool_service.py
sudo systemctl restart pool-sidecar
npm run build
```
Block 2 — SSH / external terminal:
```bash
sudo systemctl restart homebridge
```
