import type { PlatformAccessory, Service, CharacteristicValue } from 'homebridge';
import type { ProLogicPlatform } from './platform';
import type { LightProgram } from './sidecarClient';

/**
 * A pool/spa light exposed as a HomeKit **Television** accessory so its many
 * named scenes fit one tile: the power button toggles the light circuit, and the
 * input-source picker selects a ColorLogic/IntelliBrite scene. Chosen over a
 * wall of switches because the scene lists are long (spa=12, pool=17).
 *
 * Open-loop: the panel never reports the active scene, so ActiveIdentifier is
 * the last one we sent (optimistic). Power (Active) is reconciled from the
 * light circuit's real state on each poll.
 *
 * Television services must be published as EXTERNAL accessories (see
 * platform.setupLightTvs) — HomeKit won't show more than one TV per bridge.
 */
export class LightTvAccessory {
  private readonly tv: Service;
  private activeId: number;
  private isOn = false;

  constructor(
    private readonly platform: ProLogicPlatform,
    private readonly accessory: PlatformAccessory,
    private readonly body: 'pool' | 'spa',
    private readonly circuit: 'LIGHTS' | 'AUX_1',
    private readonly programs: LightProgram[],
  ) {
    const { Service: S, Characteristic: C } = this.platform;
    this.accessory.category = this.platform.api.hap.Categories.TELEVISION;

    this.accessory.getService(S.AccessoryInformation)!
      .setCharacteristic(C.Manufacturer, body === 'spa' ? 'Pentair' : 'Hayward')
      .setCharacteristic(C.Model, body === 'spa' ? 'IntelliBrite 5G' : 'ColorLogic UCL')
      .setCharacteristic(C.SerialNumber, `light-${body}`);

    const name = accessory.displayName;
    this.tv = this.accessory.getService(S.Television)
      ?? this.accessory.addService(S.Television);
    this.tv.setCharacteristic(C.ConfiguredName, name);
    this.tv.setCharacteristic(C.SleepDiscoveryMode,
      C.SleepDiscoveryMode.ALWAYS_DISCOVERABLE);

    this.activeId = programs.length ? programs[0].n : 1;

    this.tv.getCharacteristic(C.Active)
      .onGet(() => (this.isOn ? 1 : 0))
      .onSet(this.handleActive.bind(this));

    this.tv.getCharacteristic(C.ActiveIdentifier)
      .onGet(() => this.activeId)
      .onSet(this.handleSelect.bind(this));

    // RemoteKey is part of the Television service; we don't drive a real
    // remote, so accept and ignore presses.
    this.tv.getCharacteristic(C.RemoteKey).onSet(() => { /* no-op */ });

    // One InputSource per scene, linked to the Television service.
    for (const p of programs) {
      const subtype = `input-${p.n}`;
      const input = this.accessory.getServiceById(S.InputSource, subtype)
        ?? this.accessory.addService(S.InputSource, p.name, subtype);
      input
        .setCharacteristic(C.Identifier, p.n)
        .setCharacteristic(C.ConfiguredName, p.name)
        .setCharacteristic(C.Name, p.name)
        .setCharacteristic(C.IsConfigured, C.IsConfigured.CONFIGURED)
        .setCharacteristic(C.InputSourceType, C.InputSourceType.OTHER)
        .setCharacteristic(C.CurrentVisibilityState, C.CurrentVisibilityState.SHOWN);
      this.tv.addLinkedService(input);
    }
  }

  private async handleActive(value: CharacteristicValue): Promise<void> {
    const on = (value as number) !== 0;
    this.isOn = on;
    try {
      await this.platform.sidecar.setCircuit(this.circuit, on);
    } catch (err) {
      this.platform.log.error(`[Light ${this.body}] power ${on} failed:`, err);
    }
  }

  private async handleSelect(value: CharacteristicValue): Promise<void> {
    const n = value as number;
    this.activeId = n;
    const p = this.programs.find(x => x.n === n);
    this.platform.log.info(`[Light ${this.body}] scene -> ${p?.name ?? n}`);
    try {
      await this.platform.sidecar.setLightProgram(this.body, n);
      // Selecting a scene power-cycles the circuit and ends ON — reflect it.
      this.isOn = true;
      this.tv.updateCharacteristic(this.platform.Characteristic.Active, 1);
    } catch (err) {
      this.platform.log.error(`[Light ${this.body}] scene set failed:`, err);
    }
  }

  /** Reconcile power from the light circuit's real state (open-loop on scene). */
  updateState(circuitOn: boolean): void {
    if (this.isOn !== circuitOn) {
      this.isOn = circuitOn;
      this.tv.updateCharacteristic(this.platform.Characteristic.Active, circuitOn ? 1 : 0);
    }
  }
}
