import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';
import type { Circuit } from './settings';

export class SwitchAccessory {
  private service: Service;
  private currentState = false;
  // True while a write is in flight. The heater write navigates the Settings
  // menu (~15s); during that window the poll loop keeps reporting the old
  // enabled state, which would revert our optimistic update. Suppress poll
  // updates until the write resolves and the navigator has confirmed.
  private writeInFlight = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly circuit: Circuit,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, circuit);

    // Evict any stale Fanv2 service left from earlier plugin versions.
    const staleFan = this.accessory.getService(this.platform.Service.Fanv2);
    if (staleFan) this.accessory.removeService(staleFan);

    this.service = this.accessory.getService(this.platform.Service.Switch)
      ?? this.accessory.addService(this.platform.Service.Switch);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    this.service.getCharacteristic(this.platform.Characteristic.On)
      .onGet(this.handleGet.bind(this))
      .onSet(this.handleSet.bind(this));
  }

  handleGet(): CharacteristicValue {
    return this.currentState;
  }

  async handleSet(value: CharacteristicValue): Promise<void> {
    const on = value as boolean;
    this.currentState = on; // optimistic update so onGet returns new value immediately
    this.writeInFlight = true;

    const doWrite = async () => {
      if (this.circuit === 'SUPER_CHLORINATE') {
        await this.platform.sidecar.setSuperChlorinate(on);
      } else {
        await this.platform.sidecar.setCircuit(this.circuit, on);
      }
      // Keep the heater thermostats' Heat/Off dials in step immediately when
      // the enable was toggled from the Heater Auto switch, rather than letting
      // them lag until the next poll.
      if (this.circuit === 'HEATER_1') this.platform.pushHeaterEnabled(on);
    };

    // HEATER_1 and SUPER_CHLORINATE writes navigate the Settings menu (~15s),
    // which exceeds HomeKit's ~10s onSet timeout — awaiting them makes HomeKit
    // show "No Response" even though the command goes through. Fire-and-forget
    // for those: return immediately and reconcile state in the background.
    const slow = this.circuit === 'HEATER_1' || this.circuit === 'SUPER_CHLORINATE';
    if (slow) {
      doWrite()
        .catch((err) => {
          this.currentState = !on; // revert the optimistic value
          this.service.updateCharacteristic(this.platform.Characteristic.On, !on);
          this.platform.log.error(`[${this.circuit}] set failed:`, err);
        })
        .finally(() => { this.writeInFlight = false; });
      return; // don't await — let HomeKit's write complete right away
    }

    // Fast single-press circuits (FILTER/LIGHTS/AUX): await for prompt feedback.
    try {
      await doWrite();
    } catch (err) {
      this.currentState = !on; // revert on failure
      this.platform.log.error(`[${this.circuit}] set failed:`, err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    } finally {
      this.writeInFlight = false;
    }
  }

  updateState(on: boolean): void {
    // Hold our optimistic value while a write is committing — the poll loop
    // still reports the pre-write state until the navigator confirms.
    if (this.writeInFlight) return;
    if (this.currentState !== on) {
      this.currentState = on;
      this.service.updateCharacteristic(this.platform.Characteristic.On, on);
    }
  }
}
