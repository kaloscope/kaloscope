import { UAParser } from 'ua-parser-js';

/**
 * Debounce function.
 *
 * @param func - The function to debounce.
 * @param timeout - The debounce timeout in milliseconds.
 * @param immediate - Whether to trigger the function immediately.
 * @returns The debounced function.
 */
export function debounce<F extends (...args: Parameters<F>) => ReturnType<F>>(
  func: F,
  timeout: number = 500,
  immediate: boolean = false
): (...args: Parameters<F>) => Promise<ReturnType<F> | undefined> {
  let timeoutId: ReturnType<typeof setTimeout> | null;
  return (...args: Parameters<F>) => {
    return new Promise((resolve, reject) => {
      if (timeoutId) {
        clearTimeout(timeoutId);
        if (immediate) {
          // resolve undefined if debounced
          resolve(undefined);
        }
      } else if (immediate) {
        try {
          resolve(func(...args));
        } catch (error) {
          reject(error);
        }
      }
      timeoutId = setTimeout(() => {
        if (!immediate) {
          try {
            resolve(func(...args));
          } catch (error) {
            reject(error);
          }
        }
        timeoutId = null;
      }, timeout);
    });
  };
}

/**
 * Throttle function.
 *
 * @param func - The function to throttle.
 * @param timeout - The throttle timeout in milliseconds.
 * @param immediate - Whether to trigger the function immediately.
 * @returns The throttled function.
 */
export function throttle<F extends (...args: Parameters<F>) => ReturnType<F>>(
  func: F,
  timeout: number = 500,
  immediate: boolean = true
): (...args: Parameters<F>) => Promise<ReturnType<F> | undefined> {
  let previous = 0;
  return (...args: Parameters<F>) => {
    return new Promise((resolve, reject) => {
      const now = Date.now();
      if (now - previous > timeout) {
        previous = now;
        if (immediate) {
          try {
            resolve(func(...args));
          } catch (error) {
            reject(error);
          }
        } else {
          setTimeout(() => {
            try {
              resolve(func(...args));
            } catch (error) {
              reject(error);
            }
          }, timeout);
        }
      } else {
        // resolve undefined if throttled
        resolve(undefined);
      }
    });
  };
}

/**
 * Extended Document interface for Fullscreen API methods.
 */
interface DocumentFullscreen extends Document {
  webkitFullscreenElement?: Element;
  webkitExitFullscreen?: () => void;
  webkitCurrentFullScreenElement?: Element;
  webkitCancelFullScreen?: () => void;
  mozFullScreenElement?: Element;
  mozCancelFullScreen?: () => void;
  msFullscreenElement?: Element;
  msExitFullscreen?: () => void;
}

/**
 * Extended HTMLElement interface for Fullscreen API methods.
 */
interface HTMLElementFullscreen extends HTMLElement {
  webkitRequestFullscreen?: () => void;
  webkitRequestFullScreen?: () => void;
  mozRequestFullScreen?: () => void;
  msRequestFullscreen?: () => void;
}

/**
 * Fullscreen API utilities.
 */
export const fullscreen = {
  enter: () => {
    const elm = document.documentElement as HTMLElementFullscreen;
    if (elm.requestFullscreen) {
      elm.requestFullscreen();
    } else if (elm.webkitRequestFullscreen) {
      elm.webkitRequestFullscreen();
    } else if (elm.webkitRequestFullScreen) {
      elm.webkitRequestFullScreen();
    } else if (elm.mozRequestFullScreen) {
      elm.mozRequestFullScreen();
    } else if (elm.msRequestFullscreen) {
      elm.msRequestFullscreen();
    }
  },
  exit: () => {
    const doc = document as DocumentFullscreen;
    if (doc.exitFullscreen) {
      doc.exitFullscreen();
    } else if (doc.webkitExitFullscreen) {
      doc.webkitExitFullscreen();
    } else if (doc.webkitCancelFullScreen) {
      doc.webkitCancelFullScreen();
    } else if (doc.mozCancelFullScreen) {
      doc.mozCancelFullScreen();
    } else if (doc.msExitFullscreen) {
      doc.msExitFullscreen();
    }
  },
  isFullscreen: () => {
    const doc = document as DocumentFullscreen;
    return !!(
      doc.fullscreenElement ||
      doc.webkitFullscreenElement ||
      doc.webkitCurrentFullScreenElement ||
      doc.mozFullScreenElement ||
      doc.msFullscreenElement
    );
  }
};

/**
 * Parsed user agent information.
 *
 * https://docs.uaparser.dev/api/main/overview.html
 */
const { browser, device, os, ua } = UAParser(navigator.userAgent);

/**
 * Sniffer utilities.
 */
export const sniffer = {
  isIos: () => {
    return os.name === 'iOS';
  },
  isIpad: () => {
    // https://github.com/bytedance/xgplayer/blob/main/packages/xgplayer/src/utils/sniffer.js
    return /(?:iPad|PlayBook)/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  },
  isSafari: () => {
    return browser.name === 'Safari' || browser.name === 'Mobile Safari';
  },
  isMobile: () => {
    return device.type === 'mobile';
  },
  isMobileSafari: () => {
    return browser.name === 'Mobile Safari';
  },
  isAndroid: () => {
    return os.name === 'Android' || os.name === 'Android-x86';
  },
  isAndroidEdge: () => {
    return (os.name === 'Android' || os.name === 'Android-x86') && browser.name === 'Edge';
  },
  isTablet: () => {
    return device.type === 'tablet';
  },
  isDesktop: () => {
    return device.type === 'desktop' || !device.type;
  }
};

/**
 * Load a file as a base64 string from an input event target.
 *
 * @param target - The event target of a file input change event.
 * @returns A promise that resolves to the base64 string.
 */
export function loadFile(target: EventTarget & HTMLInputElement): Promise<string | null> {
  return new Promise((resolve, reject) => {
    if (target.files && target.files[0]) {
      const reader = new FileReader();
      reader.onload = (event) => {
        resolve(event.target?.result as string);
      };
      reader.onerror = (error) => {
        reject(error);
      };
      reader.readAsDataURL(target.files[0]);
    } else {
      resolve(null);
    }
  });
}

/**
 * Escape HTML special characters in a string to prevent XSS attacks.
 *
 * @param str - The string to escape.
 * @returns The escaped string.
 */
export function escapeHTML(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Check if a color is white.
 *
 * @param color - The color string to check.
 */
export function isWhite(color: string | null | undefined): boolean {
  if (!color) {
    return false;
  }
  // normalize the color code
  const code = color.trim().toLowerCase().replace(/\s+/g, '');
  // check common white color codes
  const whiteCodes = ['#ffffff', '#fff', 'rgb(255,255,255)', 'hsl(0,0%,100%)', 'white'];
  return whiteCodes.includes(code);
}

/**
 * Convert an ISO 8601 datetime string to the format required by datetime-local inputs (yyyy-MM-ddTHH:mm),
 * expressed in the user's local timezone.
 *
 * @param value - The ISO 8601 datetime string.
 * @returns The formatted local datetime string.
 */
export function toDatetimeLocal(value: string | null | undefined): string {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (isNaN(date.getTime())) {
    return '';
  }
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/**
 * Calculate the aspect ratio from a string.
 *
 * @param ratio - A CSS aspect-ratio string, e.g. "16/9", "9/16", "0.75", "auto".
 */
export function aspectRatio(ratio: number | string | null | undefined): number {
  if (typeof ratio === 'number') {
    return Math.max(0, ratio);
  }
  if (!ratio || ratio.trim().toLowerCase() === 'auto') {
    return 0;
  }
  try {
    const [w, h = '1'] = ratio.split('/');
    return Math.max(0, Number(w) / Number(h));
  } catch {
    return 0;
  }
}

/**
 * Convert a number to a fixed decimal string with bounds checking.
 *
 * @param value - The number to format, can be a number, string, null, or undefined.
 * @param decimals - The number of decimal places to include (default is 1).
 * @param min - The minimum value to return (default is null).
 * @param max - The maximum value to return (default is null).
 * @returns A string representing the fixed number, or an empty string if the input is invalid.
 */
export function fixedNumber(
  value: number | string | null | undefined,
  decimals: number = 1,
  min: number | null = null,
  max: number | null = null
): string {
  if (value === null || value === undefined) {
    return '';
  }
  const num = Number(value);
  if (Number.isNaN(num)) {
    return '';
  }
  if (min !== null && num <= min) {
    // min=0 will return empty string instead of "0",
    // which is more suitable for rating displays
    return min ? min.toString() : '';
  }
  if (max !== null && num >= max) {
    return max.toString();
  }
  return num.toFixed(decimals);
}
