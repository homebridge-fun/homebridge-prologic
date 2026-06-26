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
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    /** VSP slot number 1–4, or 0 to indicate the dedicated Spa Speed setting. */
    public readonly slot: number,
    private readonly minPct: number = 0,
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
    this.service.updateCharacteristic(C.Active, 1);

    this.service.getCharacteristic(C.CurrentFanState)
      .onGet(() => this.running
        ? C.CurrentFanState.BLOWING_AIR
        : C.CurrentFanState.IDLE);
    this.service.updateCharacteristic(C.CurrentFanState, C.CurrentFanState.IDLE);

    // minValue stays 0: a non-zero RotationSpeed minimum makes the Home app
    // render the slider across the (max - min) span starting at 0 (a 35 floor
    // shows as "0–65%"). Keep an honest 0–100 slider and enforce the panel's
    // hardware floor by snapping up to minPct on commit instead.
    this.service.getCharacteristic(C.RotationSpeed)
      .setProps({ minValue: 0, maxValue: 100, minStep: 5 })
      .onGet(() => this.configuredPct)
      .onSet(this.handleSetSpeed.bind(this));
  }

  private handleSetSpeed(value: CharacteristicValue): void {
    const pct = Math.round(value as number);

    // HomeKit sends 0 when the user taps without dragging — treat as no-op.
    if (pct === 0) {
      setTimeout(() => {
        this.service.updateCharacteristic(
          this.platform.Characteristic.RotationSpeed, this.configuredPct);
      }, 100);
      return;
    }

    // Debounce: HomeKit fires onSet repeatedly while the user drags the slider.
    // Only commit after 600 ms of silence so we get a single menu navigation.
    if (this.debounceTimer) clearTimeout(this.debounceTimer);
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      this.commitSpeed(pct);
    }, 600);
  }

  private async commitSpeed(rawPct: number): Promise<void> {
    const label = this.slot === 0 ? 'Spa Speed' : `Slot ${this.slot}`;
    // The slider is 0–100 but the panel silently clamps below minPct, so snap
    // up to the floor and push the corrected value back to the tile so the user
    // sees the real value with no flicker.
    const pct = Math.max(rawPct, this.minPct);
    if (pct !== rawPct) {
      this.service.updateCharacteristic(
        this.platform.Characteristic.RotationSpeed, pct);
    }
    this.platform.log.info(`[VSP ${label}] speed → ${pct}%`);
    try {
      if (this.slot === 0) {
        await this.platform.sidecar.setSpaSpeed(pct);
      } else {
        await this.platform.sidecar.setVspSlot(this.slot, pct);
        await this.platform.sidecar.activateVspSlot(this.slot);
      }
      this.configuredPct = pct;
    } catch (err) {
      this.platform.log.error(`[VSP ${label}] set speed failed:`, err);
      this.service.updateCharacteristic(
        this.platform.Characteristic.RotationSpeed, this.configuredPct);
    }
  }

  updateRunning(activeSlot: number | null, filterOn: boolean): void {
    const running = filterOn && activeSlot === this.slot;
    this.running = running;
    const { Characteristic: C } = this.platform;
    this.service.updateCharacteristic(C.Active, 1);
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
