# Caddy LAN front-end for the Pool Cockpit

Stock Caddy (official apt repo) reverse-proxies the localhost cockpit onto the
LAN behind HTTP Basic auth. The sidecar stays bound to `127.0.0.1:5757`, so the
only thing the LAN can reach is Caddy — not the custom Flask app. Tailscale
(`tailscale serve`) remains the remote-access path and is unaffected.

## Why this shape
- **Pi attack surface:** the sidecar never faces the network. Caddy — a widely
  used, audited, apt-maintained binary — is the only LAN listener.
- **Maintenance:** stock Caddy installs from Cloudflare/Cloudsmith's official
  apt repo, so it updates with your normal `apt upgrade`. No custom build.
- **Trade-off:** Basic auth has no persistent session, so an iOS pinned web app
  re-prompts on cold launches. If that becomes annoying, the persistent-login
  options are caddy-security (manual binary lifecycle) or Cloudflare Access.

## Install (SSH / external terminal — not the Homebridge UI terminal)

```bash
# 1. Add Caddy's official apt repo + key, then install.
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy

# 2. Create the password hash (copy the $2a$... output).
caddy hash-password --plaintext 'CHANGE-ME'

# 3. Install the Caddyfile and paste your hash into it.
sudo cp /home/greg/development/homebridge-prologic/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile        # replace REPLACE_WITH_YOUR_BCRYPT_HASH

# 4. Reload Caddy.
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
```

Then browse to `http://<pi-lan-ip>/` and you'll get the Basic-auth prompt, then
the cockpit.

## Optional: encrypt the Basic credentials (internal CA)
Plain HTTP sends the Basic credentials base64-encoded over the LAN. On a trusted
home network that's usually fine, but to encrypt:

1. In the Caddyfile, change the site address from `http://` to a hostname,
   e.g. `https://pool.lan`, and add `tls internal` inside the block.
2. Add `pool.lan` → the Pi's LAN IP in your router/hosts DNS.
3. Export Caddy's root CA and install + trust it on each device (iOS: Settings →
   General → VPN & Device Management → install profile, then Certificate Trust
   Settings → enable full trust):
   ```bash
   sudo cat /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
   ```

## Updates
Caddy updates with the rest of the system:
```bash
sudo apt update && sudo apt upgrade
```
No held packages, no custom binary — this is the whole reason we used the stock
apt build instead of caddy-security.
