"""Fingerprint self-check: what does a site see when it probes our browser?"""

from __future__ import annotations

from typing import Any

PROBE_URL = "https://fingerprint.scrapebot.test/"
PROBE_HTML = "<!doctype html><html><head><title>scrapebot fingerprint probe</title></head><body><p>probe</p></body></html>"

PROBE_JS = r"""
async () => {
  const out = {};
  const safe = async (name, fn) => { try { out[name] = await fn(); } catch (e) { out[name] = 'ERROR: ' + e.message; } };
  const nav = navigator;
  const getterSrc = (obj, prop) => {
    const d = Object.getOwnPropertyDescriptor(obj, prop);
    return d && d.get ? Function.prototype.toString.call(d.get) : null;
  };
  await safe('userAgent', () => nav.userAgent);
  await safe('platform', () => nav.platform);
  await safe('vendor', () => nav.vendor);
  await safe('language', () => nav.language);
  await safe('languages', () => Array.from(nav.languages));
  await safe('webdriver', () => nav.webdriver);
  await safe('hardwareConcurrency', () => nav.hardwareConcurrency);
  await safe('deviceMemory', () => nav.deviceMemory);
  await safe('maxTouchPoints', () => nav.maxTouchPoints);
  await safe('pluginsLength', () => nav.plugins.length);
  await safe('pdfViewerEnabled', () => nav.pdfViewerEnabled);
  await safe('isSecureContext', () => window.isSecureContext);
  await safe('userAgentData', async () => nav.userAgentData ? {
      brands: nav.userAgentData.brands,
      mobile: nav.userAgentData.mobile,
      platform: nav.userAgentData.platform,
      highEntropy: await nav.userAgentData.getHighEntropyValues(
        ['architecture', 'bitness', 'model', 'platformVersion', 'fullVersionList', 'wow64']),
    } : null);
  await safe('timezone', () => Intl.DateTimeFormat().resolvedOptions().timeZone);
  await safe('timezoneOffset', () => new Date().getTimezoneOffset());
  await safe('intlLocale', () => Intl.DateTimeFormat().resolvedOptions().locale);
  await safe('screen', () => ({
      width: screen.width, height: screen.height, availWidth: screen.availWidth,
      availHeight: screen.availHeight, colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth }));
  await safe('window', () => ({
      innerWidth, innerHeight, outerWidth, outerHeight, devicePixelRatio }));
  await safe('webgl', () => {
      const c = document.createElement('canvas');
      const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!gl) return null;
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      return {
        vendor: gl.getParameter(gl.VENDOR),
        renderer: gl.getParameter(gl.RENDERER),
        unmaskedVendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
        unmaskedRenderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
        maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
      };
  });
  await safe('canvasHash', () => {
      const c = document.createElement('canvas'); c.width = 240; c.height = 60;
      const x = c.getContext('2d');
      x.textBaseline = 'top'; x.font = '16px Arial'; x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
      x.fillStyle = '#069'; x.fillText('scrapebot \u{1F600} fp', 2, 15);
      x.fillStyle = 'rgba(102, 204, 0, 0.7)'; x.fillText('scrapebot \u{1F600} fp', 4, 17);
      const s = c.toDataURL(); let h = 0;
      for (let i = 0; i < s.length; i++) { h = (Math.imul(31, h) + s.charCodeAt(i)) | 0; }
      return (h >>> 0).toString(16);
  });
  await safe('canvasSolidFillClean', () => {
      const c = document.createElement('canvas'); c.width = 32; c.height = 32;
      const x = c.getContext('2d'); x.fillStyle = 'rgb(10,20,30)'; x.fillRect(0, 0, 32, 32);
      const d = x.getImageData(0, 0, 32, 32).data;
      for (let i = 0; i < d.length; i += 4) if (d[i] !== 10 || d[i+1] !== 20 || d[i+2] !== 30) return false;
      return true;
  });
  await safe('chrome', () => ({ present: typeof window.chrome === 'object', runtime: !!(window.chrome && window.chrome.runtime), app: !!(window.chrome && window.chrome.app) }));
  await safe('notificationPermission', () => typeof Notification !== 'undefined' ? Notification.permission : null);
  await safe('permissionsNotifications', async () => (await nav.permissions.query({ name: 'notifications' })).state);
  await safe('nativeLooking', () => ({
      hardwareConcurrencyGetter: getterSrc(Object.getPrototypeOf(nav), 'hardwareConcurrency'),
      getParameter: Function.prototype.toString.call(WebGLRenderingContext.prototype.getParameter),
      toDataURL: Function.prototype.toString.call(HTMLCanvasElement.prototype.toDataURL),
      toStringItself: Function.prototype.toString.call(Function.prototype.toString),
  }));
  await safe('illegalInvocationPreserved', () => {
      try { Object.getOwnPropertyDescriptor(Object.getPrototypeOf(nav), 'hardwareConcurrency').get.call({}); return false; }
      catch (e) { return e instanceof TypeError; }
  });
  await safe('ownWindowProps', () => Object.getOwnPropertyNames(window).filter(k => /scrapebot|playwright|stealth|__pw|cdc_/i.test(k)));
  return out;
}
"""


async def probe(page: Any) -> dict[str, Any]:
    return await page.evaluate(PROBE_JS)


async def open_probe_page(page: Any) -> None:
    """Navigate to an offline HTTPS page (secure context, no network traffic)."""

    async def fulfill(route: Any) -> None:
        await route.fulfill(status=200, content_type="text/html", body=PROBE_HTML)

    await page.route(PROBE_URL + "**", fulfill)
    await page.goto(PROBE_URL)
