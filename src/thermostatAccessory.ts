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

/**
 * §10 HomeKit thermostat accessories:
 *   body = 'auto'  → Accessory A: mode-following mirror.  Shows whichever
 *                    setpoint is active for the current valve mode.
 *   body = 'spa'   → Accessory C: dedicated spa setpoint regardless of mode.
 *
 * handleSetTarget writes the setpoint via menu navigation (§13.3).
 * handleSetMode toggles HEATER_1 (auto) or logs a note (spa, requires spa mode).
 */
export class ThermostatAccessory {
  private readonly service: Service;
  private currentTempC = fahrenheitToCelsius(70);
  private targetTempC = fahrenheitToCelsius(80);
  private heatingActive = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly body: 'auto' | 'spa',
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber,
        body === 'spa' ? 'heater-spa' : 'heater-auto');

    this.service = this.accessory.getService(this.platform.Service.Thermostat)
      ?? this.accessory.addService(this.platform.Service.Thermostat);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

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
      .onGet(() => this.heatingActive ? 1 : 0)
      .onSet(this.handleSetMode.bind(this));

    // Display units: 1 = Fahrenheit (HomeKit internally always uses Celsius)
    this.service.getCharacteristic(C.TemperatureDisplayUnits).setValue(1);
  }

  private activeBody(valveMode: 'pool' | 'spa' | null): 'pool' | 'spa' {
    if (this.body === 'spa') return 'spa';
    return valveMode ?? 'pool';   // default to pool if mode unknown
  }

  async handleSetTarget(value: CharacteristicValue): Promise<void> {
    const c = value as number;
    const f = Math.round(celsiusToFahrenheit(c));
    this.targetTempC = c;

    const which = this.activeBody(this.platform.currentValveMode);
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
    if (this.body === 'spa') {
      this.platform.log.info(
        `[Thermostat spa] mode → ${on ? 'Heat' : 'Off'} ` +
        '(HEATER_1 controls the active body; ensure Spa mode is selected first)',
      );
    }
    try {
      await this.platform.sidecar.setCircuit('HEATER_1', on);
    } catch (err) {
      this.platform.log.error(`[Thermostat ${this.body}] mode set failed:`, err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  updateState(s: ThermostatState): void {
    const { Characteristic: C } = this.platform;
    const which = this.activeBody(s.valveMode);

    // Current temperature: use the appropriate sensor for this body
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

    // Heating active: whether the appropriate heater is enabled
    const enabled = (which === 'spa' ? s.spaHeaterEnabled : s.poolHeaterEnabled) ?? false;
    if (this.heatingActive !== enabled) {
      this.heatingActive = enabled;
      this.service.updateCharacteristic(C.CurrentHeatingCoolingState, enabled ? 1 : 0);
      this.service.updateCharacteristic(C.TargetHeatingCoolingState, enabled ? 1 : 0);
    }
  }
}
