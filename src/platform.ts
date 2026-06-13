import type { API, DynamicPlatformPlugin, Logging, PlatformAccessory, PlatformConfig } from 'homebridge';
import { SwitchAccessory } from './switchAccessory';
import { ThermostatAccessory } from './thermostatAccessory';
import { TemperatureAccessory } from './temperatureAccessory';
import { SidecarClient } from './sidecarClient';
import { PLATFORM_NAME, PLUGIN_NAME, CIRCUITS, type Circuit, type PlatformConfig as ProLogicConfig } from './settings';

export class ProLogicPlatform implements DynamicPlatformPlugin {
  public readonly Service: typeof this.api.hap.Service;
  public readonly Characteristic: typeof this.api.hap.Characteristic;
  public readonly sidecar: SidecarClient;

  private readonly cfg: ProLogicConfig;
  private readonly cachedAccessories: PlatformAccessory[] = [];
  private readonly switches = new Map<Circuit, SwitchAccessory>();
  private thermostat?: ThermostatAccessory;
  private poolTempSensor?: TemperatureAccessory;
  private airTempSensor?: TemperatureAccessory;
  private pollTimer?: ReturnType<typeof setInterval>;

  constructor(
    public readonly log: Logging,
    config: PlatformConfig,
    public readonly api: API,
  ) {
    this.Service = this.api.hap.Service;
    this.Characteristic = this.api.hap.Characteristic;

    this.cfg = {
      name: config['name'] ?? 'ProLogic',
      sidecarHost: config['sidecarHost'] ?? '127.0.0.1',
      sidecarPort: config['sidecarPort'] ?? 5757,
      pollInterval: config['pollInterval'] ?? 5000,
      circuits: config['circuits'] ?? ['POOL', 'SPA', 'FILTER', 'LIGHTS', 'HEATER_1'],
      enablePoolHeaterThermostat: config['enablePoolHeaterThermostat'] ?? true,
      enableTemperatureSensors: config['enableTemperatureSensors'] ?? true,
    };

    this.sidecar = new SidecarClient(this.cfg.sidecarHost, this.cfg.sidecarPort);

    this.api.on('didFinishLaunching', () => {
      this.discoverAccessories();
      this.startPolling();
    });

    this.api.on('shutdown', () => {
      if (this.pollTimer) {
        clearInterval(this.pollTimer);
      }
    });
  }

  configureAccessory(accessory: PlatformAccessory): void {
    this.cachedAccessories.push(accessory);
  }

  private discoverAccessories(): void {
    const toRegister: PlatformAccessory[] = [];
    const toKeep = new Set<string>();

    // Switch accessories for each configured circuit
    for (const circuit of this.cfg.circuits) {
      if (circuit === 'SUPER_CHLORINATE' || CIRCUITS.includes(circuit)) {
        const label = circuitLabel(circuit);
        const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}-circuit-${circuit}`);
        toKeep.add(uuid);

        let acc = this.cachedAccessories.find(a => a.UUID === uuid);
        if (!acc) {
          acc = new this.api.platformAccessory(label, uuid);
          toRegister.push(acc);
          this.log.info(`Registering new accessory: ${label}`);
        }
        this.switches.set(circuit, new SwitchAccessory(this, acc, circuit));
      }
    }

    // Thermostat
    if (this.cfg.enablePoolHeaterThermostat) {
      const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}-thermostat`);
      toKeep.add(uuid);
      let acc = this.cachedAccessories.find(a => a.UUID === uuid);
      if (!acc) {
        acc = new this.api.platformAccessory('Pool Heater', uuid);
        toRegister.push(acc);
        this.log.info('Registering new accessory: Pool Heater thermostat');
      }
      this.thermostat = new ThermostatAccessory(this, acc);
    }

    // Temperature sensors
    if (this.cfg.enableTemperatureSensors) {
      for (const type of ['pool', 'air'] as const) {
        const label = type === 'pool' ? 'Pool Temperature' : 'Air Temperature';
        const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}-temp-${type}`);
        toKeep.add(uuid);
        let acc = this.cachedAccessories.find(a => a.UUID === uuid);
        if (!acc) {
          acc = new this.api.platformAccessory(label, uuid);
          toRegister.push(acc);
          this.log.info(`Registering new accessory: ${label}`);
        }
        const sensor = new TemperatureAccessory(this, acc, type);
        if (type === 'pool') {
          this.poolTempSensor = sensor;
        } else {
          this.airTempSensor = sensor;
        }
      }
    }

    // Remove stale accessories no longer in config
    const stale = this.cachedAccessories.filter(a => !toKeep.has(a.UUID));
    if (stale.length > 0) {
      this.api.unregisterPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, stale);
    }

    if (toRegister.length > 0) {
      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, toRegister);
    }
  }

  private startPolling(): void {
    const poll = async () => {
      try {
        const status = await this.sidecar.getStatus();

        for (const [circuit, sw] of this.switches) {
          sw.updateState(status.circuits[circuit] ?? false);
        }

        this.thermostat?.updateState(
          status.pool_temp,
          status.heater_setpoint,
          status.circuits['HEATER_1'] ?? false,
        );

        this.poolTempSensor?.updateTemperature(status.pool_temp);
        this.airTempSensor?.updateTemperature(status.air_temp);
      } catch (err) {
        this.log.debug('Sidecar poll failed (sidecar may not be running yet):', (err as Error).message);
      }
    };

    poll();
    this.pollTimer = setInterval(poll, this.cfg.pollInterval);
  }
}

function circuitLabel(circuit: Circuit): string {
  const labels: Record<Circuit, string> = {
    POOL: 'Pool',
    SPA: 'Spa',
    FILTER: 'Filter',
    LIGHTS: 'Lights',
    SPILLOVER: 'Spillover',
    AUX_1: 'Aux 1',
    AUX_2: 'Aux 2',
    HEATER_1: 'Heater',
    SUPER_CHLORINATE: 'Super Chlorinate',
  };
  return labels[circuit] ?? circuit;
}
