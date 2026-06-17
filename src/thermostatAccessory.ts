import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';
import { fahrenheitToCelsius, celsiusToFahrenheit } from './settings';

const MIN_TEMP_C = fahrenheitToCelsius(65);
const MAX_TEMP_C = fahrenheitToCelsius(104);

export interface ThermostatState {
  poolTempF: number | null;
  spaTempF: number | null;
  poolSetpointF: number | null;
  spaSetpointF: number | null;
  poolHeaterEnabled: boolean | null;
  spaHeaterEnabled: boolean | null;
  valveMode: 'pool' | 'spa' | null;
}

export type ThermostatBody = 'auto' | 'pool' | 'spa';

/**
 * §10 HomeKit thermostat accessories. One physical heater, two mode-driven
 * setpoints, exposed as three thermostats:
 *
 *   body = 'auto' → Accessory A: mode-following mirror. Points at whichever
 *                   setpoint is active for the current valve mode. Dynamic
 *                   name: "Heat — Pool" / "Heat — Spa".
 *   body = 'pool' → Accessory B: always the Pool setpoint. Name carries its
 *                   state: "Pool Heat — Heating/Standby/Off".
 *   body = 'spa'  → Accessory C: always the Spa setpoint. Same naming scheme.
 *
 * handleSetTarget writes the setpoint via menu navigation (§13.3).
 * handleSetMode toggles HEATER_1 (the single physical heater enable).
 */
export class ThermostatAccessory {
  private readonly service: Service;
  private currentTempC = fahrenheitToCelsius(70);
  private targetTempC = fahrenheitToCelsius(80);
  private heatingActive = false;
  private heaterEnabled = false;
  private currentName = '';

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly body: ThermostatBody,
  ) {
    const serials: Record<ThermostatBody, string> = {
      auto: 'heater-auto', pool: 'heater-pool', spa: 'heater-spa',
    };
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, serials[this.body]);

    this.service = this.accessory.getService(this.platform.Service.Thermostat)
      ?? this.accessory.addService(this.platform.Service.Thermostat);

    this.currentName = accessory.displayName;
    this.service.setCharacteristic(this.platform.Characteristic.Name, this.currentName);

    const { Characteristic: C } = this.platform;

    this.service.getCharacteristic(C.CurrentTemperature)
      .onGet(() => this.currentTempC);

    this.service.getCharacteristic(C.TargetTemperature)
      .setProps({ minValue: MIN_TEMP_C, maxValue: MAX_TEMP_C, minStep: 0.5 })
      .onGet(() => this.targetTempC)
      .onSet(this.handleSetTarget.bind(this));

    this.service.getCharacteristic(C.CurrentHeatingCoolingState)
      .onGet(() => this.heatingActive ? 1 : 0);

    this.service.getCharacteristic(C.TargetHeatingCoolingState)
      .setProps({ validValues: [0, 1] })
      .onGet(() => this.heaterEnabled ? 1 : 0)
      .onSet(this.handleSetMode.bind(this));

    // Display units: 1 = Fahrenheit (HomeKit internally always uses Celsius)
    this.service.getCharacteristic(C.TemperatureDisplayUnits).setValue(1);
  }

  /** Which physical body's setpoint this accessory currently reflects. */
  private targetBody(valveMode: 'pool' | 'spa' | null): 'pool' | 'spa' {
    if (this.body === 'auto') return valveMode ?? 'pool';
    return this.body;
  }

  async handleSetTarget(value: CharacteristicValue): Promise<void> {
    const c = value as number;
    const f = Math.round(celsiusToFahrenheit(c));
    this.targetTempC = c;

    const which = this.targetBody(this.platform.currentValveMode);
    this.platform.log.info(`[Thermostat ${this.body}] setpoint → ${f}°F (body: ${which})`);
    try {
      await this.platform.sidecar.setHeaterSetpoint(which, f);
    } catch (err) {
      this.platform.log.error(`[Thermostat ${this.body}] setpoint write failed:`, err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  async handleSetMode(value: CharacteristicValue): Promise<void> {
    const on = (value as number) !== 0;
    this.platform.log.info(
      `[Thermostat ${this.body}] mode → ${on ? 'Heat' : 'Off'} ` +
      '(HEATER_1 is the single physical heater enable for the active body)',
    );
    // Optimistically update so onGet returns the new value before the poll confirms.
    this.heaterEnabled = on;
    try {
      await this.platform.sidecar.setCircuit('HEATER_1', on);
    } catch (err) {
      this.heaterEnabled = !on; // revert on failure
      this.platform.log.error(`[Thermostat ${this.body}] mode set failed:`, err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  /** Compose the role-clear dynamic name (§10.1 / §10.2). */
  private composeName(s: ThermostatState, which: 'pool' | 'spa', enabled: boolean): string {
    const isCurrentMode = s.valveMode === which;
    if (this.body === 'auto') {
      return which === 'spa' ? 'Heat — Spa' : 'Heat — Pool';
    }
    const base = this.body === 'spa' ? 'Spa Heat' : 'Pool Heat';
    if (!enabled) return `${base} — Off`;
    return isCurrentMode ? `${base} — Heating` : `${base} — Standby`;
  }

  updateState(s: ThermostatState): void {
    const { Characteristic: C } = this.platform;
    const which = this.targetBody(s.valveMode);

    // Current temperature: the sensor for the body this accessory reflects
    const tempF = which === 'spa' ? s.spaTempF : s.poolTempF;
    if (tempF !== null) {
      const c = fahrenheitToCelsius(tempF);
      if (this.currentTempC !== c) {
        this.currentTempC = c;
        this.service.updateCharacteristic(C.CurrentTemperature, c);
      }
    }

    // Target setpoint
    const setpointF = which === 'spa' ? s.spaSetpointF : s.poolSetpointF;
    if (setpointF !== null) {
      const c = fahrenheitToCelsius(setpointF);
      if (this.targetTempC !== c) {
        this.targetTempC = c;
        this.service.updateCharacteristic(C.TargetTemperature, c);
      }
    }

    // Heating enabled for the reflected body
    const enabled = (which === 'spa' ? s.spaHeaterEnabled : s.poolHeaterEnabled) ?? false;

    // HomeKit has no "standby"; the current-heating-state field is HEAT only
    // when enabled AND this body is the active valve mode (§10.2). Otherwise OFF.
    const isActiveNow = enabled && (s.valveMode === which || this.body === 'auto');
    if (this.heatingActive !== isActiveNow) {
      this.heatingActive = isActiveNow;
      this.service.updateCharacteristic(C.CurrentHeatingCoolingState, isActiveNow ? 1 : 0);
    }
    if (this.heaterEnabled !== enabled) {
      this.heaterEnabled = enabled;
      this.service.updateCharacteristic(C.TargetHeatingCoolingState, enabled ? 1 : 0);
    }

    // Dynamic, role-clear name (§10.1 / §10.2)
    const name = this.composeName(s, which, enabled);
    if (name !== this.currentName) {
      this.currentName = name;
      this.service.updateCharacteristic(C.Name, name);
      const cn = (C as { ConfiguredName?: unknown }).ConfiguredName;
      if (cn) {
        this.service.updateCharacteristic(cn as Parameters<typeof this.service.updateCharacteristic>[0], name);
      }
    }
  }
}
