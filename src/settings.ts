export const PLATFORM_NAME = 'ProLogic';
export const PLUGIN_NAME = 'homebridge-prologic';

export const CIRCUITS = [
  'POOL',
  'SPA',
  'FILTER',
  'LIGHTS',
  'SPILLOVER',
  'AUX_1',
  'AUX_2',
  'HEATER_1',
  'SUPER_CHLORINATE',
] as const;

export type Circuit = typeof CIRCUITS[number];

export interface PoolStatus {
  circuits: Record<Circuit, boolean>;
  pool_temp: number | null;
  air_temp: number | null;
  spa_temp: number | null;
  salt_level: number | null;
  chlorinator_percent: number | null;
  pump_speed: number | null;
  // populated by menu navigator reads; null = not yet read
  pool_setpoint_f: number | null;
  spa_setpoint_f: number | null;
  pool_heater_enabled: boolean | null;
  spa_heater_enabled: boolean | null;
  heater_active: boolean | null;  // relay firing right now
  valve_mode: 'pool' | 'spa' | null;
  vsp_slot_pct: Record<string, number>;  // keys "1"–"4"
  vsp_active_slot: number | null;
  connected: boolean;
  last_update: number;
  bridge_wedged: boolean;
}

export interface PlatformConfig {
  name: string;
  sidecarHost: string;
  sidecarPort: number;
  pollInterval: number;
  backend: 'aquaconnect' | 'rs485';
  aquaconnectHost: string;
  rs485Host: string;
  rs485Port: number;
  circuits: Circuit[];
  activeBodies: ('pool' | 'spa' | 'spillover')[];
  enableActiveHeaterThermostat: boolean;
  enablePoolHeaterThermostat: boolean;
  enableSpaHeaterThermostat: boolean;
  enableTemperatureSensors: boolean;
  enableSpaModeSwitch: boolean;
  enableChlorinatorFan: boolean;
  enablePumpSpeedFan: boolean;
  enableSaltSensor: boolean;
  enableVspSlotTiles: boolean;
  enableHeaterFan: boolean;
  circuitLabels: Partial<Record<Circuit, string>>;
}

export function celsiusToFahrenheit(c: number): number {
  return c * 9 / 5 + 32;
}

export function fahrenheitToCelsius(f: number): number {
  return Math.round(((f - 32) * 5 / 9) * 10) / 10;
}
