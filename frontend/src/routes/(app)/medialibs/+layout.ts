import { api } from '$lib/api';
import { LibType } from '$lib/enums';
import type { MediaLib, Menu, Resp } from '$lib/types';
import type { LayoutLoad } from './$types';

/**
 * Load media library navigation and scan states.
 *
 * @param context - Load context used to register library dependencies.
 * @returns The library menus and their backing media libraries.
 */
export const load: LayoutLoad = async (context) => {
  context.depends('app:media-libraries');
  const menus: Menu[] = [];
  let libs: MediaLib[] = [];

  await api
    .get('media/lib/list')
    .json<Resp<MediaLib[]>>()
    .then(({ data }) => {
      libs = data;
      if (data.length > 0) {
        menus.push({
          title: 'nav.medialibs.title',
          routes: data.map((lib) => ({
            title: lib.name,
            path: `/medialibs/${lib.id}`,
            icon: LibType[lib.lib_type].icon,
            translate: false
          }))
        });
      }
    });

  return { menus, libs };
};
