// scrapebot profile-driven fingerprint layer.
//
// Injected as an init script into every frame before any page script runs.
// The OPTS placeholder below is replaced with a JSON object built from the active
// browser profile (see ResolvedProfile.fingerprint_options).
//
// Design rules:
//   * Never replace a property with a plain JS function: every override
//     reports "[native code]" from Function.prototype.toString, keeps the
//     native name/length, and still throws "Illegal invocation" on bad `this`.
//   * Noise is deterministic per identity (seeded), so repeated reads within
//     one identity are stable while different identities do not collide.
(() => {
  "use strict";
  const OPTS = __SCRAPEBOT_OPTS__;
  // No global marker is set on purpose: any new own-property on `window`
  // is visible to Object.getOwnPropertyNames(window) based detection.
  if (!OPTS) return;

  // ------------------------------------------------------------ utilities
  const nativeStrings = new WeakMap();
  const nativeToString = Function.prototype.toString;
  const toStringProxy = new Proxy(nativeToString, {
    apply(target, thisArg, args) {
      const masked = nativeStrings.get(thisArg);
      if (masked !== undefined) return masked;
      return Reflect.apply(target, thisArg, args);
    },
  });
  nativeStrings.set(toStringProxy, "function toString() { [native code] }");
  Object.defineProperty(Function.prototype, "toString", {
    ...Object.getOwnPropertyDescriptor(Function.prototype, "toString"),
    value: toStringProxy,
  });

  const markNative = (fn, name) => {
    nativeStrings.set(fn, `function ${name}() { [native code] }`);
    return fn;
  };

  // Replace an accessor's getter, keeping descriptor flags and native looks.
  const overrideGetter = (owner, prop, valueFn) => {
    const desc = owner && Object.getOwnPropertyDescriptor(owner, prop);
    if (!desc || typeof desc.get !== "function") return false;
    const original = desc.get;
    const getter = Object.getOwnPropertyDescriptor(
      {
        get [prop]() {
          // Run the native getter first so invalid receivers still throw
          // the genuine TypeError ("Illegal invocation").
          const nativeValue = original.call(this);
          return valueFn.call(this, nativeValue);
        },
      },
      prop
    ).get;
    markNative(getter, `get ${prop}`);
    Object.defineProperty(owner, prop, { ...desc, get: getter });
    return true;
  };

  // Wrap a method in a Proxy (keeps name/length) with a native toString.
  const wrapMethod = (owner, prop, handler) => {
    const desc = owner && Object.getOwnPropertyDescriptor(owner, prop);
    if (!desc || typeof desc.value !== "function") return false;
    const original = desc.value;
    const proxy = new Proxy(original, {
      apply(target, thisArg, args) {
        return handler(target, thisArg, args);
      },
    });
    markNative(proxy, original.name || prop);
    Object.defineProperty(owner, prop, { ...desc, value: proxy });
    return true;
  };

  // Small, fast, seeded integer hash (xorshift-multiply).
  const SEED = OPTS.seed >>> 0;
  const hash = (n) => {
    let h = (Math.imul((n ^ SEED) >>> 0, 0x9e3779b1) ^ 0x85ebca6b) >>> 0;
    h ^= h >>> 15;
    h = Math.imul(h, 0x2c1b3c6d) >>> 0;
    h ^= h >>> 12;
    return h >>> 0;
  };

  // ------------------------------------------------------------- hardware
  if (OPTS.hardware) {
    const navProto = Object.getPrototypeOf(navigator);
    const hw = OPTS.hardware;
    overrideGetter(navProto, "hardwareConcurrency", () => hw.hardwareConcurrency);
    // deviceMemory only exists in secure contexts.
    overrideGetter(navProto, "deviceMemory", () => hw.deviceMemory);
    overrideGetter(navProto, "maxTouchPoints", () => hw.maxTouchPoints);
  }

  // --------------------------------------------------------------- screen
  if (OPTS.screen) {
    const s = OPTS.screen;
    const screenProto = Object.getPrototypeOf(screen);
    for (const key of ["width", "height", "availWidth", "availHeight", "availLeft", "availTop", "colorDepth", "pixelDepth"]) {
      if (key in s) overrideGetter(screenProto, key, () => s[key]);
    }
    // Headless reports outer == inner. Real windows include the browser UI.
    const owner = (prop) => (Object.getOwnPropertyDescriptor(window, prop) ? window : Window.prototype);
    const innerW = Object.getOwnPropertyDescriptor(owner("innerWidth"), "innerWidth");
    const innerH = Object.getOwnPropertyDescriptor(owner("innerHeight"), "innerHeight");
    if (innerW && innerH && innerW.get && innerH.get) {
      overrideGetter(owner("outerWidth"), "outerWidth", function () {
        return Math.min(s.width, innerW.get.call(window));
      });
      overrideGetter(owner("outerHeight"), "outerHeight", function () {
        return Math.min(s.availHeight, innerH.get.call(window) + s.chromeHeight);
      });
    }
  }

  // ---------------------------------------------------------------- WebGL
  if (OPTS.webgl) {
    const gl = OPTS.webgl;
    const UNMASKED_VENDOR = 0x9245; // 37445
    const UNMASKED_RENDERER = 0x9246; // 37446
    const extra = gl.parameters || {};
    const patchContext = (Ctor) => {
      if (typeof Ctor !== "function") return;
      wrapMethod(Ctor.prototype, "getParameter", (target, self, args) => {
        // Call through first: preserves native errors and GL error state
        // (e.g. UNMASKED_* returns null until WEBGL_debug_renderer_info is enabled).
        const real = Reflect.apply(target, self, args);
        const pname = args[0];
        if (real === null || real === undefined) return real;
        if (pname === UNMASKED_VENDOR) return gl.vendor;
        if (pname === UNMASKED_RENDERER) return gl.renderer;
        if (Object.prototype.hasOwnProperty.call(extra, String(pname))) {
          const value = extra[String(pname)];
          if (ArrayBuffer.isView(real) && Array.isArray(value)) return new real.constructor(value);
          return value;
        }
        return real;
      });
    };
    patchContext(window.WebGLRenderingContext);
    patchContext(window.WebGL2RenderingContext);
  }

  // --------------------------------------------------------------- canvas
  if (OPTS.canvas) {
    const MAX_PIXELS = 4 * 1024 * 1024;
    const createElement = Document.prototype.createElement;
    const ctx2dProto = CanvasRenderingContext2D.prototype;
    const nativeGetImageData = ctx2dProto.getImageData;
    const nativeDrawImage = ctx2dProto.drawImage;
    const nativePutImageData = ctx2dProto.putImageData;
    const nativeGetContext = HTMLCanvasElement.prototype.getContext;

    // Perturb only "edge" pixels (those differing from their left
    // neighbour): fingerprinting text/gradients changes, while solid fills
    // stay pixel-perfect, which defeats the common "is there noise?" probe.
    const noisify = (imageData) => {
      const d = imageData.data;
      const rowStride = imageData.width * 4;
      let pr = -1, pg = -1, pb = -1, pa = -1;
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], g = d[i + 1], b = d[i + 2], a = d[i + 3];
        const rowStart = i % rowStride === 0;
        if (!rowStart && a !== 0 && (r !== pr || g !== pg || b !== pb || a !== pa)) {
          const h = hash(i);
          if ((h & 7) === 0) {
            const c = i + ((h >>> 3) % 3);
            d[c] = d[c] ^ 1;
          }
        }
        pr = r; pg = g; pb = b; pa = a;
      }
      return imageData;
    };

    const noisedCopy = (canvas) => {
      const w = canvas.width, h = canvas.height;
      if (!w || !h || w * h > MAX_PIXELS) return null;
      const copy = createElement.call(document, "canvas");
      copy.width = w;
      copy.height = h;
      const ctx = nativeGetContext.call(copy, "2d");
      if (!ctx) return null;
      nativeDrawImage.call(ctx, canvas, 0, 0);
      const data = nativeGetImageData.call(ctx, 0, 0, w, h);
      nativePutImageData.call(ctx, noisify(data), 0, 0);
      return copy;
    };

    const viaNoisedCopy = (target, self, args) => {
      if (self instanceof HTMLCanvasElement) {
        try {
          const copy = noisedCopy(self);
          if (copy) return Reflect.apply(target, copy, args);
        } catch (e) {
          // e.g. tainted canvas: fall through so the native error surfaces.
        }
      }
      return Reflect.apply(target, self, args);
    };
    wrapMethod(HTMLCanvasElement.prototype, "toDataURL", viaNoisedCopy);
    wrapMethod(HTMLCanvasElement.prototype, "toBlob", viaNoisedCopy);
    wrapMethod(ctx2dProto, "getImageData", (target, self, args) => {
      const data = Reflect.apply(target, self, args);
      if (data && data.width * data.height <= MAX_PIXELS) noisify(data);
      return data;
    });
  }

  // ---------------------------------------------------------------- audio
  if (OPTS.audio && window.AudioBuffer) {
    const touched = new WeakSet();
    const nativeGetChannelData = AudioBuffer.prototype.getChannelData;
    const perturb = (data) => {
      if (touched.has(data)) return;
      touched.add(data);
      for (let i = 0; i < data.length; i += 97) {
        // Leave true silence untouched: noise on a silent buffer is a tell.
        if (data[i] === 0) continue;
        const h = hash(i + 7919);
        // Relative perturbation of at most 1e-5: inaudible, survives float32 rounding.
        data[i] *= 1 + ((h % 2001) - 1000) * 1e-8;
      }
    };
    wrapMethod(AudioBuffer.prototype, "getChannelData", (target, self, args) => {
      const data = Reflect.apply(target, self, args);
      perturb(data);
      return data;
    });
    wrapMethod(AudioBuffer.prototype, "copyFromChannel", (target, self, args) => {
      try {
        perturb(nativeGetChannelData.call(self, args[1]));
      } catch (e) {
        // invalid channel etc.: let the native call raise the real error
      }
      return Reflect.apply(target, self, args);
    });
  }
})();
