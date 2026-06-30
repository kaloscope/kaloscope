import { icons, iconToSVG } from '$lib/icons';
import { FULLSCREEN_CHANGE, VIDEO_RESIZE } from 'xgplayer/es/events';
import TextTrack from 'xgplayer/es/plugins/track';
import './index.css';

/**
 * Internal subtitle layout metadata maintained by the xgplayer subtitle renderer.
 */
type SubtitleMeta = {
  scale: number;
  videoHeight: number;
  videoWidth: number;
  vBottom: number;
  marginBottom: number;
};

/**
 * The subset of the xgplayer subtitle renderer used by the styled subtitle plugin.
 */
type SubtitleRendererApi = {
  config?: {
    mode?: 'stroke' | 'bg';
    offsetBottom?: number;
  };
  off?: (event: string, callback: () => void) => void;
  on?: (event: string, callback: () => void) => void;
  _ctime?: number;
  _getPlayerCurrentTime?: () => number;
  _patchedGetPlayerCurrentTime?: () => number;
  _onTimeupdate?: () => void;
  root?: HTMLElement;
  resize: (width: number, height: number) => void;
  _videoMeta?: SubtitleMeta;
};

/**
 * The media dimensions required for subtitle scale calculation.
 */
type VideoSize = Pick<HTMLVideoElement, 'videoWidth' | 'videoHeight'>;

/**
 * Subtitle settings applied to the xgplayer subtitle renderer.
 */
export type SubtitleStyleSettings = {
  displayMode: 'stroke' | 'bg';
  fontScale: number;
  verticalPosition: number;
  timeOffset: number;
};

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

/**
 * Styled subtitle plugin with rotate-fullscreen layout fixes.
 */
export default class StyledTextTrack extends TextTrack {
  /**
   * Whether the previous subtitle resize ran in rotate fullscreen mode.
   */
  private lastRotateFullscreen = false;

  /**
   * Subtitle timeline offset in seconds.
   */
  private subtitleTimeOffset = 0;

  /**
   * The subtitle renderer currently bound to layout update events.
   */
  private boundSubtitleRenderer?: SubtitleRendererApi;

  /**
   * Use the app subtitle icon instead of xgplayer's text fallback.
   */
  registerIcons() {
    const icon = iconToSVG(icons.subtitlesFilled);
    return {
      textTrackOpen: {
        icon,
        class: 'xg-texttrak-open size-6'
      },
      textTrackClose: {
        icon,
        class: 'xg-texttrak-close size-6'
      }
    };
  }

  /**
   * Initialize the xgplayer subtitle plugin and subscribe to player size changes.
   */
  afterCreate() {
    super.afterCreate();
    this.ensureIconText();
    this.bindSubtitleEvents();
    this.on([FULLSCREEN_CHANGE, VIDEO_RESIZE], this.requestSubtitleResize);
    this.requestSubtitleResize();
  }

  /**
   * Keep text label for non-portrait layouts while using a custom icon in portrait.
   */
  private ensureIconText() {
    const iconRoot = this.find('.xgplayer-icon');
    if (!iconRoot || this.find('.icon-text')) {
      return;
    }

    const iconText = document.createElement('span');
    iconText.className = 'icon-text';
    iconRoot.classList.add('btn-text');
    iconRoot.appendChild(iconText);
    this.unbind('click', this.onIconClick);
    this.isIcons = false;
    this.changeCurrentText();
  }

  /**
   * Render subtitle options and apply the custom option list class.
   */
  renderItemList() {
    super.renderItemList();
    this.optionsList?.root?.classList.add('xgplayer-texttrack');
    this.requestSubtitleResize();
  }

  /**
   * Remove subtitle renderer event listeners before destroying the plugin.
   */
  destroy() {
    this.unbindSubtitleEvents();
    super.destroy();
  }

  /**
   * Apply app subtitle settings to xgplayer's subtitle renderer.
   */
  applySubtitleSettings(settings: SubtitleStyleSettings) {
    this.bindSubtitleEvents();
    const displayMode = settings.displayMode === 'bg' ? 'bg' : 'stroke';
    const fontScale = clamp(settings.fontScale, 50, 200) / 100;
    const verticalPosition = clamp(settings.verticalPosition, 0, 15);
    this.subtitleTimeOffset = clamp(settings.timeOffset, -3600, 3600);

    this.config.style.mode = displayMode;
    this.config.style.offsetBottom = verticalPosition;
    this.player?.root?.style.setProperty('--xgplayer-subtitle-bottom', `${verticalPosition}%`);

    const subtitleRenderer = this.subTitles as SubtitleRendererApi | undefined;
    if (!subtitleRenderer) {
      return;
    }
    if (subtitleRenderer.config) {
      subtitleRenderer.config.mode = displayMode;
      subtitleRenderer.config.offsetBottom = verticalPosition;
    }
    if (subtitleRenderer.root) {
      subtitleRenderer.root.classList.toggle('text-track-stroke', displayMode === 'stroke');
      subtitleRenderer.root.classList.toggle('text-track-bg', displayMode === 'bg');
      subtitleRenderer.root.style.setProperty('--xgplayer-subtitle-font-scale', String(fontScale));
      subtitleRenderer.root.style.setProperty('--xgplayer-subtitle-bottom', `${verticalPosition}%`);
    }
    this.syncSubtitleTimeOffset(subtitleRenderer);
    this.deferSubtitleResize(true);
  }

  /**
   * Keep xgplayer's control-focus repositioning out of rotate fullscreen.
   */
  rePosition() {
    if (this.player?.isRotateFullscreen) {
      this.requestSubtitleResize();
      return;
    }
    super.rePosition();
  }

  /**
   * Request a deferred subtitle resize.
   */
  private requestSubtitleResize = () => {
    this.deferSubtitleResize();
  };

  /**
   * Request a forced subtitle resize when subtitle content or selection changes.
   */
  private requestForcedSubtitleResize = () => {
    this.deferSubtitleResize(true);
  };

  /**
   * Bind subtitle renderer events that require a forced layout refresh.
   */
  private bindSubtitleEvents() {
    const subtitleRenderer = this.subTitles as SubtitleRendererApi | undefined;
    if (!subtitleRenderer || subtitleRenderer === this.boundSubtitleRenderer) {
      return;
    }
    this.unbindSubtitleEvents();
    subtitleRenderer.on?.('change', this.requestForcedSubtitleResize);
    subtitleRenderer.on?.('reset', this.requestForcedSubtitleResize);
    this.boundSubtitleRenderer = subtitleRenderer;
  }

  /**
   * Unbind subtitle renderer events.
   */
  private unbindSubtitleEvents() {
    if (!this.boundSubtitleRenderer) {
      return;
    }
    this.boundSubtitleRenderer.off?.('change', this.requestForcedSubtitleResize);
    this.boundSubtitleRenderer.off?.('reset', this.requestForcedSubtitleResize);
    this.boundSubtitleRenderer = undefined;
  }

  /**
   * Defer subtitle resizing until after xgplayer and ResizeObserver finish their own updates.
   *
   * @param force - Whether to resize even when the player size has not changed.
   */
  private deferSubtitleResize(force = false) {
    window.requestAnimationFrame(() => this.syncSubtitleResize(force));
    window.setTimeout(() => this.syncSubtitleResize(force), 80);
    if (force) {
      window.setTimeout(() => this.syncSubtitleResize(true), 250);
    }
  }

  /**
   * Resize the subtitle renderer with the dimensions it needs for the current player mode.
   */
  private syncSubtitleResize = (force = false) => {
    const subtitleRenderer = this.subTitles as SubtitleRendererApi | undefined;
    const subtitleRoot = subtitleRenderer?.root;
    const playerRoot = this.player?.root;
    if (!subtitleRenderer || !subtitleRoot || !playerRoot) {
      return;
    }

    const rect = playerRoot.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      return;
    }

    const media = this.player.media as Partial<VideoSize> | null;
    const meta = subtitleRenderer._videoMeta;
    if (meta && !meta.scale && media?.videoWidth && media.videoHeight) {
      meta.videoWidth = media.videoWidth;
      meta.videoHeight = media.videoHeight;
      meta.scale = Math.floor((media.videoHeight / media.videoWidth) * 100);
    }
    if (!meta?.scale) {
      return;
    }

    if (!this.player.isRotateFullscreen) {
      const wasRotateFullscreen = this.lastRotateFullscreen;
      this.lastRotateFullscreen = false;
      if (!force && !wasRotateFullscreen) {
        return;
      }
      if (wasRotateFullscreen) {
        subtitleRoot.style.removeProperty('bottom');
        subtitleRoot.style.removeProperty('transform');
      }
      subtitleRenderer.resize(rect.width, rect.height);
      return;
    }
    this.lastRotateFullscreen = true;

    const width = Math.max(rect.width, rect.height);
    const height = Math.min(rect.width, rect.height);
    subtitleRenderer.resize(width, height);
    const bottom = meta.vBottom + meta.marginBottom;
    if (Number.isFinite(bottom)) {
      subtitleRoot.style.setProperty('bottom', `${bottom}px`, 'important');
    }
    subtitleRoot.style.setProperty('transform', 'none', 'important');
  };

  /**
   * Patch xgplayer's subtitle clock so subtitle offset updates take effect immediately.
   *
   * Positive offsets delay subtitles; negative offsets advance them.
   */
  private syncSubtitleTimeOffset(subtitleRenderer: SubtitleRendererApi) {
    const originalGetTime =
      subtitleRenderer._patchedGetPlayerCurrentTime ?? subtitleRenderer._getPlayerCurrentTime?.bind(subtitleRenderer);
    if (!originalGetTime) {
      return;
    }
    subtitleRenderer._patchedGetPlayerCurrentTime = originalGetTime;
    subtitleRenderer._getPlayerCurrentTime = () => Math.max(0, originalGetTime() - this.subtitleTimeOffset);
    subtitleRenderer._ctime = Number.NaN;
    subtitleRenderer._onTimeupdate?.();
  }
}
