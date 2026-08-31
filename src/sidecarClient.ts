import axios, { AxiosInstance } from 'axios';
import { PoolStatus } from './settings';

export class SidecarClient {
  private readonly http: AxiosInstance;

  constructor(host: string, port: number) {
    this.http = axios.create({
      baseURL: `http://${host}:${port}`,
      // Menu navigation can take several seconds (multiple RS-485 round-trips),
      // so keep a generous timeout.
      timeout: 30000,
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
   * Mirror the plugin's UI config to the sidecar: enabled circuits, label
   * overrides, and which light standard sits on which relay (that mapping
   * varies per installation and drives how scenes are selected).
   */
  async setUiConfig(
    circuits: string[],
    labels: Record<string, string>,
    lights?: Record<string, { type: string; circuit: string }>,
  ): Promise<void> {
    await this.http.post('/config/ui', { circuits, labels, lights });
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
    backend: 'aquaconnect' | 'rs485bridge';
    aquaconnect_host?: string;
    rs485bridge_host?: string;
    rs485bridge_port?: number;
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

  // ── Lights ────────────────────────────────────────────────────────────────

  /** The named ColorLogic/IntelliBrite scenes for a body (spa=Pentair 12,
   * pool=Hayward UCL 17), used to build the HomeKit input-source list. */
  async getLightPrograms(body: 'pool' | 'spa'): Promise<LightProgram[]> {
    const res = await this.http.get<{ programs?: LightProgram[] }>(
      `/lights/programs?body=${body}`);
    return res.data.programs ?? [];
  }

  /** Select a scene by number (1..N). The sidecar power-cycles the light using
   * the saved per-body calibration; open-loop (no confirmation). */
  async setLightProgram(body: 'pool' | 'spa', program: number): Promise<void> {
    await this.http.post(`/lights/${body}/program`, { program });
  }

  // ── Misc ──────────────────────────────────────────────────────────────────

  async setSuperChlorinate(on: boolean): Promise<void> {
    await this.http.post('/superchlorinate', { on });
  }
}

export interface LightProgram {
  n: number;
  name: string;
  type: 'show' | 'fixed';
}
