import { goto } from '$app/navigation';
import { alert } from '$lib/components';
import { UserRole } from '$lib/enums';
import { token, user } from '$lib/stores';
import type { BaseResp, Resp, User } from '$lib/types';
import ky, { isHTTPError } from 'ky';
import { get } from 'svelte/store';

/**
 * The API client for the application.
 */
export const api = ky.create({
  prefix: '/_api',
  timeout: 60000,
  hooks: {
    beforeRequest: [
      ({ request }) => {
        const _token = get(token);
        if (_token) {
          // authorization tokens in the form `Token <token>` or `Bearer <token>` are supported
          // see https://sanic.dev/en/guide/basics/headers.html for more details
          request.headers.set('Authorization', `Token ${_token}`);
        }
      }
    ],
    afterResponse: [
      async ({ response }) => {
        if (response.ok && response.headers.get('Content-Type') === 'application/json') {
          const resp = await response.json<BaseResp>();
          if (resp.message) {
            alert({ level: 'success', message: resp.message });
          }
        }
      }
    ],
    beforeError: [
      async ({ error }) => {
        if (isHTTPError(error)) {
          const resp = error.data as BaseResp;

          // goto login page if unauthorized
          if (resp.status === 401) {
            token.set(null);
            goto('/login');
          }

          // alert error message
          error.message = resp.message;
          if (resp.message || resp.status === 500) {
            alert({
              level: 'error',
              message: resp.message || 'internal_server_error',
              unique: resp.status === 401
            });
            return error;
          }

          // log other errors
          console.error(error);
        }
        return error;
      }
    ]
  }
});

/**
 * Proxies the image URL through the server.
 *
 * @param url - The original URL of the image.
 * @param policy - The policy for proxying the image.
 * @returns The proxied URL of the image.
 */
export function proxyImage(url: string | null, policy: boolean | 'auto' | 'store' = false): string | null {
  if (!url) {
    return url;
  }

  // serve icons and avatars directly from the API endpoint
  if (url.startsWith('icons/') || url.startsWith('avatars/')) {
    return `/_api/${url}`;
  }

  // return the original URL
  if (policy === false) {
    return url;
  }

  const buildProxyUrl = (store: boolean): string => `/_api/image/proxy?store=${store}&url=${encodeURIComponent(url)}`;

  // only proxy the image if the URL contains the query parameter `proxy=store` or `proxy=true`
  if (policy === 'auto') {
    const [, query = ''] = url.split('?', 2);
    const proxy = new URLSearchParams(query).get('proxy');
    if (proxy === 'store') {
      return buildProxyUrl(true);
    }
    if (proxy !== 'true') {
      return url;
    }
    return buildProxyUrl(false);
  }

  // proxy the image and store it if the policy is 'store'
  if (policy === 'store') {
    return buildProxyUrl(true);
  }

  // proxy the image if the policy is true
  return buildProxyUrl(false);
}

/**
 * Get the current user's role, either from the store or by making an API call.
 *
 * @returns The current user's role.
 */
export async function getCurrentRole(): Promise<keyof typeof UserRole | null> {
  const role = get(user)?.role;
  if (role) {
    return role;
  }
  try {
    const resp = await api.get('auth/current').json<Resp<User>>();
    return resp.data.role ?? null;
  } catch {
    return null;
  }
}
