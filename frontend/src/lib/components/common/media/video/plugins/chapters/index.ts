import { api } from '$lib/api';
import { icons, iconToSVG } from '$lib/icons';
import type { Chapter, Resp } from '$lib/types';
import { extractStreamPath, isTranscodedStream } from '$lib/utils';
import OptionList from 'xgplayer/es/plugins/common/optionList';
import OptionsIcon from 'xgplayer/es/plugins/common/optionsIcon';
import './index.css';

type AttrObject = {
  [key: string]: string | number | undefined;
  index?: number;
};

type ChangeData = {
  from: AttrObject | null;
  to: AttrObject;
};

type DelegateEvent = Event & {
  delegateTarget: Element;
};

export default class Chapters extends OptionsIcon {
  static get pluginName() {
    return 'chapters';
  }

  static get defaultConfig() {
    return {
      ...OptionsIcon.defaultConfig,
      className: 'xgplayer-chapters',
      isShowIcon: true,
      heightLimit: false,
      hidePortrait: false,
      chapterId: '',
      chapterChange: null
    };
  }

  onIconClick = () => {
    for (const name of ['definitions', 'playbackRate']) {
      const plugin = this.player.getPlugin(name);
      if (plugin && plugin.optionsList && plugin.isActive) {
        plugin.optionsList.hide();
        plugin.isActive = false;
      }
    }
  };

  onItemClick = (event: DelegateEvent, data: ChangeData) => {
    super.onItemClick(event, data);
    const { id, url, showText } = data.to;
    if (typeof this.config.chapterChange === 'function') {
      this.config.chapterChange({
        id: id,
        url: url,
        title: showText
      });
    } else if (typeof url === 'string') {
      this.playNext(url, showText);
    }
  };

  private async playNext(url: string, title: string | number | undefined) {
    let duration: number | undefined;
    if (isTranscodedStream(url)) {
      try {
        const path = extractStreamPath(url);
        const resp = await api.get('media/probe', { searchParams: { path } }).json<Resp<{ duration: number }>>();
        if (resp.data.duration > 0) {
          duration = resp.data.duration;
        }
      } catch {
        // probe failed
      }
    }
    this.player.playNext({ url, topBar: { title }, customDuration: duration });
  }

  registerIcons() {
    return {
      chapters: {
        icon: iconToSVG(icons.listCheck),
        class: 'size-6 text-white/80'
      }
    };
  }

  afterCreate() {
    super.afterCreate();
    this.renderItemList();
  }

  renderItemList() {
    const { config, optionsList, player } = this;

    this.curIndex = -1;
    const items = (config.list as Chapter[]).map((item, index) => {
      let url = player.config.url;
      if (typeof url === 'string' && isTranscodedStream(url)) {
        // remove transcode=true from url if it's a transcoded stream
        url = url.replace('&transcode=true', '');
      }
      const chapterItem = {
        id: item.id || '',
        url: item.url || '',
        showText: item.title,
        selected: (item.id || item.url) === (config.chapterId || url || '')
      };
      if (chapterItem.selected) {
        this.curIndex = index;
      }
      return chapterItem;
    });

    if (optionsList) {
      optionsList.renderItemList(items);
    } else {
      const isSide = config.listType === 'side';
      this.optionsList = new OptionList({
        root: isSide ? player.innerContainer || player.root : this.root,
        config: {
          data: items || [],
          className: isSide ? 'xg-right-side xg-side-list xgplayer-chapters' : '',
          domEventType: 'click',
          onItemClick: this.onItemClick
        }
      });
      this.show();
    }
  }

  show() {
    if (this.config.list && this.config.list.length > 0) {
      super.show();
    }
  }

  destroy() {
    super.destroy();
  }
}
