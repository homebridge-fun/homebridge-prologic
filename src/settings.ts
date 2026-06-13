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
  pool_temp: number | null;       // °F from controller
  air_temp: number | null;        // °F from controller
  heater_setpoint: number | null; // °F
  salt_level: number | null;      // ppm
  chlorinator_percent: number | null;
}

export interface PlatformConfig {
  name: string;
  sidecarHost: string;
  sidecarPort: number;
  pollInterval: number;
  circuits: Circuit[];
  enablePoolHeaterThermostat: boolean;
  enableTemperatureSensors: boolean;
}

export function celsiusToFahrenheit(c: number): number {
  return c * 9 / 5 + 32;
}

export function fahrenheitToCelsius(f: number): number {
  return Math.round(((f - 32) * 5 / 9) * 10) / 10;
}
