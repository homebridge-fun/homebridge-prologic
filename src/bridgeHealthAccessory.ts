import type { PlatformAccessory, Service } from 'homebridge';
import type { ProLogicPlatform } from './platform';

/**
 * Switch tile that surfaces the AquaConnect command-path wedge condition.
 * On (true)  = bridge is wedged, needs power-cycle.
 * Off (false) = command path OK.
 *
 * Rendered as a switch so it sits alongside other switches in HomeKit and
 * highlights visibly when the bridge needs attention. Tap writes are ignored;
 * the next poll restores the correct state.
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

    // Self-heal: this accessory was originally a ContactSensor. If a cached
    // copy still carries that service, strip it so HomeKit presents the Switch.
    const staleContact = this.accessory.getService(this.platform.Service.ContactSensor);
    if (staleContact) {
      this.accessory.removeService(staleContact);
    }

    this.service = this.accessory.getService(this.platform.Service.Switch)
      ?? this.accessory.addService(this.platform.Service.Switch);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    this.service.getCharacteristic(this.platform.Characteristic.On)
      .onGet(() => this.wedged)
      .onSet(() => {
        // Ignore — state is driven by the sidecar poll, not user input.
        // Next poll will correct the tile.
      });
  }

  updateWedged(wedged: boolean): void {
    if (this.wedged === wedged) return;
    this.wedged = wedged;
    this.service.updateCharacteristic(this.platform.Characteristic.On, wedged);
    if (wedged) {
      this.platform.log.warn(
        '[BridgeHealth] AquaConnect command path wedged — power-cycle the box to restore control.',
      );
    } else {
      this.platform.log.info('[BridgeHealth] AquaConnect command path recovered.');
    }
  }
}

