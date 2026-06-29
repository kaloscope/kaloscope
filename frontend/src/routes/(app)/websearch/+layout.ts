import { api } from '$lib/api';
import { icons } from '$lib/icons';
import type { FlowGraph, Menu, Page, Resp } from '$lib/types';
import type { LayoutLoad } from './$types';

export const load: LayoutLoad = async () => {
  const menus: Menu[] = [
    {
      title: 'nav.websearch.global.title',
      routes: [
        {
          title: 'nav.websearch.global.search',
          path: '/websearch/global',
          icon: icons.search
        },
        {
          title: 'nav.websearch.global.favorites',
          path: '/websearch/favorites',
          icon: icons.star
        }
      ]
    }
  ];

  await api
    .get('flow/graph/list', {
      searchParams: [
        ['page_num', 0],
        ['ordering', 'name'],
        ['category', 'indexer'],
        ['states', 'modified'],
        ['states', 'published']
      ]
    })
    .json<Resp<Page<FlowGraph>>>()
    .then(({ data }) => {
      let graphs = data.items;
      graphs = graphs.filter((g) => g.node_types.includes('search_start'));
      menus.push({
        title: 'nav.websearch.indexers',
        routes: graphs.map((graph) => ({
          title: graph.name,
          path: `/websearch/${graph.id}`,
          icon: graph.icon ?? icons.globe,
          translate: false
        }))
      });
    });

  return { menus };
};
