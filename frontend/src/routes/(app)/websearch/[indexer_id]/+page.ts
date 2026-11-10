import { api } from '$lib/api';
import type { IndexerConfig, Resp } from '$lib/types';
import { redirect } from '@sveltejs/kit';
import { isHTTPError } from 'ky';
import type { PageLoad } from './$types';

/**
 * Load selected indexer configuration, falling back to the first search route when it is unavailable.
 *
 * @param event - Load event providing the route parameters and the parent layout data.
 * @returns Data used by the layout or page.
 */
export const load: PageLoad = async ({ params, parent }) => {
  const { menus } = await parent();
  const fallback = menus[0]?.routes[0]?.path ?? '/websearch';

  // check if the indexer ID is valid
  if (!menus[1]?.routes.some((r) => r.path === `/websearch/${params.indexer_id}`)) {
    redirect(307, fallback);
  }

  // get the indexer config
  try {
    const { data: config } = await api.get(`flow/indexer/${params.indexer_id}/config`).json<Resp<IndexerConfig>>();
    return { config };
  } catch (error) {
    // authentication failures are already redirected to login by the API client
    if (isHTTPError(error) && error.response.status === 401) {
      throw error;
    }
    // the API client has already reported the failure
    redirect(307, fallback);
  }
};
