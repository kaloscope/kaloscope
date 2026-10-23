import { isHTTPError } from 'ky';

/**
 * Convert request failures to existing translated application error codes.
 *
 * @param error - Transport or application failure.
 * @param fallback - Safe localized code when no public code is available.
 * @returns A public error code without upstream bodies.
 */
export function errorCode(error: unknown, fallback: string): string {
  if (isHTTPError(error)) {
    const message = (error.data as { message?: unknown } | undefined)?.message;
    if (typeof message === 'string' && /^[a-z][a-z0-9_]+$/.test(message)) return message;
  }
  if (error instanceof Error && error.message.startsWith('assistant_')) return error.message;
  return fallback;
}
