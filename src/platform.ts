import type { API, DynamicPlatformPlugin, Logging, PlatformAccessory, PlatformConfig } from 'homebridge';
import { SwitchAccessory } from './switchAccessory';
import { ThermostatAccessory, type ThermostatState } from './thermostatAccessory';
import { TemperatureAccessory } from './temperatureAccessory';
import { FanAccessory } from './fanAccessory';
import { BridgeHealthAccessory } from './bridgeHealthAccessory';
import { SaltSensorAccessory } from './saltSensorAccessory';
import { HeaterRunningAccessory } from './heaterRunningAccessory';
import { LightTvAccessory } from './lightTvAccessory';
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
  // §10: single mode-following thermostat (mirrors the active heater setpoint).
  private thermostatAuto?: ThermostatAccessory;
  private poolTempSensor?: TemperatureAccessory;
  private airTempSensor?: TemperatureAccessory;
  private chlorinatorFan?: FanAccessory;
  private bridgeHealth?: BridgeHealthAccessory;
  private saltSensor?: SaltSensorAccessory;
  private heaterRunning?: HeaterRunningAccessory;
  private readonly lightTvs = new Map<'pool' | 'spa', LightTvAccessory>();
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
      rs485bridgeHost: config['rs485bridgeHost'] || undefined,
      rs485bridgePort: config['rs485bridgePort'] ?? 8899,
      circuits: config['circuits'] ?? ['FILTER', 'LIGHTS', 'HEATER_1'],
      activeBodies: config['activeBodies'] ?? ['pool', 'spa'],
      enableActiveHeaterThermostat: config['enableActiveHeaterThermostat'] ?? true,
      enableTemperatureSensors: config['enableTemperatureSensors'] ?? true,
      enableChlorinatorFan: config['enableChlorinatorFan'] ?? true,
      enableSaltSensor: config['enableSaltSensor'] ?? true,
      enableSpaLightScenes: config['enableSpaLightScenes'] ?? false,
      enablePoolLightScenes: config['enablePoolLightScenes'] ?? false,
      spaLightSceneList: config['spaLightSceneList'] ?? [],
      poolLightSceneList: config['poolLightSceneList'] ?? [],
      circuitLabels: config['circuitLabels'] ?? {},
    };

    this.sidecar = new SidecarClient(this.cfg.sidecarHost, this.cfg.sidecarPort);

    this.api.on('didFinishLaunching', () => {
      this.reconcileBackend();
      this.pushUiConfig();
      this.discoverAccessories();
      this.setupLightTvs();
      this.startPolling();
    });

    this.api.on('shutdown', () => {
      if (this.pollTimer) clearInterval(this.pollTimer);
    });
  }

  configureAccessory(accessory: PlatformAccessory): void {
    this.cachedAccessories.push(accessory);
  }

  /**
   * Propagate a heater enable/disable that originated from one tile (a
   * thermostat's Heat/Off dial or the Heater Auto switch) to every other tile
   * that mirrors the same single physical HEATER_1 enable, so they don't lag
   * until the next poll. Optimistic only — no write is issued here; the tile
   * that called this already committed the write.
   */
  public pushHeaterEnabled(enabled: boolean): void {
    this.switches.get('HEATER_1')?.updateState(enabled);
    this.thermostatAuto?.setModeOptimistic(enabled);
  }

  private discoverAccessories(): void {
    const toRegister: PlatformAccessory[] = [];
    const toUpdate: PlatformAccessory[] = [];
    const toKeep = new Set<string>();

    const register = (label: string, uuid: string): PlatformAccessory => {
      toKeep.add(uuid);
      let acc = this.cachedAccessories.find(a => a.UUID === uuid);
      if (!acc) {
        acc = new this.api.platformAccessory(label, uuid);
        toRegister.push(acc);
        this.log.info(`Registering new accessory: ${label}`);
      } else if (acc.displayName !== label) {
        // The config label changed (e.g. a circuitLabels rename). The UUID is
        // stable, so the accessory persists — but its displayName is only set
        // at creation. Update it here (before the service handler reads it) and
        // persist, otherwise the rename is silently ignored.
        this.log.info(`Renaming accessory: ${acc.displayName} -> ${label}`);
        acc.displayName = label;
        toUpdate.push(acc);
      }
      return acc;
    };

    // Circuit switches. HEATER_1 is rendered as a tappable three-state Fanv2
    // HEATER_1 is split into two switches below ("Heater Auto" tappable +
    // "Heater Running" read-only), so skip it from the plain-switch loop.
    for (const circuit of this.cfg.circuits) {
      if (circuit === 'HEATER_1') continue;
      if (CIRCUITS.includes(circuit)) {
        const acc = register(circuitLabel(circuit, this.cfg.circuitLabels),
          this.api.hap.uuid.generate(`${PLUGIN_NAME}-circuit-${circuit}`));
        this.switches.set(circuit, new SwitchAccessory(this, acc, circuit));
      }
    }

    // Heater as two switches: "Heater Auto" (tappable arm/disarm) and
    // "Heater Running" (read-only firing indicator from the relay bit).
    if (this.cfg.circuits.includes('HEATER_1')) {
      const autoAcc = register(this.cfg.circuitLabels['HEATER_1'] ?? 'Heater Auto',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-circuit-HEATER_1`));
      this.switches.set('HEATER_1', new SwitchAccessory(this, autoAcc, 'HEATER_1'));

      const runAcc = register('Heater Running',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-heater-running`));
      this.heaterRunning = new HeaterRunningAccessory(this, runAcc);
    }

    // Single mode-following "active" thermostat (§10.1). One physical heater =
    // one tile; it mirrors whichever setpoint is active for the current valve
    // mode. Name ("Active Heat") is set once here and never pushed again — a
    // body-swapping name was tried and confirmed broken on hardware (2026-08):
    // ConfiguredName is user-owned by the Home app and can get permanently
    // stuck showing a stale body, actively misleading rather than just stale.
    // If you want a different name, rename it in the Home app; the temperature
    // values (which DO update reliably) already convey which body is active.
    if (this.cfg.enableActiveHeaterThermostat) {
      const acc = register('Active Heat',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-thermostat-auto`));
      this.thermostatAuto = new ThermostatAccessory(this, acc);
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

    // Pump/VSP speeds are NOT exposed to HomeKit — they're controlled in the
    // web cockpit (which reads/writes the sidecar directly). HomeKit only keeps
    // the on/off circuits, thermostats, chlorinator, and sensors.

    // Note: the menu-navigable values (heater setpoints, chlorinator %, VSP
    // slot speeds, spa speed) are pre-fetched by the SIDECAR itself on its
    // startup (one menu pass via read_all_settings), so they populate on every
    // sidecar restart — not just when this plugin restarts. We just poll
    // /status and the cached values flow to the accessories; no plugin-side
    // pre-fetch call is needed.

    // Salt level sensor
    if (this.cfg.enableSaltSensor) {
      const acc = register('Salt Level',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-salt-sensor`));
      this.saltSensor = new SaltSensorAccessory(this, acc);
    }

    // Switch: bridge command-path health. Backend-dependent meaning — an
    // AquaConnect wedge needs a physical power-cycle; the RS-485 pad bridge just
    // goes briefly offline and self-heals when it reconnects.
    {
      const rs485 = this.cfg.backend === 'rs485bridge';
      const acc = register(rs485 ? 'Bridge Offline' : 'Bridge Needs Rebooting',
        this.api.hap.uuid.generate(`${PLUGIN_NAME}-bridge-health`));
      this.bridgeHealth = new BridgeHealthAccessory(this, acc, this.cfg.backend);
    }

    // Catch-all: strip any legacy ContactSensor service (the old wedge-sensor
    // form) from every KEPT accessory and persist it, so a stale contact-sensor
    // tile can't linger even if the per-accessory self-heal didn't catch it.
    for (const acc of this.cachedAccessories) {
      if (!toKeep.has(acc.UUID)) continue;
      const cs = acc.getService(this.api.hap.Service.ContactSensor);
      if (cs) {
        acc.removeService(cs);
        this.log.info(`Removed legacy ContactSensor service from "${acc.displayName}"`);
        if (!toRegister.includes(acc) && !toUpdate.includes(acc)) toUpdate.push(acc);
      }
    }

    const stale = this.cachedAccessories.filter(a => !toKeep.has(a.UUID));
    if (stale.length > 0) {
      this.log.info(`Removing ${stale.length} stale accessory(ies): ${stale.map(a => a.displayName).join(', ')}`);
      this.api.unregisterPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, stale);
    }
    if (toRegister.length > 0) {
      this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, toRegister);
    }
    if (toUpdate.length > 0) {
      this.api.updatePlatformAccessories(toUpdate);
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
      // Always push the full desired config, not just when the backend TYPE
      // changed. Comparing only `cur.active === this.cfg.backend` missed a
      // changed HOST/PORT (e.g. the pad Pi's tailnet IP changing after a
      // re-image) — the sidecar kept polling the dead old address forever.
      // setBackend is idempotent: it returns {unchanged:true} without a restart
      // when nothing actually changed, so pushing unconditionally is safe.
      if (cur.active !== this.cfg.backend) {
        this.log.info(
          `Sidecar backend is '${cur.active}', config wants '${this.cfg.backend}' — switching (sidecar will restart).`);
      } else {
        this.log.debug(`Reconciling sidecar '${this.cfg.backend}' config (host/port).`);
      }
      await this.sidecar.setBackend({
        backend: this.cfg.backend,
        aquaconnect_host: this.cfg.aquaconnectHost,
        rs485bridge_host: this.cfg.rs485bridgeHost,
        rs485bridge_port: this.cfg.rs485bridgePort,
      });
    } catch (err) {
      this.log.warn('Backend reconcile failed (sidecar may be unreachable):',
        (err as Error).message);
    }
  }

  /**
   * Mirror the enabled circuits + label overrides to the sidecar so the web
   * cockpit shows the same switches and names as HomeKit. Best-effort.
   */
  private async pushUiConfig(): Promise<void> {
    try {
      await this.sidecar.setUiConfig(this.cfg.circuits, this.cfg.circuitLabels);
      this.log.debug('Pushed UI config (circuits + labels) to sidecar.');
    } catch (err) {
      this.log.debug('Push UI config failed (sidecar may be unreachable):',
        (err as Error).message);
    }
  }

  /**
   * Expose each enabled light as a HomeKit Television (scene picker). These MUST
   * be published as EXTERNAL accessories — HomeKit shows only one Television per
   * bridge, so each is its own external accessory the user adds separately.
   * The scene list is fetched from the sidecar so HomeKit inputs match the
   * cockpit exactly. Best-effort: a sidecar hiccup just skips the TV.
   */
  private async setupLightTvs(): Promise<void> {
    const bodies: Array<'pool' | 'spa'> = [];
    if (this.cfg.enableSpaLightScenes) bodies.push('spa');
    if (this.cfg.enablePoolLightScenes) bodies.push('pool');

    for (const body of bodies) {
      try {
        const all = await this.sidecar.getLightPrograms(body);
        if (all.length === 0) {
          this.log.warn(`No light scenes reported for ${body} — skipping its TV tile `
            + '(needs the rs485bridge backend).');
          continue;
        }
        // Config list (if any) curates + orders + renames; else all, in order.
        const custom = body === 'spa' ? this.cfg.spaLightSceneList : this.cfg.poolLightSceneList;
        const programs = (custom && custom.length > 0)
          ? custom
            .map(c => {
              const found = all.find(p => p.n === c.program);
              return found
                ? { n: found.n, name: (c.name || found.name), type: found.type }
                : null;
            })
            .filter((p): p is NonNullable<typeof p> => p !== null)
          : all;
        if (programs.length === 0) {
          this.log.warn(`Configured ${body} light scenes matched none of the `
            + `${all.length} available — skipping.`);
          continue;
        }
        const circuit = body === 'spa' ? 'AUX_1' : 'LIGHTS';
        const name = body === 'spa' ? 'Spa Light' : 'Pool Light';
        const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}-lighttv-${body}`);
        const acc = new this.api.platformAccessory(name, uuid);
        this.lightTvs.set(body, new LightTvAccessory(this, acc, body, circuit, programs));
        this.api.publishExternalAccessories(PLUGIN_NAME, [acc]);
        this.log.info(`Published "${name}" scene TV (${programs.length} scenes).`);
      } catch (err) {
        this.log.warn(`Light TV setup for ${body} failed:`, (err as Error).message);
      }
    }
  }

  private startPolling(): void {
    const poll = async () => {
      try {
        const status = await this.sidecar.getStatus();

        // Self-heal: if the sidecar restarted on its own it loses the pushed UI
        // config until the next Homebridge restart, leaving the cockpit to fall
        // back to panel-reported circuits (which include the AUX2 canary).
        // Re-push whenever we see it empty.
        if (!status.ui_circuits || status.ui_circuits.length === 0) {
          this.pushUiConfig();
        }

        this.currentValveMode = status.valve_mode;

        for (const [circuit, sw] of this.switches) {
          if (circuit === 'HEATER_1') {
            // Show enabled (Auto mode) not active-heating so the switch
            // stays on whenever the heater is armed, regardless of whether
            // it is currently calling for heat. Only update when the navigator
            // has confirmed the armed state — null means not yet read, so we
            // hold the last-known value rather than flickering to false.
            const heaterEnabled = status.valve_mode === 'spa'
              ? status.spa_heater_enabled
              : status.pool_heater_enabled;
            if (heaterEnabled !== null) {
              sw.updateState(heaterEnabled);
            }
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
          heaterActive: status.heater_active ?? false,
        };
        this.thermostatAuto?.updateState(ts);

        this.poolTempSensor?.updateTemperature(status.pool_temp);
        this.airTempSensor?.updateTemperature(status.air_temp);

        // Heater Running switch: read-only firing relay
        this.heaterRunning?.updateFiring(status.heater_active ?? false);

        const filterOn = status.circuits['FILTER'] ?? false;
        // Chlorinator % is body-specific: show the spa value in spa mode,
        // pool value otherwise. Both are updated by the idle LCD scroll.
        const chlorPct = status.valve_mode === 'spa'
          ? status.spa_chlorinator_percent
          : status.chlorinator_percent;
        this.chlorinatorFan?.updateSpeed(chlorPct);
        this.chlorinatorFan?.updateRunning(filterOn && (chlorPct ?? 0) > 0);
        this.saltSensor?.updateSaltLevel(status.salt_level);
        this.bridgeHealth?.updateWedged(status.bridge_wedged ?? false);

        // Light scene TVs: reconcile power from the real circuit state, and the
        // selected scene from the sidecar's last-sent program (survives plugin
        // restarts; scene selection itself is open-loop).
        this.lightTvs.get('spa')?.updateState(
          status.circuits['AUX_1'] ?? false, status.light_program?.['spa']);
        this.lightTvs.get('pool')?.updateState(
          status.circuits['LIGHTS'] ?? false, status.light_program?.['pool']);
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
