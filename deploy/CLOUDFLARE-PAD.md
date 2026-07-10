# Cloudflare Tunnel for the pad-mounted sidecar (Pi Zero 2 W)

Architecture: the sidecar runs directly on a Pi Zero 2 W mounted at the pool
pad, connected to the panel via an isolated USB-RS485 adapter (direct serial —
no TCP bridge, which is what makes writes reliable; see
`docs/aqualogic-automation-spec.md` §0). This Pi lives on an **isolated/guest
WiFi network** for defense-in-depth (it has direct physical control of pool
equipment), which means it has **no LAN reachability** from `homebridge-hop` or
your phone. Cloudflare Tunnel bridges that gap: outbound-only from the pad Pi,
no inbound port anywhere, authenticated via Cloudflare Access.

Two consumers need to reach the pad Pi's sidecar through the tunnel:
- **Homebridge** (on `homebridge-hop`) — polls `/status` and sends commands.
  Authenticates with a Cloudflare Access **service token** (see
  `sidecarBaseUrl` / `sidecarAccessClientId` / `sidecarAccessClientSecret` in
  the plugin config — wired up in `src/sidecarClient.ts`).
- **You** (browser/cockpit) — authenticate interactively via Access (email
  one-time PIN), same as any Cloudflare Access app.

## Prerequisites
- A domain already on Cloudflare (confirmed available).
- The pad Pi reachable via SSH for initial setup (put it on your **main** LAN
  first — see the top-level conversation notes; move it to the isolated
  network only after the tunnel is proven working).
- The sidecar itself running and bound to `127.0.0.1:5757` (same default as
  the main install — no code difference needed here).

## 1. Install cloudflared (official apt repo — stays in your normal `apt upgrade`)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://pkg.cloudflare.com/cloudflare-main.gpg' \
  | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-main-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main-archive-keyring.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update
sudo apt install -y cloudflared
```

## 2. Authenticate + create the tunnel

```bash
cloudflared tunnel login   # opens a URL — approve it in your browser, pick your domain

cloudflared tunnel create pool-pad
# note the tunnel ID / credentials file path it prints
```

## 3. Config file

```bash
sudo mkdir -p /etc/cloudflared
sudo tee /etc/cloudflared/config.yml >/dev/null <<'EOF'
tunnel: pool-pad
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json   # from step 2 — replace <TUNNEL_ID>

ingress:
  - hostname: pool-pad.yourdomain.com          # replace with your actual domain
    service: http://127.0.0.1:5757
  - service: http_status:404
EOF
```

## 4. DNS route + run as a service

```bash
cloudflared tunnel route dns pool-pad pool-pad.yourdomain.com
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager   # confirm it's running
```

## 5. Cloudflare Access application

In the Cloudflare Zero Trust dashboard → **Access → Applications → Add an application** → Self-hosted:
- Domain: `pool-pad.yourdomain.com`
- **Policy 1 (you, interactive)**: Allow → your email, authenticate via **One-Time PIN**. Set session duration long (e.g. 1 month) so you rarely re-log-in from the cockpit.
- **Policy 2 (Homebridge, machine-to-machine)**: create a **Service Token** (Access → Service Tokens → Create Service Token, name it `homebridge`). Add an Allow policy on the same application for that service token. Copy the generated **Client ID** and **Client Secret** — you won't see the secret again.

## 6. Point the Homebridge plugin at it

In the plugin config (Homebridge UI → this plugin's settings, "advanced" fields):
- **Sidecar Base URL**: `https://pool-pad.yourdomain.com`
- **Cloudflare Access Client ID**: the service token's Client ID from step 5
- **Cloudflare Access Client Secret**: the service token's Client Secret from step 5

These override `sidecarHost`/`sidecarPort` entirely (see `src/settings.ts` /
`src/sidecarClient.ts`) and attach `CF-Access-Client-Id` /
`CF-Access-Client-Secret` headers to every request, which is what lets the
service token through without an interactive login.

Restart Homebridge and confirm the accessories update — that proves
Homebridge is reaching the pad Pi entirely through the tunnel.

## 7. Only after all of the above is confirmed working: move the Pi to the isolated network

Reconfigure its WiFi to the guest/isolated SSID (`sudo raspi-config` → System
Options → Wireless LAN, or edit the NetworkManager connection). Everything
above keeps working unchanged — the tunnel is outbound-only, so it doesn't
care which network it's dialing out from, and Homebridge/you never need LAN
reachability to it again.

## Updates
`cloudflared` updates with your normal routine: `sudo apt update && sudo apt upgrade`.
No held packages, no custom binary — same reasoning as the Caddy setup on the
other Pi (`deploy/CADDY.md`).
