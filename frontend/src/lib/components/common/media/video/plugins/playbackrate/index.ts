import { Events } from 'xgplayer';
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

type RateItem = {
  rate: number;
  text: string;
  iconText?: string;
};

export default class PlaybackRate extends OptionsIcon {
  static get pluginName() {
    return 'playbackRate';
  }

  static get defaultConfig() {
    return {
      ...OptionsIcon.defaultConfig,
      className: 'xgplayer-playbackrate',
      isShowIcon: true,
      heightLimit: false,
      hidePortrait: false
    };
  }

  onIconClick = () => {
    for (const name of ['definitions', 'chapters']) {
      const plugin = this.player.getPlugin(name);
      if (plugin && plugin.optionsList && plugin.isActive) {
        plugin.optionsList.hide();
        plugin.isActive = false;
      }
    }
  };

  onItemClick = (event: DelegateEvent, data: ChangeData) => {
    super.onItemClick(event, data);
    const rate = Number(data.to.rate);
    if (rate && this.curValue !== rate) {
      this.curValue = rate;
      this.player.playbackRate = rate;
    }
  };

  afterCreate() {
    super.afterCreate();
    this.renderItemList();
    this.on(Events.RATE_CHANGE, () => {
      if (this.curValue !== this.player.playbackRate) {
        this.renderItemList();
      }
    });
    this.bind('click', this.onIconClick);
  }

  renderItemList() {
    this.curIndex = -1;
    this.curValue = this.player.playbackRate || 1;
    const items = (this.config.list as RateItem[]).map((item, index) => {
      const rateItem = {
        rate: item.rate,
        showText: this.getTextByLang(item, 'text', null),
        selected: this.curValue === item.rate
      };
      if (rateItem.selected) {
        this.curIndex = index;
      }
      return rateItem;
    });
    super.renderItemList(items, this.curIndex);
  }

  changeCurrentText() {
    if (this.isIcons) {
      return;
    }
    const iconText = this.find('.icon-text');
    if (iconText) {
      const rates: RateItem[] = this.config.list;
      const rate = rates[this.curIndex < rates.length ? this.curIndex : 0];
      if (rate) {
        iconText.innerHTML = this.getTextByLang(rate, 'iconText', null);
      } else {
        iconText.innerHTML = `${this.player.playbackRate.toFixed(1)}x`;
      }
    }
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
