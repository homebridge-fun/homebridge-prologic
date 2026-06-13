import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';
import { fahrenheitToCelsius } from './settings';

export type TempSensorType = 'pool' | 'air';

export class TemperatureAccessory {
  private service: Service;
  private currentTempC = 20;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly sensorType: TempSensorType,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, `temp-${sensorType}`);

    this.service = this.accessory.getService(this.platform.Service.TemperatureSensor)
      ?? this.accessory.addService(this.platform.Service.TemperatureSensor);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    this.service.getCharacteristic(this.platform.Characteristic.CurrentTemperature)
      .onGet(this.handleGet.bind(this));
  }

  handleGet(): CharacteristicValue {
    return this.currentTempC;
  }

  updateTemperature(tempF: number | null): void {
    if (tempF === null) {
      return;
    }
    const c = fahrenheitToCelsius(tempF);
    if (this.currentTempC !== c) {
      this.currentTempC = c;
      this.service.updateCharacteristic(this.platform.Characteristic.CurrentTemperature, c);
    }
  }
}
