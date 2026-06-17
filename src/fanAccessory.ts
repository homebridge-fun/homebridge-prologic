import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

export type FanRole = 'chlorinator' | 'pump';

/**
 * Generic Fan accessory used for percentage-based controls that have no
 * native HomeKit service: pool chlorinator output % and VSP pump speed %.
 *
 * RotationSpeed (0–100%) maps directly to the underlying % value.
 * Active is always reported as true (the Fan tile shows the speed ring
 * when active; off would hide it).  On/off writes are no-ops.
 */
export class FanAccessory {
  private readonly service: Service;
  private currentPct = 0;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly role: FanRole,
  ) {
    const serial = role === 'chlorinator' ? 'fan-chlorinator' : 'fan-pump';
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, serial);

    this.service = this.accessory.getService(this.platform.Service.Fanv2)
      ?? this.accessory.addService(this.platform.Service.Fanv2);

    const { Characteristic: C } = this.platform;

    // Always active so the speed ring is visible in Home app
    this.service.getCharacteristic(C.Active)
      .onGet(() => 1)
      .onSet(() => { /* no-op */ });

    this.service.getCharacteristic(C.RotationSpeed)
      .setProps({ minValue: 0, maxValue: 100, minStep: role === 'chlorinator' ? 1 : 5 })
      .onGet(() => this.currentPct)
      .onSet(this.handleSetSpeed.bind(this));
  }

  private async handleSetSpeed(value: CharacteristicValue): Promise<void> {
    const pct = Math.round(value as number);
    this.platform.log.info(`[Fan ${this.role}] speed → ${pct}%`);
    try {
      if (this.role === 'chlorinator') {
        await this.platform.sidecar.setChlorinatorPercent('pool', pct);
      } else {
        await this.platform.sidecar.setVspSlot4(pct);
      }
      this.currentPct = pct;
    } catch (err) {
      this.platform.log.error(`[Fan ${this.role}] set speed failed:`, err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  updateSpeed(pct: number | null): void {
    if (pct === null) return;
    const rounded = Math.round(pct);
    if (this.currentPct !== rounded) {
      this.currentPct = rounded;
      this.service.updateCharacteristic(this.platform.Characteristic.RotationSpeed, rounded);
    }
  }
}
