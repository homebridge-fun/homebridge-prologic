import type { PlatformAccessory, Service } from 'homebridge';
import type { ProLogicPlatform } from './platform';

/**
 * Switch tile that surfaces the AquaConnect command-path wedge condition AND
 * doubles as a manual "test the bridge now" button.
 * On (true)  = bridge is wedged, needs power-cycle.
 * Off (false) = command path OK.
 *
 * Rendered as a switch so it sits alongside other switches in HomeKit and
 * highlights visibly when the bridge needs attention. Tapping it (either
 * direction) runs a live active command-path probe and snaps the tile to the
 * real result — so it works as a test button: tap it, and if the bridge is
 * healthy it falls back to off, if wedged it stays on.
 */
export class BridgeHealthAccessory {
  private readonly service: Service;
  private wedged = false;
  private testing = false;

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
      // Fire-and-forget: the bridge probe can take a few seconds; awaiting it in
      // onSet risks HomeKit's timeout. handleManualTest snaps the tile itself.
      .onSet(value => { void this.handleManualTest(Boolean(value)); });
  }

  /**
   * Tapping the tile runs a live bridge test. The requested on/off value is
   * irrelevant — we always probe and then force the tile to the true result.
   * A brief delay before the corrective update avoids HomeKit's "updating a
   * characteristic within its own set handler" warning.
   */
  private async handleManualTest(requested: boolean): Promise<void> {
    if (this.testing) return;
    this.testing = true;
    this.platform.log.info(
      `[BridgeHealth] Manual bridge test requested via switch (tapped ${requested ? 'on' : 'off'})…`,
    );
    try {
      const wedged = await this.platform.sidecar.testBridge();
      this.wedged = wedged;
      this.platform.log.info(
        `[BridgeHealth] Manual test result: ${wedged ? 'WEDGED — power-cycle the box' : 'healthy'}.`,
      );
    } catch (err) {
      this.platform.log.warn(
        '[BridgeHealth] Manual bridge test failed (sidecar unreachable?):',
        (err as Error).message,
      );
    } finally {
      this.testing = false;
      // Snap the tile to the true state regardless of what the user tapped.
      setTimeout(
        () => this.service.updateCharacteristic(this.platform.Characteristic.On, this.wedged),
        200,
      );
    }
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

