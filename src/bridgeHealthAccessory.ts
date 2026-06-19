import type { PlatformAccessory, Service } from 'homebridge';
import type { ProLogicPlatform } from './platform';

/**
 * Contact sensor that surfaces the AquaConnect command-path wedge condition.
 *
 * ContactSensorState: 0 = CONTACT_DETECTED (normal / path OK)
 *                     1 = CONTACT_NOT_DETECTED (wedged / reboot needed)
 *
 * The accessory name "Pool Bridge Reboot Needed" is static; it shows as a
 * contact sensor in HomeKit and can trigger automations (e.g. a notification)
 * when the box enters read-only mode.
 */
export class BridgeHealthAccessory {
  private readonly service: Service;
  private wedged = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'AquaConnect')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, 'bridge-health');

    this.service = this.accessory.getService(this.platform.Service.ContactSensor)
      ?? this.accessory.addService(this.platform.Service.ContactSensor);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    this.service.getCharacteristic(this.platform.Characteristic.ContactSensorState)
      .onGet(() => this.wedged ? 1 : 0);
  }

  updateWedged(wedged: boolean): void {
    if (this.wedged === wedged) return;
    this.wedged = wedged;
    this.service.updateCharacteristic(
      this.platform.Characteristic.ContactSensorState,
      wedged ? 1 : 0,
    );
    if (wedged) {
      this.platform.log.warn(
        '[BridgeHealth] AquaConnect command path wedged — power-cycle the box to restore control.',
      );
    } else {
      this.platform.log.info('[BridgeHealth] AquaConnect command path recovered.');
    }
  }
}
