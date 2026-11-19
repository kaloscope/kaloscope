import { icons, iconToSVG } from '$lib/icons';
import { historyBack } from '$lib/stores.svelte';
import { escapeHTML } from '$lib/utils/text';
import { Events, Plugin } from 'xgplayer';

const { POSITIONS } = Plugin;

/**
 * Player top bar with navigation, media metadata, and settings access.
 */
export default class TopBar extends Plugin {
  /**
   * Last accepted control event timestamp.
   */
  private clickTimeStamp = 0;

  /**
   * Xgplayer plugin name.
   */
  static get pluginName() {
    return 'topBar';
  }

  /**
   * Default top bar configuration.
   */
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

  /**
   * Current media title.
   */
  get title(): string {
    return this.config.title || '';
  }

  /**
   * Formatted uploader and upload time label.
   */
  get uploader(): string {
    const { uploader, uploadedAt } = this.config;
    return `${uploader ? `UP: ${uploader}` : ''}${uploader && uploadedAt ? ' ・ ' : ''}${uploadedAt}`;
  }

  /**
   * Title markup with the marquee copy, escaped because titles come from scraped sites and media files.
   *
   * @returns HTML for the title element's children.
   */
  private titleHTML(): string {
    const title = escapeHTML(String(this.title));
    return `
            <span class="pr-8!">${title}</span>
            <span class="pr-8! hidden">${title}</span>
          `;
  }

  /**
   * Reject duplicate `touchend` and synthetic `click` events.
   *
   * @returns Whether the current event falls within the debounce window.
   */
  private isClickDebounced() {
    const now = Date.now();
    if (now - this.clickTimeStamp < 300) {
      return true;
    }
    this.clickTimeStamp = now;
    return false;
  }

  /**
   * Navigate back using the configured callback or app history.
   *
   * @param event - Control activation event.
   */
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

  /**
   * Open the player settings modal.
   *
   * @param event - Control activation event.
   */
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

  /**
   * Toggle the title marquee when its content overflows.
   */
  toggleMarquee() {
    const titleEl = this.root?.querySelector('.font-title');
    const titleCopyEl = titleEl?.querySelector('span:last-child');
    const titleParentEl = titleEl?.parentElement;
    if (titleEl && titleCopyEl && titleParentEl) {
      // the visible copy doubles the measured marquee width
      const scrollWidth = titleCopyEl.classList.contains('hidden') ? titleEl.scrollWidth : titleEl.scrollWidth / 2;
      if (scrollWidth > titleParentEl.clientWidth) {
        titleEl.classList.add('animate-marquee');
        titleCopyEl.classList.remove('hidden');
        titleParentEl.classList.add('marquee-mask');
      } else {
        titleEl.classList.remove('animate-marquee');
        titleCopyEl.classList.add('hidden');
        titleParentEl.classList.remove('marquee-mask');
      }
    }
  }

  /**
   * Bind controls and keep title layout synchronized with player events.
   */
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
        // keep the title in sync when chapter playback changes media
        this.config.title = newTitle;
        const titleEl = this.root?.querySelector('.font-title');
        if (titleEl) {
          titleEl.innerHTML = this.titleHTML();
          this.toggleMarquee();
        }
      }
    });
  }

  /**
   * Remove top bar control bindings.
   */
  destroy() {
    this.unbind('.back-icon', ['click', 'touchend'], this.onBackIconClick);
    this.unbind('.settings-icon', ['click', 'touchend'], this.onSettingsIconClick);
  }

  /**
   * Render top bar controls and media metadata.
   */
  render() {
    return `
    <div class="flex gap-4 w-full">
      <div class="pt-3! cursor-pointer back-icon">
        ${iconToSVG(icons.backSolid, 'size-5 text-white opacity-80')}
      </div>
      <div class="pt-2! flex flex-col truncate">
        <div>
          <div class="font-title font-medium text-lg text-white/80 w-max">${this.titleHTML()}</div>
        </div>
        <div class="text-xs text-white/60">${escapeHTML(this.uploader)}</div>
      </div>
      <div class="pt-2.5! ml-auto! cursor-pointer settings-icon">
        ${iconToSVG(icons.moreVertical, 'size-6 text-white/80')}
      </div>
    </div>
    `;
  }
}
