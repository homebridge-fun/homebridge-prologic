import type { PlatformAccessory, Service } from 'homebridge';
import type { ProLogicPlatform } from './platform';

export class SaltSensorAccessory {
  private readonly service: Service;
  private currentPpm = 0;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, 'salt-sensor');

    this.service = this.accessory.getService(this.platform.Service.AirQualitySensor)
      ?? this.accessory.addService(this.platform.Service.AirQualitySensor);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    // Pin quality to Excellent so the tile never shows a warning colour —
    // we're using the PPM field purely as a numeric display for salt level.
    this.service.setCharacteristic(
      this.platform.Characteristic.AirQuality,
      this.platform.Characteristic.AirQuality.EXCELLENT,
    );

    // VOCDensity defaults to a maxValue of 1000 per the HAP spec, which clamps
    // saltwater pool levels (typically 2700–3500 PPM) down to 1000. Raise the
    // max with setProps so the full salt range comes through unclamped.
    this.service.getCharacteristic(this.platform.Characteristic.VOCDensity)
      .setProps({ minValue: 0, maxValue: 10000 })
      .onGet(() => this.currentPpm);
  }

  updateSaltLevel(ppm: number | null): void {
    if (ppm === null) return;
    const rounded = Math.round(ppm);
    if (this.currentPpm !== rounded) {
      this.currentPpm = rounded;
      this.service.updateCharacteristic(this.platform.Characteristic.VOCDensity, rounded);
    }
  }
}
