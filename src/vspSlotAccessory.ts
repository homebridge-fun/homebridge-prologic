import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

/**
 * Fan accessory representing one VSP speed slot (1–4).
 * RotationSpeed shows the configured speed % for this slot.
 * Setting the speed writes the new value to that slot and activates it,
 * switching the pump to run at that slot.
 *
 * These tiles are registered outside the standard home screen rooms
 * (no room assignment at registration time) so they appear only in the
 * accessory detail view, not cluttering the home tab.
 */
export class VspSlotAccessory {
  private readonly service: Service;
  private configuredPct = 0;
  private running = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    public readonly slot: number,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, `vsp-slot-${slot}`);

    this.service = this.accessory.getService(this.platform.Service.Fanv2)
      ?? this.accessory.addService(this.platform.Service.Fanv2);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    const { Characteristic: C } = this.platform;

    this.service.getCharacteristic(C.Active)
      .onGet(() => 1)
      .onSet(() => { /* no-op */ });

    this.service.getCharacteristic(C.CurrentFanState)
      .onGet(() => this.running
        ? C.CurrentFanState.BLOWING_AIR
        : C.CurrentFanState.IDLE);

    this.service.getCharacteristic(C.RotationSpeed)
      .setProps({ minValue: 0, maxValue: 100, minStep: 5 })
      .onGet(() => this.configuredPct)
      .onSet(this.handleSetSpeed.bind(this));
  }

  private async handleSetSpeed(value: CharacteristicValue): Promise<void> {
    const pct = Math.round(value as number);
    this.platform.log.info(`[VSP Slot ${this.slot}] speed → ${pct}%`);
    try {
      await this.platform.sidecar.setVspSlot(this.slot, pct);
      await this.platform.sidecar.activateVspSlot(this.slot);
      this.configuredPct = pct;
    } catch (err) {
      this.platform.log.error(`[VSP Slot ${this.slot}] set speed failed:`, err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  updateRunning(activeSlot: number | null, filterOn: boolean): void {
    const running = filterOn && activeSlot === this.slot;
    if (this.running === running) return;
    this.running = running;
    const { Characteristic: C } = this.platform;
    this.service.updateCharacteristic(
      C.CurrentFanState,
      running ? C.CurrentFanState.BLOWING_AIR : C.CurrentFanState.IDLE,
    );
  }

  updateSpeed(pct: number | undefined): void {
    if (pct === undefined) return;
    const rounded = Math.round(pct);
    if (this.configuredPct !== rounded) {
      this.configuredPct = rounded;
      this.service.updateCharacteristic(this.platform.Characteristic.RotationSpeed, rounded);
    }
  }
}
