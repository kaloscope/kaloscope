import type { _ } from '$lib/i18n';
import { isHTTPError } from 'ky';
import type { Readable } from 'svelte/store';

export type AssistantTranslator = typeof _ extends Readable<infer T> ? T : never;

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

  if (error instanceof Error && typeof error.message === 'string' && /^assistant_[a-z0-9_]+$/.test(error.message)) {
    return error.message;
  }

  return fallback;
}

/**
 * Resolve an assistant error code to assistant guidance, a shared alert, or a fallback line.
 *
 * @param translate - Active reactive i18n formatter.
 * @param code - Public application error code.
 * @param fallbackKey - Localization key used when neither `assistant.errors` nor `alert` matches.
 * @returns Localized error message suitable for display.
 */
export function errorMessage(
  translate: AssistantTranslator,
  code: string,
  fallbackKey = 'assistant.request_failed'
): string {
  return translate(`assistant.errors.${code}`, {
    default: translate(`alert.${code}`, { default: translate(fallbackKey) })
  });
}
