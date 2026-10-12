'use strict';

const os = require('os');
const { Bonjour } = require('bonjour-service');

const SERVICE_TYPE = 'qevosagent';

/**
 * Publish the dashboard through DNS-SD/mDNS. Discovery is best-effort: a
 * missing multicast route or a locked-down firewall must never prevent the
 * dashboard itself from starting.
 */
function startMdnsAdvertisement({ port, host, instanceName, version, instanceId, log = console }) {
  const disabled = /^(0|false|off)$/i.test(process.env.QEVOS_MDNS || '');
  const loopbackOnly = host === '127.0.0.1' || host === 'localhost' || host === '::1';
  if (disabled || loopbackOnly) return null;

  let bonjour;
  try {
    bonjour = new Bonjour({}, error => {
      log.warn(`  mDNS     : ${error && error.message ? error.message : error}`);
    });
    const displayName = (instanceName || os.hostname() || 'QevosAgent').trim();
    const service = bonjour.publish({
      name: `${displayName} (${port})`,
      type: SERVICE_TYPE,
      protocol: 'tcp',
      port,
      txt: {
        id: instanceId,
        name: displayName,
        version: String(version || 'dev'),
        api: '1',
        path: '/',
      },
    });
    service.on('up', () => log.log(`  mDNS     : ${service.name}._${SERVICE_TYPE}._tcp.local`));
    service.on('error', error => log.warn(`  mDNS     : ${error.message || error}`));
    return bonjour;
  } catch (error) {
    if (bonjour) bonjour.destroy();
    log.warn(`  mDNS     : unavailable (${error.message || error})`);
    return null;
  }
}

function stopMdnsAdvertisement(bonjour) {
  if (!bonjour) return;
  try { bonjour.unpublishAll(() => bonjour.destroy()); }
  catch { try { bonjour.destroy(); } catch {} }
}

module.exports = { SERVICE_TYPE, startMdnsAdvertisement, stopMdnsAdvertisement };
