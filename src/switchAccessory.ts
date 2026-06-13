import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';
import type { Circuit } from './settings';

export class SwitchAccessory {
  private service: Service;
  private currentState = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly circuit: Circuit,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, circuit);

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
    try {
      if (this.circuit === 'SUPER_CHLORINATE') {
        await this.platform.sidecar.setSuperChlorinate(on);
      } else {
        await this.platform.sidecar.setCircuit(this.circuit, on);
      }
    } catch (err) {
      this.platform.log.error(`[${this.circuit}] set failed:`, err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  updateState(on: boolean): void {
    if (this.currentState !== on) {
      this.currentState = on;
      this.service.updateCharacteristic(this.platform.Characteristic.On, on);
    }
  }
}
