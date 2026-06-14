import type { API, DynamicPlatformPlugin, Logging, PlatformAccessory, PlatformConfig } from 'homebridge';
import { SwitchAccessory } from './switchAccessory';
import { ThermostatAccessory, type ThermostatState } from './thermostatAccessory';
import { TemperatureAccessory } from './temperatureAccessory';
import { SidecarClient } from './sidecarClient';
import {
  PLATFORM_NAME, PLUGIN_NAME, CIRCUITS,
  type Circuit, type PlatformConfig as ProLogicConfig,
} from './settings';

export class ProLogicPlatform implements DynamicPlatformPlugin {
  public readonly Service: typeof this.api.hap.Service;
  public readonly Characteristic: typeof this.api.hap.Characteristic;
  public readonly sidecar: SidecarClient;

  /** Latest valve mode from the sidecar poll; used by ThermostatAccessory.handleSetTarget. */
  public currentValveMode: 'pool' | 'spa' | null = null;

  private readonly cfg: ProLogicConfig;
  private readonly cachedAccessories: PlatformAccessory[] = [];
  private readonly switches = new Map<Circuit, SwitchAccessory>();
  // Accessory A: mode-following thermostat (pool mode → pool setpoint, spa mode → spa setpoint)
  private thermostatAuto?: ThermostatAccessory;
  // Accessory C: dedicated spa setpoint thermostat
  private thermostatSpa?: ThermostatAccessory;
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
      activeBodies: config['activeBodies'] ?? ['pool', 'spa'],
      enablePoolHeaterThermostat: config['enablePoolHeaterThermostat'] ?? true,
      enableSpaHeaterThermostat: config['enableSpaHeaterThermostat'] ?? true,
      enableTemperatureSensors: config['enableTemperatureSensors'] ?? true,
    };

    this.sidecar = new SidecarClient(this.cfg.sidecarHost, this.cfg.sidecarPort);

    this.api.on('didFinishLaunching', () => {
      this.discoverAccessories();
      this.startPolling();
    });

    this.api.on('shutdown', () => {
      if (this.pollTimer) clearInterval(this.pollTimer);
    });
  }

  configureAccessory(accessory: PlatformAccessory): void {
    this.cachedAccessories.push(accessory);
  }

  private discoverAccessories(): void {
    const toRegister: PlatformAccessory[] = [];
    const toKeep = new Set<string>();

    const register = (label: string, uuid: string): PlatformAccessory => {
      toKeep.add(uuid);
      let acc = this.cachedAccessories.find(a => a.UUID === uuid);
      if (!acc) {
        acc = new this.api.platformAccessory(label, uuid);
        toRegister.push(acc);
        this.log.info(`Registering new accessory: ${label}`);
      }
      return acc;
    };

    // Circuit switches
    for (const circuit of this.cfg.circuits) {
      if (CIRCUITS.includes(circuit)) {
        const acc = register(circuitLabel(circuit),
          this.api.hap.uuid.generate(`${PLUGIN_NAME}-circuit-${circuit}`));
        this.switches.set(circuit, new SwitchAccessory(this, acc, circuit));
      }
    }

    // Accessory A: mode-following thermostat (§10)
    if (this.cfg.enablePoolHeaterThermostat) {
      const acc = register('Pool Heater',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-thermostat-auto`));
      this.thermostatAuto = new ThermostatAccessory(this, acc, 'auto');
    }

    // Accessory C: dedicated spa thermostat (§10.2)
    if (this.cfg.enableSpaHeaterThermostat && this.cfg.activeBodies.includes('spa')) {
      const acc = register('Spa Heater',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-thermostat-spa`));
      this.thermostatSpa = new ThermostatAccessory(this, acc, 'spa');
    }

    // Temperature sensors
    if (this.cfg.enableTemperatureSensors) {
      for (const type of ['pool', 'air'] as const) {
        const label = type === 'pool' ? 'Pool Temperature' : 'Air Temperature';
        const acc = register(label,
          this.api.hap.uuid.generate(`${PLUGIN_NAME}-temp-${type}`));
        const sensor = new TemperatureAccessory(this, acc, type);
        if (type === 'pool') {
          this.poolTempSensor = sensor;
        } else {
          this.airTempSensor = sensor;
        }
      }
    }

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

        this.currentValveMode = status.valve_mode;

        for (const [circuit, sw] of this.switches) {
          sw.updateState(status.circuits[circuit] ?? false);
        }

        const ts: ThermostatState = {
          poolTempF: status.pool_temp,
          spaTempF: status.spa_temp,
          poolSetpointF: status.pool_setpoint_f,
          spaSetpointF: status.spa_setpoint_f,
          poolHeaterEnabled: status.pool_heater_enabled,
          spaHeaterEnabled: status.spa_heater_enabled,
          valveMode: status.valve_mode,
        };
        this.thermostatAuto?.updateState(ts);
        this.thermostatSpa?.updateState(ts);

        this.poolTempSensor?.updateTemperature(status.pool_temp);
        this.airTempSensor?.updateTemperature(status.air_temp);
      } catch (err) {
        this.log.debug('Sidecar poll failed:', (err as Error).message);
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
