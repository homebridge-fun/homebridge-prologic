import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

export type FanRole = 'chlorinator' | 'pump' | 'heater';

/**
 * Fan accessory covering three roles:
 *
 * chlorinator / pump — percentage display + control. Active always 1 so the
 *   speed ring stays visible even at 0%.
 *
 * heater — three-state indicator using proper Fanv2 semantics:
 *   Active=0, CurrentFanState=0  →  grayed out  (heater in Manual Off)
 *   Active=1, CurrentFanState=1  →  highlighted, fan still  (Auto, not heating)
 *   Active=1, CurrentFanState=2  →  highlighted, fan spinning  (Auto + calling for heat)
 *   Writes to Active/RotationSpeed are no-ops — state is read-only from the poll.
 */
export class FanAccessory {
  private readonly service: Service;
  private currentPct = 0;
  private heaterEnabled = false;
  private heaterActive = false;

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

    if (role === 'heater') {
      this.service.getCharacteristic(C.Active)
        .onGet(() => this.heaterEnabled ? 1 : 0)
        .onSet(() => { /* read-only */ });

      this.service.getCharacteristic(C.CurrentFanState)
        .onGet(() => {
          if (!this.heaterEnabled) return 0; // Inactive
          return this.heaterActive ? 2 : 1;  // Blowing Air : Idle
        });
    } else {
      // chlorinator / pump: always active so the speed ring stays visible
      this.service.getCharacteristic(C.Active)
        .onGet(() => 1)
        .onSet(() => { /* no-op */ });

      const minStep = role === 'chlorinator' ? 1 : 5;
      this.service.getCharacteristic(C.RotationSpeed)
        .setProps({ minValue: 0, maxValue: 100, minStep })
        .onGet(() => this.currentPct)
        .onSet(this.handleSetSpeed.bind(this));
    }
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

  /** For chlorinator / pump: update the speed percentage display. */
  updateSpeed(pct: number | null): void {
    if (pct === null) return;
    const rounded = Math.round(pct);
    if (this.currentPct !== rounded) {
      this.currentPct = rounded;
      this.service.updateCharacteristic(this.platform.Characteristic.RotationSpeed, rounded);
    }
  }

  /**
   * For heater role: drive the three visual states.
   * enabled = heater in Auto mode; active = actively calling for heat right now.
   */
  updateHeater(enabled: boolean, active: boolean): void {
    const { Characteristic: C } = this.platform;
    if (this.heaterEnabled !== enabled) {
      this.heaterEnabled = enabled;
      this.service.updateCharacteristic(C.Active, enabled ? 1 : 0);
    }
    if (this.heaterActive !== active) {
      this.heaterActive = active;
      const fanState = !enabled ? 0 : active ? 2 : 1;
      this.service.updateCharacteristic(C.CurrentFanState, fanState);
    }
  }
}
