import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

/**
 * Heater represented as a single tappable Fanv2 with a three-state model:
 *
 *   Active=0, CurrentFanState=0  →  grayed out            (Manual Off)
 *   Active=1, CurrentFanState=1  →  highlighted, still     (Auto, not firing)
 *   Active=1, CurrentFanState=2  →  highlighted, spinning  (Auto + actively firing)
 *
 * Tapping the tile toggles Auto vs Manual Off via the HEATER_1 keypad path
 * (setCircuit('HEATER_1', on)). The spin state is read-only from the poll:
 *   armed  = heater enabled / Auto mode  (circuits['HEATER_1'])
 *   firing = relay calling for heat now  (status.heater_active)
 */
export class HeaterFanAccessory {
  private readonly service: Service;
  private armed = false;
  private firing = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, 'heater-fan');

    this.service = this.accessory.getService(this.platform.Service.Fanv2)
      ?? this.accessory.addService(this.platform.Service.Fanv2);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    const { Characteristic: C } = this.platform;

    this.service.getCharacteristic(C.Active)
      .onGet(() => this.armed ? 1 : 0)
      .onSet(this.handleSetActive.bind(this));

    this.service.getCharacteristic(C.CurrentFanState)
      .onGet(() => {
        if (!this.armed) return C.CurrentFanState.INACTIVE;
        return this.firing ? C.CurrentFanState.BLOWING_AIR : C.CurrentFanState.IDLE;
      });

    // RotationSpeed is required for HomeKit to honor CurrentFanState (a Fanv2
    // without a speed ring animates purely on Active). 100 = firing, 0 = idle.
    // Read-only: the slider just reflects firing state.
    this.service.getCharacteristic(C.RotationSpeed)
      .setProps({ minValue: 0, maxValue: 100, minStep: 1 })
      .onGet(() => this.firing ? 100 : 0)
      .onSet(() => { /* read-only */ });
  }

  private async handleSetActive(value: CharacteristicValue): Promise<void> {
    const on = (value as number) !== 0;
    this.platform.log.info(`[Heater] tap → ${on ? 'Auto' : 'Manual Off'} (HEATER_1)`);
    this.armed = on; // optimistic so onGet reflects the tap immediately
    try {
      await this.platform.sidecar.setCircuit('HEATER_1', on);
    } catch (err) {
      this.armed = !on; // revert on failure
      this.platform.log.error('[Heater] set failed:', err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  updateState(armed: boolean, firing: boolean): void {
    const { Characteristic: C } = this.platform;
    this.armed = armed;
    this.firing = firing;
    // Push Active and CurrentFanState every poll — HomeKit can silently
    // drop updates if Active isn't re-asserted alongside state changes.
    this.service.updateCharacteristic(C.Active, armed ? 1 : 0);
    const fanState = !armed
      ? C.CurrentFanState.INACTIVE
      : (firing ? C.CurrentFanState.BLOWING_AIR : C.CurrentFanState.IDLE);
    this.service.updateCharacteristic(C.CurrentFanState, fanState);
    this.service.updateCharacteristic(C.RotationSpeed, firing ? 100 : 0);
  }
}
