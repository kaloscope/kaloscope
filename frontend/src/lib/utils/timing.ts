/**
 * Debounce calls to a function.
 *
 * @param func - Function to debounce.
 * @param timeout - Debounce timeout in milliseconds.
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
          // resolve `undefined` if debounced
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
 * Throttle calls to a function.
 *
 * @param func - Function to throttle.
 * @param timeout - Throttle timeout in milliseconds.
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
        // resolve `undefined` if throttled
        resolve(undefined);
      }
    });
  };
}

/**
 * Repeat a callback on an interval while the page is visible.
 *
 * Polling pauses while the document is hidden, such as in a background tab, and resumes with an
 * immediate call when the page becomes visible again, so the data is fresh as soon as it is seen.
 *
 * @param callback - Function to call on each tick.
 * @param interval - Interval in milliseconds.
 * @returns A function that stops polling and removes the visibility listener.
 */
export function pollWhileVisible(callback: () => void, interval: number): () => void {
  let timer: ReturnType<typeof setInterval> | null = null;

  const start = () => {
    timer ??= setInterval(callback, interval);
  };
  const stop = () => {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
  const onVisibilityChange = () => {
    if (document.visibilityState === 'hidden') {
      stop();
    } else if (timer === null) {
      callback();
      start();
    }
  };

  if (document.visibilityState !== 'hidden') {
    start();
  }
  document.addEventListener('visibilitychange', onVisibilityChange);
  return () => {
    stop();
    document.removeEventListener('visibilitychange', onVisibilityChange);
  };
}
