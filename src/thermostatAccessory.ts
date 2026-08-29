import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';
import { fahrenheitToCelsius, celsiusToFahrenheit } from './settings';

const MIN_TEMP_C = fahrenheitToCelsius(65);
const MAX_TEMP_C = fahrenheitToCelsius(104);

export interface ThermostatState {
  poolTempF: number | null;
  spaTempF: number | null;
  poolSetpointF: number | null;
  spaSetpointF: number | null;
  poolHeaterEnabled: boolean | null;
  spaHeaterEnabled: boolean | null;
  valveMode: 'pool' | 'spa' | null;
  /** HEATER_1 Auto-mode circuit — true when the heater is ARMED (Auto vs Manual Off). */
  heater1Circuit: boolean;
  /** Heater relay firing RIGHT NOW (distinct from armed). Drives Heating vs Idle. */
  heaterActive: boolean;
}

/**
 * §10 HomeKit thermostat accessory ("Active Heat"). One physical heater, two
 * mode-driven setpoints, ONE tile: mirrors whichever setpoint is active for
 * the current valve mode (pool or spa).
 *
 * History: this used to also support dedicated always-that-body tiles
 * ("Pool Heat" / "Spa Heat", a `body: 'pool'|'spa'` parameter) as part of a
 * three-accessory design — see docs/aqualogic-automation-spec.md §10
 * (marked historical). Removed: one physical HEATER_1 enable rendered as
 * three tiles was confusing (they could disagree on Heating/Standby and on
 * which setpoint was "live"). Only this single mirror tile ships now.
 *
 * The Name/ConfiguredName characteristic is set ONCE at registration and
 * never touched again. An earlier version swapped the name between
 * "Heat — Pool" / "Heat — Spa" on every mode change — confirmed on hardware
 * (2026-08) that this doesn't work: HAP documents Name as not meant to
 * change post-pairing, ConfiguredName in particular is user-owned (editable
 * via the Home app), and a pushed update can get permanently stuck showing a
 * stale value that no longer has anything to do with what the accessory
 * sends — confirmed by the fact the display was wrong even after the code
 * was changed to a constant that was never being pushed again. The fix that
 * actually worked was renaming it BY HAND once in the Home app. Lesson: don't
 * fight HomeKit for control of the name; the temperature/setpoint values
 * (which DO update reliably) are the only thing this accessory should use to
 * convey which body is active.
 *
 * handleSetTarget writes the setpoint via menu navigation (§13.3).
 * handleSetMode toggles HEATER_1 (the single physical heater enable).
 */
export class ThermostatAccessory {
  private readonly service: Service;
  private currentTempC = fahrenheitToCelsius(70);
  private targetTempC = fahrenheitToCelsius(80);
  private heatingActive = false;
  private heaterEnabled = false;
  private setpointDebounce: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, 'heater-auto');

    this.service = this.accessory.getService(this.platform.Service.Thermostat)
      ?? this.accessory.addService(this.platform.Service.Thermostat);

    // Set once at registration. Never pushed again — see the class doc above
    // for why (HomeKit doesn't respect post-pairing Name/ConfiguredName
    // pushes reliably; rename it in the Home app if you want something else).
    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    const { Characteristic: C } = this.platform;

    this.service.getCharacteristic(C.CurrentTemperature)
      .onGet(() => this.currentTempC);

    this.service.getCharacteristic(C.TargetTemperature)
      .setProps({ minValue: MIN_TEMP_C, maxValue: MAX_TEMP_C, minStep: 0.5 })
      .onGet(() => this.targetTempC)
      .onSet(this.handleSetTarget.bind(this));

    this.service.getCharacteristic(C.CurrentHeatingCoolingState)
      .onGet(() => this.heatingActive ? 1 : 0);

    this.service.getCharacteristic(C.TargetHeatingCoolingState)
      .setProps({ validValues: [0, 1] })
      .onGet(() => this.heaterEnabled ? 1 : 0)
      .onSet(this.handleSetMode.bind(this));

    // Display units: 1 = Fahrenheit (HomeKit internally always uses Celsius)
    this.service.getCharacteristic(C.TemperatureDisplayUnits).setValue(1);
  }

  handleSetTarget(value: CharacteristicValue): void {
    const c = value as number;
    // HomeKit fires onSet for every step as the user drags the temperature
    // dial. Each setpoint write is a full menu navigation (PLUS/MINUS stepping),
    // so committing every intermediate value would flood the panel and risk a
    // wedge. Debounce: commit only the final value after 600ms of silence.
    // targetTempC is left untouched until the write confirms, so a failed
    // commit can revert the dial to the last known good value.
    if (this.setpointDebounce) clearTimeout(this.setpointDebounce);
    this.setpointDebounce = setTimeout(() => {
      this.setpointDebounce = null;
      void this.commitSetpoint(c);
    }, 600);
  }

  private async commitSetpoint(c: number): Promise<void> {
    const f = Math.round(celsiusToFahrenheit(c));
    const which = this.platform.currentValveMode ?? 'pool';
    this.platform.log.info(`[Active Heat] setpoint → ${f}°F (body: ${which})`);
    try {
      await this.platform.sidecar.setHeaterSetpoint(which, f);
      this.targetTempC = c;
    } catch (err) {
      this.platform.log.error('[Active Heat] setpoint write failed:', err);
      // Revert the dial to the last known good setpoint rather than leaving the
      // failed drag value showing.
      this.service.updateCharacteristic(
        this.platform.Characteristic.TargetTemperature, this.targetTempC);
    }
  }

  // NOT async: the HEATER_1 enable navigates the Settings menu (~15s), which
  // exceeds HomeKit's ~10s onSet timeout and shows "No Response" even though the
  // toggle goes through. Fire-and-forget; reconcile on failure.
  handleSetMode(value: CharacteristicValue): void {
    const on = (value as number) !== 0;
    this.platform.log.info(`[Active Heat] mode → ${on ? 'Heat' : 'Off'} (HEATER_1 circuit)`);
    this.heaterEnabled = on; // optimistic
    this.platform.sidecar.setCircuit('HEATER_1', on)
      .then(() => {
        // Keep the Heater Auto switch in step immediately.
        this.platform.pushHeaterEnabled(on);
      })
      .catch((err) => {
        this.heaterEnabled = !on; // revert
        this.service.updateCharacteristic(
          this.platform.Characteristic.TargetHeatingCoolingState, this.heaterEnabled ? 1 : 0);
        this.platform.log.error('[Active Heat] mode set failed:', err);
      });
  }

  /**
   * Reflect a heater enable/disable that happened via another tile (the Heater
   * Auto switch), without triggering a write. Keeps the Heat/Off dial in sync
   * immediately instead of waiting for the next poll.
   */
  setModeOptimistic(enabled: boolean): void {
    if (this.heaterEnabled === enabled) return;
    this.heaterEnabled = enabled;
    this.service.updateCharacteristic(
      this.platform.Characteristic.TargetHeatingCoolingState, enabled ? 1 : 0);
  }

  updateState(s: ThermostatState): void {
    const { Characteristic: C } = this.platform;
    const which = s.valveMode ?? 'pool';

    // Current temperature: the sensor for the body this accessory reflects
    const tempF = which === 'spa' ? s.spaTempF : s.poolTempF;
    if (tempF !== null) {
      const c = fahrenheitToCelsius(tempF);
      if (this.currentTempC !== c) {
        this.currentTempC = c;
        this.service.updateCharacteristic(C.CurrentTemperature, c);
      }
    }

    // Target setpoint
    const setpointF = which === 'spa' ? s.spaSetpointF : s.poolSetpointF;
    if (setpointF !== null) {
      const c = fahrenheitToCelsius(setpointF);
      if (this.targetTempC !== c) {
        this.targetTempC = c;
        this.service.updateCharacteristic(C.TargetTemperature, c);
      }
    }

    // TargetHeatingCoolingState = ARMED state (Auto vs Manual Off). Body-specific
    // Auto-mode flag: is the heater set to fire when temp < setpoint? Falls back
    // to the HEATER_1 Auto-mode circuit if the sidecar hasn't seen the scroll
    // screen yet (e.g. RS-485 backend which doesn't expose the Auto/Manual field).
    const enabledByBody = which === 'spa' ? s.spaHeaterEnabled : s.poolHeaterEnabled;
    const enabled = enabledByBody ?? s.heater1Circuit;

    // CurrentHeatingCoolingState = HEAT only when the relay is actually FIRING
    // right now — this tile always mirrors whichever body is active, so it's
    // just the raw heater_active signal (one physical relay, not body-specific).
    if (this.heatingActive !== s.heaterActive) {
      this.heatingActive = s.heaterActive;
      this.service.updateCharacteristic(C.CurrentHeatingCoolingState, s.heaterActive ? 1 : 0);
    }
    if (this.heaterEnabled !== enabled) {
      this.heaterEnabled = enabled;
      this.service.updateCharacteristic(C.TargetHeatingCoolingState, enabled ? 1 : 0);
    }
  }
}
