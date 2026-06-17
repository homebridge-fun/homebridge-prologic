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

  // ── VSP slot 4 (menu navigation + FILTER activation) ─────────────────────

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
