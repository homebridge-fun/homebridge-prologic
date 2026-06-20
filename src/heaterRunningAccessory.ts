import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

/**
 * Read-only "Heater Running" switch: On when the heater relay is actively
 * firing (status.heater_active), Off otherwise. The switch is not user
 * controllable — any tap snaps back to the real relay state.
 */
export class HeaterRunningAccessory {
  private readonly service: Service;
  private firing = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, 'heater-running');

    this.service = this.accessory.getService(this.platform.Service.Switch)
      ?? this.accessory.addService(this.platform.Service.Switch);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    this.service.getCharacteristic(this.platform.Characteristic.On)
      .onGet(() => this.firing)
      .onSet((value: CharacteristicValue) => {
        // Read-only: immediately revert any user toggle to the real state.
        if ((value as boolean) !== this.firing) {
          setTimeout(() => {
            this.service.updateCharacteristic(
              this.platform.Characteristic.On, this.firing);
          }, 100);
        }
      });
  }

  updateFiring(firing: boolean): void {
    if (this.firing === firing) return;
    this.firing = firing;
    this.service.updateCharacteristic(this.platform.Characteristic.On, firing);
  }
}
