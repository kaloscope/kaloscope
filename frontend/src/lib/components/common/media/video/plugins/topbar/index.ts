import { icons, iconToSVG } from '$lib/icons';
import { historyBack } from '$lib/stores';
import { Events, Plugin } from 'xgplayer';

const { POSITIONS } = Plugin;

export default class TopBar extends Plugin {
  private clickTimeStamp = 0;

  static get pluginName() {
    return 'topBar';
  }

  static get defaultConfig() {
    return {
      position: POSITIONS.ROOT_TOP,
      index: 0,
      back: null,
      title: '',
      uploader: '',
      uploadedAt: ''
    };
  }

  get title(): string {
    return this.config.title || '';
  }

  get uploader(): string {
    const { uploader, uploadedAt } = this.config;
    return `${uploader ? `UP: ${uploader}` : ''}${uploader && uploadedAt ? ' ・ ' : ''}${uploadedAt}`;
  }

  private isClickDebounced() {
    const now = Date.now();
    if (now - this.clickTimeStamp < 300) {
      return true;
    }
    this.clickTimeStamp = now;
    return false;
  }

  onBackIconClick = (event: Event) => {
    if (this.isClickDebounced()) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (typeof this.config.back === 'function') {
      this.config.back();
    } else {
      historyBack();
    }
  };

  onSettingsIconClick = (event: Event) => {
    if (this.isClickDebounced()) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (this.player.config.settings) {
      this.player.config.settings.showModal();
    }
  };

  toggleMarquee() {
    const titleEl = this.root?.querySelector('.font-title');
    const titleCopyEl = titleEl?.querySelector('span:last-child');
    const titleParentEl = titleEl?.parentElement;
    if (titleEl && titleCopyEl && titleParentEl) {
      const scrollWidth = titleCopyEl.classList.contains('hidden') ? titleEl.scrollWidth : titleEl.scrollWidth / 2;
      if (scrollWidth > titleParentEl.clientWidth) {
        // enable marquee animation if title overflows
        titleEl.classList.add('animate-marquee');
        titleCopyEl.classList.remove('hidden');
        titleParentEl.classList.add('marquee-mask');
      } else {
        // disable marquee animation if title fits
        titleEl.classList.remove('animate-marquee');
        titleCopyEl.classList.add('hidden');
        titleParentEl.classList.remove('marquee-mask');
      }
    }
  }

  afterCreate() {
    this.bind('.back-icon', ['click', 'touchend'], this.onBackIconClick);
    this.bind('.settings-icon', ['click', 'touchend'], this.onSettingsIconClick);
    this.toggleMarquee();
    this.on(Events.VIDEO_RESIZE, () => {
      this.toggleMarquee();
    });
    this.on(Events.PLAYNEXT, () => {
      const newTitle = this.player.config.topBar.title;
      if (newTitle && newTitle !== this.title) {
        // update title when playNext with a different title
        this.config.title = newTitle;
        const titleEl = this.root?.querySelector('.font-title');
        if (titleEl) {
          titleEl.innerHTML = `
            <span class="pr-8!">${this.title}</span>
            <span class="pr-8! hidden">${this.title}</span>
          `;
          this.toggleMarquee();
        }
      }
    });
  }

  destroy() {
    this.unbind('.back-icon', ['click', 'touchend'], this.onBackIconClick);
    this.unbind('.settings-icon', ['click', 'touchend'], this.onSettingsIconClick);
  }

  render() {
    return `
    <div class="flex gap-4 w-full">
      <div class="pt-3! cursor-pointer back-icon">
        ${iconToSVG(icons.backSolid, 'size-5 text-white opacity-80')}
      </div>
      <div class="pt-2! flex flex-col truncate">
        <div>
          <div class="font-title font-medium text-lg text-white/80 w-max">
            <span class="pr-8!">${this.title}</span>
            <span class="pr-8! hidden">${this.title}</span>
          </div>
        </div>
        <div class="text-xs text-white/60">${this.uploader}</div>
      </div>
      <div class="pt-2.5! ml-auto! cursor-pointer settings-icon">
        ${iconToSVG(icons.moreVertical, 'size-6 text-white/80')}
      </div>
    </div>
    `;
  }
}
