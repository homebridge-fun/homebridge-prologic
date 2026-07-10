import axios, { AxiosInstance } from 'axios';
import { PoolStatus } from './settings';

export interface SidecarClientOpts {
  /** Overrides host:port entirely — e.g. a Cloudflare Tunnel hostname for a
   * sidecar on a separate/isolated network. */
  baseUrl?: string;
  /** Cloudflare Access service-token credentials (CF-Access-Client-Id /
   * CF-Access-Client-Secret headers), for a baseUrl behind Access. */
  accessClientId?: string;
  accessClientSecret?: string;
}

export class SidecarClient {
  private readonly http: AxiosInstance;

  constructor(host: string, port: number, opts: SidecarClientOpts = {}) {
    const headers: Record<string, string> = {};
    if (opts.accessClientId) headers['CF-Access-Client-Id'] = opts.accessClientId;
    if (opts.accessClientSecret) headers['CF-Access-Client-Secret'] = opts.accessClientSecret;
    this.http = axios.create({
      baseURL: opts.baseUrl || `http://${host}:${port}`,
      // Menu navigation can take several seconds (multiple RS-485 round-trips).
      // A Cloudflare-tunneled sidecar adds edge round-trip latency on top of
      // that, so the same generous timeout comfortably covers both cases.
      timeout: 30000,
      headers,
    });
  }

  async getStatus(): Promise<PoolStatus> {
    const res = await this.http.get<PoolStatus>('/status');
    return res.data;
  }

  async setCircuit(name: string, on: boolean): Promise<void> {
    await this.http.post(`/circuit/${encodeURIComponent(name)}`, { on });
  }

  /**
   * Mirror the plugin's UI config (enabled circuits + label overrides) to the
   * sidecar so the web cockpit renders the same switches/labels as HomeKit.
   */
  async setUiConfig(circuits: string[], labels: Record<string, string>): Promise<void> {
    await this.http.post('/config/ui', { circuits, labels });
  }

  // ── Bridge health ─────────────────────────────────────────────────────────

  /**
   * Run a live active command-path probe on the AquaConnect box and return
   * whether it is wedged. This physically presses the canary output and checks
   * the equipment-state field actually moves, so it can take several seconds
   * (longer when wedged, since it retries). Updates the sidecar's cached flag
   * as a side effect.
   */
  async testBridge(): Promise<boolean> {
    const res = await this.http.get('/bridge/health', { params: { probe: 1 } });
    return Boolean((res.data as { bridge_wedged?: boolean })?.bridge_wedged);
  }

  // ── Backend selection ─────────────────────────────────────────────────────

  async getBackend(): Promise<{ active: string | null; config: Record<string, unknown> }> {
    const res = await this.http.get('/backend');
    return res.data;
  }

  /**
   * Switch the sidecar navigation backend. The sidecar persists the choice and
   * restarts itself (systemd) to apply, so this call may be followed by a brief
   * window where the sidecar is unreachable. No-op if already on that backend.
   */
  async setBackend(opts: {
    backend: 'aquaconnect' | 'rs485';
    aquaconnect_host?: string;
    rs485_host?: string;
    rs485_port?: number;
  }): Promise<void> {
    await this.http.post('/backend', opts);
  }

  // ── Heater setpoint (menu navigation) ─────────────────────────────────────

  /**
   * Write a heater setpoint via menu navigation. tempF is clamped to [65, 104]
   * by the sidecar. (Heater enable/disable goes through setCircuit('HEATER_1').)
   */
  async setHeaterSetpoint(which: 'pool' | 'spa', tempF: number): Promise<void> {
    await this.http.post(`/heater/${which}/setpoint`, { temp_f: Math.round(tempF) });
  }

  // ── Chlorinator (menu navigation) ────────────────────────────────────────

  async setChlorinatorPercent(which: 'pool' | 'spa', percent: number): Promise<void> {
    await this.http.post(`/chlorinator/${which}`, { percent: Math.round(percent) });
  }

  // ── Misc ──────────────────────────────────────────────────────────────────

  async setSuperChlorinate(on: boolean): Promise<void> {
    await this.http.post('/superchlorinate', { on });
  }
}
