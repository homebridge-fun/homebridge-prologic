# Working with this repo

## Git workflow — branch discipline
- **Never edit code, or commit, directly on `main`.** All code changes happen
  on a branch.
- **If not already on a branch when a code change is needed, ASK first** which
  branch to use / whether to create a new one — don't just create one silently.
  Once confirmed, create it off latest `main` and make all edits there.
- **Never merge/push a branch into `main`, and never commit directly to `main`,
  without an explicit command from the user to do so in that moment.** Finishing
  a fix and pushing the feature branch is NOT itself permission to merge —
  wait to be told (e.g. "bring it back to main", "merge it").
- This applies to every code change, however small — docs-only edits to files
  like this one follow the same rule unless the user says otherwise.

## Response style
- **Always say WHERE every command runs.** This is a multi-machine setup — the
  **hop** (Homebridge host: dev checkout, `pool-sidecar`, cockpit), the **pad**
  (Pi Zero at the equipment pad: `pool-bridge`, `rs485_bridge.py`, reached via
  `ssh <user>@pool`), and sometimes an **SSH/external terminal**. Label every
  command block with the machine it runs on (e.g. "On the PAD:", "On the HOP:").
  A block referencing a repo file (e.g. `deploy/harden-pad.sh`) only works after
  that machine has pulled the code — include the `git pull` step if needed.
- When giving terminal commands, always provide **complete, runnable blocks**
  that can be copy-pasted in one go — not many small one-line snippets.
- **Put all code/commands in fenced code blocks, never inline.** Even a single
  command goes in a ```block``` (so it's copy-pasteable and unambiguous), not in
  backticks mid-sentence.
- The Homebridge UI terminal runs *inside* the Homebridge process, so
  `sudo systemctl restart homebridge` kills that session. That's fine as the
  **last command** in a block — put the restart at the end and it still
  completes. Only use a **second block** when something must run *after* the
  restart (those commands wouldn't survive the killed session); label that
  second block for an SSH / external terminal.

## Deploy layout
- Dev checkout: `/home/greg/development/homebridge-prologic`
- Sidecar runs from `/opt/pool-sidecar/pool_service.py` (systemd: `pool-sidecar`)
- **Sidecar interpreter is a venv**: `/opt/pool-sidecar/venv/bin/python`
  (has Flask + aqualogic; the system `python3` does NOT). Use it to run the
  sidecar or a test instance by hand.
- Plugin `dist/` is the same directory as
  `/var/lib/homebridge/node_modules/homebridge-prologic/dist/` (no `cp dist/*`
  needed after `npm run build`).

### Pad-Pi RS-485 smart bridge
- A separate Pi Zero 2 W at the pad runs the direct-serial bridge daemon
  (`sidecar/rs485_bridge.py`, systemd: `pool-bridge`), reached from the hop over
  Tailscale. Setup + re-image runbook: `deploy/README-PAD.md`. The hop sidecar
  talks to it via the `rs485bridge` backend.

### Running a test sidecar instance (no prod impact)
Use the venv python, an isolated config, and a spare API port; point it at the
pad bridge by MagicDNS name `pool` (works now that the hop accepts tailnet DNS —
`getent hosts pool` resolves; MagicDNS name survives a tailnet-IP change) or by
tailnet IP (`100.113.118.4`):
```bash
SIDECAR_CONFIG=/tmp/bridge-test.json /opt/pool-sidecar/venv/bin/python \
  sidecar/pool_service.py --backend rs485bridge \
  --rs485bridge-host pool --api-port 5758
```
`SIDECAR_CONFIG` isolates `backend.json` so the test can't read/clobber the
production `/opt/pool-sidecar/backend.json`.

### Standard deploy
Single block — Homebridge terminal. Put `restart homebridge` **last**: it kills
this session but still completes, so it belongs at the end of the same block.
Only add a **second** block when something must run *after* the restart (label
it for an SSH / external terminal).
```bash
cd /home/greg/development/homebridge-prologic
git stash
git pull --rebase origin <current-branch>
sudo cp sidecar/pool_service.py /opt/pool-sidecar/pool_service.py
sudo cp sidecar/web/index.html /opt/pool-sidecar/web/index.html
sudo systemctl restart pool-sidecar
npm run build
sudo systemctl restart homebridge
```
