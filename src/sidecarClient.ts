import axios, { AxiosInstance } from 'axios';
import { PoolStatus } from './settings';

export class SidecarClient {
  private readonly http: AxiosInstance;

  constructor(host: string, port: number) {
    this.http = axios.create({
      baseURL: `http://${host}:${port}`,
      timeout: 4000,
    });
  }

  async getStatus(): Promise<PoolStatus> {
    const res = await this.http.get<PoolStatus>('/status');
    return res.data;
  }

  async setCircuit(name: string, on: boolean): Promise<void> {
    await this.http.post(`/circuit/${encodeURIComponent(name)}`, { on });
  }

  async setHeaterSetpoint(tempF: number): Promise<void> {
    await this.http.post('/heater/setpoint', { temp: tempF });
  }

  async setChlorinatorPercent(percent: number): Promise<void> {
    await this.http.post('/chlorinator', { percent });
  }

  async setSuperChlorinate(on: boolean): Promise<void> {
    await this.http.post('/superchlorinate', { on });
  }
}
