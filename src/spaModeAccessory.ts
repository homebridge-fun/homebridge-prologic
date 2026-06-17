import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';

/**
 * Single switch representing pool/spa valve mode.
 *   On  = spa mode active
 *   Off = pool mode (default/resting state)
 *
 * State is driven by valve_mode from the sidecar poll, not optimistic.
 * Writes call POST /mode which sends the POOL/SPA cycle key as needed.
 */
export class SpaModeAccessory {
  private readonly service: Service;
  private isSpa = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
  ) {
    this.accessory.getService(this.platform.Service.AccessoryInformation)!
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Hayward')
      .setCharacteristic(this.platform.Characteristic.Model, 'ProLogic/AquaPlus')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, 'mode-spa');

    this.service = this.accessory.getService(this.platform.Service.Switch)
      ?? this.accessory.addService(this.platform.Service.Switch);

    this.service.setCharacteristic(this.platform.Characteristic.Name, accessory.displayName);

    this.service.getCharacteristic(this.platform.Characteristic.On)
      .onGet(() => this.isSpa)
      .onSet(this.handleSet.bind(this));
  }

  private async handleSet(value: CharacteristicValue): Promise<void> {
    const wantSpa = value as boolean;
    const target = wantSpa ? 'spa' : 'pool';
    this.platform.log.info(`[SpaModeSwitch] mode → ${target}`);
    try {
      await this.platform.sidecar.setMode(target);
    } catch (err) {
      this.platform.log.error('[SpaModeSwitch] set mode failed:', err);
      throw new this.platform.api.hap.HapStatusError(
        this.platform.api.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE,
      );
    }
  }

  updateMode(valveMode: 'pool' | 'spa' | null): void {
    const spa = valveMode === 'spa';
    if (this.isSpa !== spa) {
      this.isSpa = spa;
      this.service.updateCharacteristic(this.platform.Characteristic.On, spa);
    }
  }
}
