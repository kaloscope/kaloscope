import { getCurrentRole } from '$lib/api';
import { icons } from '$lib/icons';
import type { Menu } from '$lib/types';
import type { LayoutLoad } from './$types';

export const load: LayoutLoad = async () => {
  const menus: Menu[] = [
    {
      title: 'nav.settings.personal.title',
      routes: [
        {
          title: 'nav.settings.personal.account',
          path: '/settings/personal/account',
          icon: icons.lockClosedKey
        },
        {
          title: 'nav.settings.personal.sessions',
          path: '/settings/personal/sessions',
          icon: icons.phoneTablet
        },
        {
          title: 'nav.settings.personal.preferences',
          path: '/settings/personal/preferences',
          icon: icons.textGrammarSettings
        }
      ]
    },
    {
      title: 'nav.settings.help.title',
      routes: [
        {
          title: 'nav.settings.help.doc',
          path: 'https://kaloscope.org/docs/faq',
          icon: icons.bookQuestionMark
        },
        {
          title: 'nav.settings.help.issue',
          path: 'https://github.com/kaloscope/kaloscope/issues',
          icon: icons.chatWarning
        }
      ]
    }
  ];

  if ((await getCurrentRole()) === 'admin') {
    menus.splice(
      1,
      0,
      {
        title: 'nav.settings.workflows.title',
        routes: [
          {
            title: 'nav.settings.workflows.templates',
            path: '/settings/workflows/templates',
            icon: icons.appStore
          },
          {
            title: 'nav.settings.workflows.variables',
            path: '/settings/workflows/variables',
            icon: icons.bracesVariable
          },
          {
            title: 'nav.settings.workflows.graphs',
            path: '/settings/workflows/graphs',
            icon: icons.documentFlowchart
          },
          {
            title: 'nav.settings.workflows.schedules',
            path: '/settings/workflows/schedules',
            icon: icons.clock
          }
        ]
      },
      {
        title: 'nav.settings.system.title',
        routes: [
          {
            title: 'nav.settings.system.users',
            path: '/settings/system/users',
            icon: icons.peopleSettings
          },
          {
            title: 'nav.settings.system.network',
            path: '/settings/system/network',
            icon: icons.globeDesktop
          },
          {
            title: 'nav.settings.system.general',
            path: '/settings/system/general',
            icon: icons.settings
          },
          {
            title: 'nav.settings.system.medialibs',
            path: '/settings/system/medialibs',
            icon: icons.videoClipMultiple
          },
          {
            title: 'nav.settings.system.downloaders',
            path: '/settings/system/downloaders',
            icon: icons.box3dDownload
          }
        ]
      },
      {
        title: 'nav.settings.monitor.title',
        routes: [
          {
            title: 'nav.settings.monitor.transcodes',
            path: '/settings/monitor/transcodes',
            icon: icons.arrowRepeatAll
          },
          {
            title: 'nav.settings.monitor.logs',
            path: '/settings/monitor/logs',
            icon: icons.clipboardTextLtr
          }
        ]
      }
    );
  }

  return { menus };
};
