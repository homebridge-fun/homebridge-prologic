import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

export type FanRole = 'chlorinator' | 'pump' | 'heater';

/**
 * Generic Fan accessory used for percentage-based controls that have no
 * native HomeKit service: pool chlorinator output %, VSP pump speed %, and
 * heater active state (0% = idle/off, 100% = actively calling for heat).
 *
 * RotationSpeed (0–100%) maps directly to the underlying % value.
 * Active is always reported as true (the Fan tile shows the speed ring
 * when active; off would hide it).  On/off writes are no-ops.
 * For 'heater': speed writes are no-ops — the value is read-only from the
 * HEATER_1 LED circuit bit.
 */
export class FanAccessory {
  private readonly service: Service;
  private currentPct = 0;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly role: FanRole,
  ) {
    const serials: Record<FanRole, string> = {
      chlorinator: 'fan-chlorinator',
      pump: 'fan-pump',
      heater: 'fan-heater-active',
    };
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, serials[role]);

    this.service = this.accessory.getService(this.platform.Service.Fanv2)
      ?? this.accessory.addService(this.platform.Service.Fanv2);

    const { Characteristic: C } = this.platform;

    // Always active so the speed ring is visible in Home app
    this.service.getCharacteristic(C.Active)
      .onGet(() => 1)
      .onSet(() => { /* no-op */ });

    const minStep = role === 'chlorinator' ? 1 : role === 'pump' ? 5 : 100;
    this.service.getCharacteristic(C.RotationSpeed)
      .setProps({ minValue: 0, maxValue: 100, minStep })
      .onGet(() => this.currentPct)
      .onSet(this.handleSetSpeed.bind(this));
  }

  private async handleSetSpeed(value: CharacteristicValue): Promise<void> {
    if (this.role === 'heater') return; // read-only
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
