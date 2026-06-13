import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';
import { fahrenheitToCelsius, celsiusToFahrenheit } from './settings';

const MIN_TEMP_C = fahrenheitToCelsius(65);
const MAX_TEMP_C = fahrenheitToCelsius(104);

export class ThermostatAccessory {
  private service: Service;
  private currentTempC = fahrenheitToCelsius(70);
  private targetTempC = fahrenheitToCelsius(80);
  private heatingActive = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, 'heater-thermostat');

    this.service = this.accessory.getService(this.platform.Service.Thermostat)
      ?? this.accessory.addService(this.platform.Service.Thermostat);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    const { Characteristic: C } = this.platform;

    // Current temperature — read-only, driven by pool temp sensor
    this.service.getCharacteristic(C.CurrentTemperature)
      .onGet(() => this.currentTempC);

    // Target temperature — writable, sends setpoint to controller
    this.service.getCharacteristic(C.TargetTemperature)
      .setProps({ minValue: MIN_TEMP_C, maxValue: MAX_TEMP_C, minStep: 0.5 })
      .onGet(() => this.targetTempC)
      .onSet(this.handleSetTarget.bind(this));

    // Current heating state — 0=off, 1=heating (no cooling on pool heater)
    this.service.getCharacteristic(C.CurrentHeatingCoolingState)
      .onGet(() => this.heatingActive ? 1 : 0);

    // Target heating state — expose Heat + Off only
    this.service.getCharacteristic(C.TargetHeatingCoolingState)
      .setProps({ validValues: [0, 1] })
      .onGet(() => this.heatingActive ? 1 : 0)
      .onSet(this.handleSetMode.bind(this));

    this.service.getCharacteristic(C.TemperatureDisplayUnits)
      .setValue(1); // Fahrenheit display (HomeKit still uses Celsius internally)
  }

  async handleSetTarget(value: CharacteristicValue): Promise<void> {
    const c = value as number;
    this.targetTempC = c;
    const f = Math.round(celsiusToFahrenheit(c));
    try {
      await this.platform.sidecar.setHeaterSetpoint(f);
    } catch (err) {
      this.platform.log.error('[Thermostat] setpoint failed:', err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  async handleSetMode(value: CharacteristicValue): Promise<void> {
    const on = (value as number) !== 0;
    try {
      await this.platform.sidecar.setCircuit('HEATER_1', on);
    } catch (err) {
      this.platform.log.error('[Thermostat] mode set failed:', err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  updateState(poolTempF: number | null, setpointF: number | null, heaterOn: boolean): void {
    const { Characteristic: C } = this.platform;

    if (poolTempF !== null) {
      const c = fahrenheitToCelsius(poolTempF);
      if (this.currentTempC !== c) {
        this.currentTempC = c;
        this.service.updateCharacteristic(C.CurrentTemperature, c);
      }
    }

    if (setpointF !== null) {
      const c = fahrenheitToCelsius(setpointF);
      if (this.targetTempC !== c) {
        this.targetTempC = c;
        this.service.updateCharacteristic(C.TargetTemperature, c);
      }
    }

    if (this.heatingActive !== heaterOn) {
      this.heatingActive = heaterOn;
      this.service.updateCharacteristic(C.CurrentHeatingCoolingState, heaterOn ? 1 : 0);
      this.service.updateCharacteristic(C.TargetHeatingCoolingState, heaterOn ? 1 : 0);
    }
  }
}
