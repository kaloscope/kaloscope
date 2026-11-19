import { icons, iconToSVG } from '$lib/icons';
import type { Definition } from '$lib/types';
import { isTranscodedStream } from '$lib/utils/media';
import { escapeHTML } from '$lib/utils/text';
import OptionsIcon from 'xgplayer/es/plugins/common/optionsIcon';
import './index.css';

/**
 * Option attributes used by xgplayer's delegated option list.
 */
type AttrObject = {
  [key: string]: string | number | undefined;
  index?: number;
};

/**
 * Previous and selected definition option values.
 */
type ChangeData = {
  from: AttrObject | null;
  to: AttrObject;
};

/**
 * A delegated DOM event emitted by xgplayer's option list.
 */
type DelegateEvent = Event & {
  delegateTarget: Element;
};

/**
 * Definition selector backed by the app playback source switcher.
 */
export default class Definitions extends OptionsIcon {
  /**
   * Xgplayer plugin name.
   */
  static get pluginName() {
    return 'definitions';
  }

  /**
   * Default definition selector configuration.
   */
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

  /**
   * Close other option menus before opening the definition selector.
   */
  onIconClick = () => {
    for (const name of ['chapters', 'playbackRate']) {
      const plugin = this.player.getPlugin(name);
      if (plugin && plugin.optionsList && plugin.isActive) {
        plugin.optionsList.hide();
        plugin.isActive = false;
      }
    }
  };

  /**
   * Switch playback to the selected definition.
   *
   * @param event - Delegated option click event.
   * @param data - Previous and selected option values.
   */
  onItemClick = (event: DelegateEvent, data: ChangeData) => {
    super.onItemClick(event, data);
    const { url } = data.to;
    if (typeof url === 'string') {
      this.player.config.settings.changePlaybackSource(url);
    }
  };

  /**
   * Register the app definition icon.
   */
  registerIcons() {
    return {
      definitions: {
        icon: iconToSVG(icons.shadow),
        class: 'size-6 text-white/80'
      }
    };
  }

  /**
   * Initialize options and synchronize external source changes.
   */
  afterCreate() {
    super.afterCreate();
    this.renderItemList();
    this.on('url_change', (url: string) => {
      // refresh selection after source changes outside this menu
      this.player.config.url = url;
      this.renderItemList();
    });
    this.bind('click', this.onIconClick);
  }

  /**
   * Render definition options and select the active source.
   */
  renderItemList() {
    this.curIndex = -1;
    let url = this.player.config.url;
    if (typeof url === 'string' && isTranscodedStream(url)) {
      // compare transcoded playback against its direct definition URL
      url = url.replace('&transcode=true', '');
    }
    const items = ((this.config.list as Definition[] | undefined) ?? []).map((item, index) => {
      const definitionItem = {
        url: item.url,
        // xgplayer inserts `showText` as HTML, and labels come from scraped sites
        showText: escapeHTML(String(item.definition)),
        selected: item.url === url
      };
      if (definitionItem.selected) {
        this.curIndex = index;
      }
      return definitionItem;
    });
    super.renderItemList(items, this.curIndex);
    this.optionsList?.root?.classList.add('xgplayer-definitions');
  }

  /**
   * Show the selector when definitions are available.
   */
  show() {
    if (this.config.list && this.config.list.length > 0) {
      super.show();
    }
  }

  /**
   * Remove definition selector event binding.
   */
  destroy() {
    super.destroy();
    this.unbind('click', this.onIconClick);
  }
}
