import { getCurrentRole } from '$lib/api';
import { icons } from '$lib/icons';
import type { Navigation } from '$lib/types';
import type { LayoutLoad } from './$types';

/**
 * Build application navigation for the current user.
 * @returns Data used by the layout or page.
 */
export const load: LayoutLoad = async () => {
  const navs: Navigation[] = [
    {
      title: 'nav.dashboard.title',
      path: '/dashboard',
      icon: icons.dashboardBar,
      iconFilled: icons.dashboardBarFill,
      mobile: true
    },
    {
      title: 'nav.websearch.title',
      path: '/websearch',
      icon: icons.globeSearch,
      iconFilled: icons.globeSearchFilled,
      mobile: true,
      drawerStyle: 'menu'
    },
    {
      title: 'nav.medialibs.title',
      path: '/medialibs',
      icon: icons.videoClipMultiple,
      iconFilled: icons.videoClipMultipleFilled,
      mobile: true,
      drawerStyle: 'menu'
    },
    {
      title: 'nav.settings.title',
      path: '/settings',
      icon: icons.settings,
      iconFilled: icons.settingsFilled,
      mobile: true,
      drawerStyle: 'menu'
    }
  ];

  if ((await getCurrentRole(true)) === 'admin') {
    navs.splice(3, 0, {
      title: 'nav.downloads.title',
      path: '/downloads',
      icon: icons.box3dDownload,
      iconFilled: icons.box3dDownloadFill,
      mobile: true,
      drawerStyle: 'menu'
    });
  }

  return { navs };
};
