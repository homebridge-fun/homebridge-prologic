import type { API } from 'homebridge';
import { PLATFORM_NAME } from './settings';
import { ProLogicPlatform } from './platform';

export default (api: API) => {
  api.registerPlatform(PLATFORM_NAME, ProLogicPlatform);
};
