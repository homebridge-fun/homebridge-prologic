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

export type ThermostatBody = 'auto' | 'pool' | 'spa';

/**
 * §10 HomeKit thermostat accessory. One physical heater, two mode-driven
 * setpoints. ONLY `body: 'auto'` is ever instantiated (see platform.ts) —
 * mode-following mirror: points at whichever setpoint is active for the
 * current valve mode, one tile ("Active Heat"). Its name is STATIC — see
 * composeName()'s doc for why a body-swapping name was reverted (HomeKit
 * doesn't reliably re-render a pushed Name change, so it could show the
 * wrong body).
 *
 * `body: 'pool' | 'spa'` (dedicated always-that-body tiles, "Pool Heat" /
 * "Spa Heat") is UNUSED DEAD CODE, not a shipping feature: there was
 * originally a three-accessory design (see docs/aqualogic-automation-spec.md
 * §10, marked historical) with dedicated `enablePoolHeaterThermostat` /
 * `enableSpaHeaterThermostat` config options, but it was removed — one
 * physical HEATER_1 enable rendered as three tiles was confusing (they could
 * disagree on Heating/Standby and on which setpoint was "live"). Those config
 * options and the accessories are gone; the `'pool'`/`'spa'` branches below
 * are unreachable in production and kept only because nothing has needed
 * removing them yet — don't treat comments describing them as current-state
 * documentation.
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
  private currentName = '';
  private setpointDebounce: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly body: ThermostatBody,
  ) {
    const serials: Record<ThermostatBody, string> = {
      auto: 'heater-auto', pool: 'heater-pool', spa: 'heater-spa',
    };
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, serials[this.body]);

    this.service = this.accessory.getService(this.platform.Service.Thermostat)
      ?? this.accessory.addService(this.platform.Service.Thermostat);

    this.currentName = accessory.displayName;
    this.service.setCharacteristic(this.platform.Characteristic.Name, this.currentName);

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

  /** Which physical body's setpoint this accessory currently reflects. */
  private targetBody(valveMode: 'pool' | 'spa' | null): 'pool' | 'spa' {
    if (this.body === 'auto') return valveMode ?? 'pool';
    return this.body;
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
    const which = this.targetBody(this.platform.currentValveMode);
    this.platform.log.info(`[Thermostat ${this.body}] setpoint → ${f}°F (body: ${which})`);
    try {
      await this.platform.sidecar.setHeaterSetpoint(which, f);
      this.targetTempC = c;
    } catch (err) {
      this.platform.log.error(`[Thermostat ${this.body}] setpoint write failed:`, err);
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
    this.platform.log.info(`[Thermostat ${this.body}] mode → ${on ? 'Heat' : 'Off'} (HEATER_1 circuit)`);
    this.heaterEnabled = on; // optimistic
    this.platform.sidecar.setCircuit('HEATER_1', on)
      .then(() => {
        // Keep the Heater Auto switch + the other heater thermostats in step.
        this.platform.pushHeaterEnabled(on);
      })
      .catch((err) => {
        this.heaterEnabled = !on; // revert
        this.service.updateCharacteristic(
          this.platform.Characteristic.TargetHeatingCoolingState, this.heaterEnabled ? 1 : 0);
        this.platform.log.error(`[Thermostat ${this.body}] mode set failed:`, err);
      });
  }

  /**
   * Reflect a heater enable/disable that happened via another tile (the Heater
   * Auto switch or another thermostat), without triggering a write. Keeps the
   * Heat/Off dial in sync immediately instead of waiting for the next poll.
   */
  setModeOptimistic(enabled: boolean): void {
    if (this.heaterEnabled === enabled) return;
    this.heaterEnabled = enabled;
    this.service.updateCharacteristic(
      this.platform.Characteristic.TargetHeatingCoolingState, enabled ? 1 : 0);
  }

  /** Compose the role-clear dynamic name (§10.1 / §10.2).
   *
   * The 'auto' tile's name is kept STATIC ('Active Heat', its registered name)
   * rather than swapping 'Heat — Pool' / 'Heat — Spa' with the valve mode. HAP's
   * Name characteristic is documented as not meant to change post-pairing, and
   * in practice the iOS Home app's room-tile summary does not reliably re-render
   * a pushed Name/ConfiguredName update — so after switching bodies the tile's
   * TEMPERATURE VALUES update correctly (confirmed) but the LABEL can be stuck
   * showing the previous body, actively misleading rather than just stale
   * cosmetics. The values alone already convey which body is active; a name
   * that can silently lie about it is worse than a neutral one. */
  private composeName(s: ThermostatState, which: 'pool' | 'spa', enabled: boolean): string {
    if (this.body === 'auto') {
      return 'Active Heat';
    }
    const isCurrentMode = s.valveMode === which;
    const base = this.body === 'spa' ? 'Spa Heat' : 'Pool Heat';
    if (!enabled) return `${base} — Off`;
    return isCurrentMode ? `${base} — Heating` : `${base} — Standby`;
  }

  updateState(s: ThermostatState): void {
    const { Characteristic: C } = this.platform;
    const which = this.targetBody(s.valveMode);

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
    // right now (heater_active), NOT merely armed. heater1Circuit is the armed
    // Auto-mode bit, so it must not drive this — that's the Auto/Off distinction,
    // handled by `enabled` above. This is the Running (Heating) vs Idle line.
    const isActiveNow = s.heaterActive && (s.valveMode === which || this.body === 'auto');
    if (this.heatingActive !== isActiveNow) {
      this.heatingActive = isActiveNow;
      this.service.updateCharacteristic(C.CurrentHeatingCoolingState, isActiveNow ? 1 : 0);
    }
    if (this.heaterEnabled !== enabled) {
      this.heaterEnabled = enabled;
      this.service.updateCharacteristic(C.TargetHeatingCoolingState, enabled ? 1 : 0);
    }

    // Dynamic, role-clear name (§10.1 / §10.2)
    const name = this.composeName(s, which, enabled);
    if (name !== this.currentName) {
      this.currentName = name;
      this.service.updateCharacteristic(C.Name, name);
      const cn = (C as { ConfiguredName?: unknown }).ConfiguredName;
      if (cn) {
        this.service.updateCharacteristic(cn as Parameters<typeof this.service.updateCharacteristic>[0], name);
      }
    }
  }
}
