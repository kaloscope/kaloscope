import { icons, iconToSVG } from '$lib/icons';
import type { Definition } from '$lib/types';
import { isTranscodedStream } from '$lib/utils';
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

export default class Definitions extends OptionsIcon {
  static get pluginName() {
    return 'definitions';
  }

  static get defaultConfig() {
    return {
      ...OptionsIcon.defaultConfig,
      className: 'xgplayer-definitions',
      isShowIcon: true,
      index: 99,
      heightLimit: false,
      hidePortrait: false
    };
  }

  onIconClick = () => {
    for (const name of ['chapters', 'playbackRate']) {
      const plugin = this.player.getPlugin(name);
      if (plugin && plugin.optionsList && plugin.isActive) {
        plugin.optionsList.hide();
        plugin.isActive = false;
      }
    }
  };

  onItemClick = (event: DelegateEvent, data: ChangeData) => {
    super.onItemClick(event, data);
    const { url } = data.to;
    if (typeof url === 'string') {
      this.player.config.settings.changeDefinition(url);
    }
  };

  registerIcons() {
    return {
      definitions: {
        icon: iconToSVG(icons.shadow),
        class: 'size-6 text-white/80'
      }
    };
  }

  afterCreate() {
    super.afterCreate();
    this.renderItemList();
    this.on('url_change', (url: string) => {
      this.player.config.url = url;
      this.renderItemList();
    });
    this.bind('click', this.onIconClick);
  }

  renderItemList() {
    this.curIndex = -1;
    let url = this.player.config.url;
    if (typeof url === 'string' && isTranscodedStream(url)) {
      url = url.replace('&transcode=true', '');
    }
    const items = ((this.config.list as Definition[] | undefined) ?? []).map((item, index) => {
      const definitionItem = {
        url: item.url,
        showText: String(item.definition),
        selected: item.url === url
      };
      if (definitionItem.selected) {
        this.curIndex = index;
      }
      return definitionItem;
    });
    super.renderItemList(items, this.curIndex);
    this.optionsList?.root.classList.add('xgplayer-definitions');
  }

  show() {
    if (this.config.list && this.config.list.length > 0) {
      super.show();
    }
  }

  destroy() {
    super.destroy();
    this.unbind('click', this.onIconClick);
  }
}
