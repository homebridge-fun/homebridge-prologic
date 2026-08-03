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
  /** Scene we just sent; poll-sync ignores the sidecar's value until it catches
   * up, so a poll during the several-second power-cycle can't revert the pick. */
  private pending: number | null = null;

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

    // HomeKit lets you rename inputs but NOT reorder them — so pin the order
    // here. DisplayOrder is a TLV8 list of identifiers in the desired sequence;
    // each entry is 0x01,0x04,<4-byte LE id>,0x00,0x00. We use program order
    // (matches the Pentair/Hayward manual and the cockpit dropdown).
    const order: number[] = [];
    for (const p of programs) {
      order.push(0x01, 0x04, p.n & 0xff, (p.n >> 8) & 0xff,
        (p.n >> 16) & 0xff, (p.n >> 24) & 0xff, 0x00, 0x00);
    }
    this.tv.setCharacteristic(C.DisplayOrder, Buffer.from(order).toString('base64'));
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

  // NOT async: HomeKit's ActiveIdentifier write must return immediately.
  // Awaiting the multi-second power-cycle makes the write time out and HomeKit
  // reverts the selector to the previous input (even though the scene fired).
  private handleSelect(value: CharacteristicValue): void {
    const n = value as number;
    this.activeId = n;
    this.pending = n;
    const p = this.programs.find(x => x.n === n);
    this.platform.log.info(`[Light ${this.body}] scene -> ${p?.name ?? n}`);
    this.platform.sidecar.setLightProgram(this.body, n)
      .then(() => {
        // The power-cycle ends ON — reflect it.
        this.isOn = true;
        this.tv.updateCharacteristic(this.platform.Characteristic.Active, 1);
      })
      .catch((err) => {
        this.pending = null;
        this.platform.log.error(`[Light ${this.body}] scene set failed:`, err);
      });
  }

  /**
   * Reconcile from the sidecar poll: power from the real circuit state, and the
   * selected scene from the sidecar's last-sent program (so HomeKit shows the
   * last scene instead of resetting to a default input after a plugin restart —
   * scene selection is otherwise open-loop).
   */
  updateState(circuitOn: boolean, lastProgram?: number): void {
    if (this.isOn !== circuitOn) {
      this.isOn = circuitOn;
      this.tv.updateCharacteristic(this.platform.Characteristic.Active, circuitOn ? 1 : 0);
    }
    if (lastProgram == null) return;
    // While a pick is in flight, ignore the sidecar's (still-old) value until it
    // reflects our selection — otherwise a poll mid power-cycle reverts it.
    if (this.pending !== null) {
      if (lastProgram === this.pending) this.pending = null;
      return;
    }
    // No pending pick: mirror the sidecar (restores last scene after a restart,
    // and reflects scenes fired from the cockpit).
    if (lastProgram !== this.activeId && this.programs.some(p => p.n === lastProgram)) {
      this.activeId = lastProgram;
      this.tv.updateCharacteristic(this.platform.Characteristic.ActiveIdentifier, lastProgram);
    }
  }
}
