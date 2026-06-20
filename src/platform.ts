import type { API, DynamicPlatformPlugin, Logging, PlatformAccessory, PlatformConfig } from 'homebridge';
import { SwitchAccessory } from './switchAccessory';
import { ThermostatAccessory, type ThermostatState } from './thermostatAccessory';
import { TemperatureAccessory } from './temperatureAccessory';
import { FanAccessory } from './fanAccessory';
import { SpaModeAccessory } from './spaModeAccessory';
import { BridgeHealthAccessory } from './bridgeHealthAccessory';
import { SaltSensorAccessory } from './saltSensorAccessory';
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
  // §10 thermostats: A=mode-following, B=dedicated pool, C=dedicated spa
  private thermostatAuto?: ThermostatAccessory;
  private thermostatPool?: ThermostatAccessory;
  private thermostatSpa?: ThermostatAccessory;
  private poolTempSensor?: TemperatureAccessory;
  private airTempSensor?: TemperatureAccessory;
  private spaModeSwitch?: SpaModeAccessory;
  private chlorinatorFan?: FanAccessory;
  private pumpFan?: FanAccessory;
  private bridgeHealth?: BridgeHealthAccessory;
  private saltSensor?: SaltSensorAccessory;
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
      backend: config['backend'] ?? 'aquaconnect',
      aquaconnectHost: config['aquaconnectHost'] ?? '192.168.50.100',
      rs485Host: config['rs485Host'] ?? '192.168.68.101',
      rs485Port: config['rs485Port'] ?? 8899,
      circuits: config['circuits'] ?? ['FILTER', 'LIGHTS', 'HEATER_1'],
      activeBodies: config['activeBodies'] ?? ['pool', 'spa'],
      enableActiveHeaterThermostat: config['enableActiveHeaterThermostat'] ?? true,
      enablePoolHeaterThermostat: config['enablePoolHeaterThermostat'] ?? true,
      enableSpaHeaterThermostat: config['enableSpaHeaterThermostat'] ?? true,
      enableTemperatureSensors: config['enableTemperatureSensors'] ?? true,
      enableSpaModeSwitch: config['enableSpaModeSwitch'] ?? true,
      enableChlorinatorFan: config['enableChlorinatorFan'] ?? true,
      enablePumpSpeedFan: config['enablePumpSpeedFan'] ?? true,
      enableSaltSensor: config['enableSaltSensor'] ?? true,
      circuitLabels: config['circuitLabels'] ?? {},
    };

    this.sidecar = new SidecarClient(this.cfg.sidecarHost, this.cfg.sidecarPort);

    this.api.on('didFinishLaunching', () => {
      this.reconcileBackend();
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

    // Spa mode switch (On=spa, Off=pool)
    if (this.cfg.enableSpaModeSwitch) {
      const acc = register('Spa',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-mode-spa`));
      this.spaModeSwitch = new SpaModeAccessory(this, acc);
    }

    // Circuit switches
    for (const circuit of this.cfg.circuits) {
      if (CIRCUITS.includes(circuit)) {
        const acc = register(circuitLabel(circuit, this.cfg.circuitLabels),
          this.api.hap.uuid.generate(`${PLUGIN_NAME}-circuit-${circuit}`));
        this.switches.set(circuit, new SwitchAccessory(this, acc, circuit));
      }
    }

    // Accessory A: mode-following "active" thermostat (§10.1)
    if (this.cfg.enableActiveHeaterThermostat) {
      const acc = register('Active Heat',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-thermostat-auto`));
      this.thermostatAuto = new ThermostatAccessory(this, acc, 'auto');
    }

    // Accessory B: dedicated pool thermostat (§10.2)
    if (this.cfg.enablePoolHeaterThermostat && this.cfg.activeBodies.includes('pool')) {
      const acc = register('Pool Heat',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-thermostat-pool`));
      this.thermostatPool = new ThermostatAccessory(this, acc, 'pool');
    }

    // Accessory C: dedicated spa thermostat (§10.2)
    if (this.cfg.enableSpaHeaterThermostat && this.cfg.activeBodies.includes('spa')) {
      const acc = register('Spa Heat',
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

    // Fan: pool chlorinator output %
    if (this.cfg.enableChlorinatorFan) {
      const acc = register('Chlorinator',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-fan-chlorinator`));
      this.chlorinatorFan = new FanAccessory(this, acc, 'chlorinator');
    }

    // Fan: live filter/pump running speed from scroll
    if (this.cfg.enablePumpSpeedFan) {
      const acc = register('Filter Speed',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-fan-pump`));
      this.pumpFan = new FanAccessory(this, acc, 'pump');
    }

    // Salt level sensor
    if (this.cfg.enableSaltSensor) {
      const acc = register('Salt Level',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-salt-sensor`));
      this.saltSensor = new SaltSensorAccessory(this, acc);
    }

    // Switch: open when AC box command path is wedged
    {
      const acc = register('Bridge Needs Rebooting',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-bridge-health`));
      this.bridgeHealth = new BridgeHealthAccessory(this, acc);
    }

    const stale = this.cachedAccessories.filter(a => !toKeep.has(a.UUID));
    if (stale.length > 0) {
      this.api.unregisterPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, stale);
    }
    if (toRegister.length > 0) {
      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, toRegister);
    }
  }

  /**
   * Ensure the sidecar is running the backend selected in the plugin config.
   * If it differs, push the choice — the sidecar persists it and restarts
   * itself to apply. Best-effort: failures are logged, not fatal.
   */
  private async reconcileBackend(): Promise<void> {
    try {
      const cur = await this.sidecar.getBackend();
      if (cur.active === this.cfg.backend) {
        this.log.debug(`Sidecar backend already '${this.cfg.backend}'.`);
        return;
      }
      this.log.info(
        `Sidecar backend is '${cur.active}', config wants '${this.cfg.backend}' — switching (sidecar will restart).`);
      await this.sidecar.setBackend({
        backend: this.cfg.backend,
        aquaconnect_host: this.cfg.aquaconnectHost,
        rs485_host: this.cfg.rs485Host,
        rs485_port: this.cfg.rs485Port,
      });
    } catch (err) {
      this.log.warn('Backend reconcile failed (sidecar may be unreachable):',
        (err as Error).message);
    }
  }

  private startPolling(): void {
    const poll = async () => {
      try {
        const status = await this.sidecar.getStatus();

        this.currentValveMode = status.valve_mode;

        this.spaModeSwitch?.updateMode(status.valve_mode);

        for (const [circuit, sw] of this.switches) {
          if (circuit === 'HEATER_1') {
            // Show enabled (Auto mode) not active-heating so the switch
            // stays on whenever the heater is armed, regardless of whether
            // it is currently calling for heat. Falls back to the LED bit
            // until the scroll has confirmed the enabled state.
            const heaterEnabled = status.valve_mode === 'spa'
              ? (status.spa_heater_enabled ?? status.circuits['HEATER_1'] ?? false)
              : (status.pool_heater_enabled ?? status.circuits['HEATER_1'] ?? false);
            sw.updateState(heaterEnabled);
          } else {
            sw.updateState(status.circuits[circuit] ?? false);
          }
        }

        const ts: ThermostatState = {
          poolTempF: status.pool_temp,
          spaTempF: status.spa_temp,
          poolSetpointF: status.pool_setpoint_f,
          spaSetpointF: status.spa_setpoint_f,
          poolHeaterEnabled: status.pool_heater_enabled,
          spaHeaterEnabled: status.spa_heater_enabled,
          valveMode: status.valve_mode,
          heater1Circuit: status.circuits['HEATER_1'] ?? false,
        };
        this.thermostatAuto?.updateState(ts);
        this.thermostatPool?.updateState(ts);
        this.thermostatSpa?.updateState(ts);

        this.poolTempSensor?.updateTemperature(status.pool_temp);
        this.airTempSensor?.updateTemperature(status.air_temp);

        this.chlorinatorFan?.updateSpeed(status.chlorinator_percent);
        this.pumpFan?.updateSpeed(status.pump_speed);
        this.pumpFan?.updateActiveSlot(status.vsp_active_slot);
        this.saltSensor?.updateSaltLevel(status.salt_level);
        this.bridgeHealth?.updateWedged(status.bridge_wedged ?? false);
      } catch (err) {
        this.log.debug('Sidecar poll failed:', (err as Error).message);
      }
    };

    poll();
    this.pollTimer = setInterval(poll, this.cfg.pollInterval);
  }
}

function circuitLabel(circuit: Circuit, overrides: Partial<Record<Circuit, string>> = {}): string {
  const defaults: Record<Circuit, string> = {
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
  return overrides[circuit] ?? defaults[circuit] ?? circuit;
}
