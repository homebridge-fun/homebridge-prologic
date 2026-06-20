import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

export type FanRole = 'chlorinator' | 'pump';

/**
 * Fan accessory for percentage-based controls: pool chlorinator output % and
 * VSP pump speed %. Active is always 1 so the speed ring stays visible at 0%.
 */
export class FanAccessory {
  private readonly service: Service;
  private currentPct = 0;
  private running = false;
  private activeSlot: number | null = null;
  private debounceTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly role: FanRole,
  ) {
    const serials: Record<FanRole, string> = {
      chlorinator: 'fan-chlorinator',
      pump: 'fan-pump',
    };
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, serials[role]);

    this.service = this.accessory.getService(this.platform.Service.Fanv2)
      ?? this.accessory.addService(this.platform.Service.Fanv2);

    const { Characteristic: C } = this.platform;

    this.service.getCharacteristic(C.Active)
      .onGet(() => 1)
      .onSet(() => { /* no-op */ });
    // HomeKit caches Active=0 (Inactive) by default; push 1 now so the
    // spinning animation works without waiting for a HAP poll.
    this.service.updateCharacteristic(C.Active, 1);

    // CurrentFanState drives the spinning animation:
    // 0=Inactive, 1=Idle (on but not moving), 2=Blowing Air (spinning)
    this.service.getCharacteristic(C.CurrentFanState)
      .onGet(() => this.running
        ? C.CurrentFanState.BLOWING_AIR
        : C.CurrentFanState.IDLE);
    this.service.updateCharacteristic(C.CurrentFanState, C.CurrentFanState.IDLE);

    this.service.getCharacteristic(C.RotationSpeed)
      .setProps({ minValue: 0, maxValue: 100, minStep: role === 'chlorinator' ? 1 : 5 })
      .onGet(() => this.currentPct)
      .onSet(this.handleSetSpeed.bind(this));
  }

  private handleSetSpeed(value: CharacteristicValue): void {
    const pct = Math.round(value as number);
    // HomeKit fires onSet repeatedly while the user drags the speed ring. Each
    // commit is a full menu navigation (and for chlorinator, minStep is 1), so
    // writing every intermediate value would flood the panel and risk a wedge.
    // Debounce: commit only the final value after 600ms of silence.
    if (this.debounceTimer) clearTimeout(this.debounceTimer);
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null;
      this.commitSpeed(pct);
    }, 600);
  }

  private async commitSpeed(pct: number): Promise<void> {
    this.platform.log.info(`[Fan ${this.role}] speed → ${pct}%`);
    try {
      if (this.role === 'chlorinator') {
        // Write whichever body's chlorinator matches the current valve mode;
        // default to pool until the first poll resolves the mode.
        const which = this.platform.currentValveMode === 'spa' ? 'spa' : 'pool';
        await this.platform.sidecar.setChlorinatorPercent(which, pct);
      } else {
        await this.platform.sidecar.setVspSlot(4, pct);
        await this.platform.sidecar.activateVspSlot(4);
      }
      this.currentPct = pct;
    } catch (err) {
      this.platform.log.error(`[Fan ${this.role}] set speed failed:`, err);
      // Revert the ring to the last known good value rather than leaving the
      // user's failed drag value showing.
      this.service.updateCharacteristic(
        this.platform.Characteristic.RotationSpeed, this.currentPct);
    }
  }

  updateRunning(running: boolean): void {
    this.running = running;
    const { Characteristic: C } = this.platform;
    // Push Active=1 every poll — HomeKit can silently reset it to 0 on
    // accessory reconnect, which prevents the spinning animation.
    this.service.updateCharacteristic(C.Active, 1);
    this.service.updateCharacteristic(
      C.CurrentFanState,
      running ? C.CurrentFanState.BLOWING_AIR : C.CurrentFanState.IDLE,
    );
  }

  updateSpeed(pct: number | null): void {
    if (pct === null) return;
    const rounded = Math.round(pct);
    if (this.currentPct !== rounded) {
      this.currentPct = rounded;
      this.service.updateCharacteristic(this.platform.Characteristic.RotationSpeed, rounded);
    }
  }

  updateActiveSlot(slot: number | null): void {
    if (this.role !== 'pump') return;
    if (this.activeSlot === slot) return;
    this.activeSlot = slot;
    const label = slot !== null
      ? `${this.accessory.displayName} · Speed ${slot}`
      : this.accessory.displayName;
    this.service.updateCharacteristic(this.platform.Characteristic.Name, label);
  }
}
