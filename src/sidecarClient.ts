import axios, { AxiosInstance } from 'axios';
import { PoolStatus } from './settings';

export interface HeaterState {
  which: 'pool' | 'spa';
  enabled: boolean;
  setpoint_f: number | null;
  raw: string;
}

export interface VspSlot4 {
  slot: number;
  speed_pct: number;
}

export class SidecarClient {
  private readonly http: AxiosInstance;

  constructor(host: string, port: number) {
    this.http = axios.create({
      baseURL: `http://${host}:${port}`,
      // Menu navigation can take several seconds (multiple RS-485 round-trips).
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

  // ── Heater setpoints (menu navigation) ────────────────────────────────────

  async getHeaterState(which: 'pool' | 'spa'): Promise<HeaterState> {
    const res = await this.http.get<HeaterState>(`/heater/${which}/state`);
    return res.data;
  }

  /**
   * Write a heater setpoint via menu navigation (§13.3 restore-to-prior-state).
   * tempF is clamped to [65, 104] by the sidecar.
   */
  async setHeaterSetpoint(which: 'pool' | 'spa', tempF: number): Promise<void> {
    await this.http.post(`/heater/${which}/setpoint`, { temp_f: Math.round(tempF) });
  }

  /** Enable/disable a heater (Auto vs Manual Off) via menu navigation. */
  async setHeaterEnabled(which: 'pool' | 'spa', on: boolean): Promise<void> {
    await this.http.post(`/heater/${which}/enable`, { on });
  }

  // ── VSP slots 1–4 (menu navigation + FILTER activation) ─────────────────

  async getAllVspSlots(): Promise<{ slots: Record<string, number> }> {
    const res = await this.http.get('/vsp/slots');
    return res.data;
  }

  async getVspSlot(slot: number): Promise<{ slot: number; speed_pct: number }> {
    const res = await this.http.get(`/vsp/slot/${slot}`);
    return res.data;
  }

  async setVspSlot(slot: number, speedPct: number): Promise<void> {
    await this.http.post(`/vsp/slot/${slot}`, { speed_pct: Math.round(speedPct) });
  }

  async activateVspSlot(slot: number): Promise<void> {
    await this.http.post(`/vsp/slot/${slot}/activate`);
  }

  // Legacy slot-4 wrappers used by FanAccessory.
  async getVspSlot4(): Promise<VspSlot4> {
    const res = await this.http.get<VspSlot4>('/vsp/slot4');
    return res.data;
  }

  async setVspSlot4(speedPct: number): Promise<void> {
    await this.http.post('/vsp/slot4', { speed_pct: Math.round(speedPct) });
  }

  /** Cycle FILTER off→on to open slot-selection window and select slot 4 (§6.2). */
  async activateVspSlot4(): Promise<void> {
    await this.http.post('/vsp/slot4/activate');
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
