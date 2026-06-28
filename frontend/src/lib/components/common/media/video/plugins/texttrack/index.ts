import { FULLSCREEN_CHANGE, VIDEO_RESIZE } from 'xgplayer/es/events';
import TextTrack from 'xgplayer/es/plugins/track';
import './index.css';

/**
 * Internal subtitle layout metadata maintained by xgplayer-subtitles.
 */
type SubtitleMeta = {
  scale: number;
  videoHeight: number;
  videoWidth: number;
  vBottom: number;
  marginBottom: number;
};

/**
 * The subset of xgplayer-subtitles used by the styled texttrack plugin.
 */
type SubtitleApi = {
  root?: HTMLElement;
  resize: (width: number, height: number) => void;
  _videoMeta?: SubtitleMeta;
};

/**
 * The media dimensions required for subtitle scale calculation.
 */
type VideoSize = Pick<HTMLVideoElement, 'videoWidth' | 'videoHeight'>;

/**
 * TextTrack plugin with rotate-fullscreen subtitle layout fixes.
 */
export default class StyledTextTrack extends TextTrack {
  /**
   * Whether the previous subtitle resize ran in rotate fullscreen mode.
   */
  private lastRotateFullscreen = false;

  /**
   * Initialize xgplayer TextTrack and subscribe to player size changes.
   */
  afterCreate() {
    super.afterCreate();
    this.on([FULLSCREEN_CHANGE, VIDEO_RESIZE], this.scheduleSubtitleResize);
    this.scheduleSubtitleResize();
  }

  /**
   * Render subtitle options and apply the custom option list class.
   */
  renderItemList() {
    super.renderItemList();
    this.optionsList?.root?.classList.add('xgplayer-texttrack');
    this.scheduleSubtitleResize();
  }

  /**
   * Keep xgplayer's control-focus repositioning out of rotate fullscreen.
   */
  rePosition() {
    if (this.player?.isRotateFullscreen) {
      this.scheduleSubtitleResize();
      return;
    }
    super.rePosition();
  }

  /**
   * Schedule subtitle resizing after xgplayer and ResizeObserver finish their own updates.
   */
  private scheduleSubtitleResize = () => {
    window.requestAnimationFrame(this.syncSubtitleResize);
    window.setTimeout(this.syncSubtitleResize, 80);
  };

  /**
   * Resize the subtitle renderer with the dimensions it needs for the current player mode.
   */
  private syncSubtitleResize = () => {
    const subTitles = this.subTitles as SubtitleApi | undefined;
    const subtitleRoot = subTitles?.root;
    const playerRoot = this.player?.root;
    if (!subTitles || !subtitleRoot || !playerRoot) {
      return;
    }

    const rect = playerRoot.getBoundingClientRect();
    if (!rect.width || !rect.height) {
      return;
    }

    if (!this.player.isRotateFullscreen) {
      if (!this.lastRotateFullscreen) {
        return;
      }
      this.lastRotateFullscreen = false;
      subtitleRoot.style.removeProperty('bottom');
      subtitleRoot.style.removeProperty('transform');
      subTitles.resize(rect.width, rect.height);
      return;
    }
    this.lastRotateFullscreen = true;

    const width = Math.max(rect.width, rect.height);
    const height = Math.min(rect.width, rect.height);
    const media = this.player.media as Partial<VideoSize> | null;
    const meta = subTitles._videoMeta;
    if (meta && !meta.scale && media?.videoWidth && media.videoHeight) {
      meta.videoWidth = media.videoWidth;
      meta.videoHeight = media.videoHeight;
      meta.scale = Math.floor((media.videoHeight / media.videoWidth) * 100);
    }
    if (!meta?.scale) {
      return;
    }

    subTitles.resize(width, height);
    const bottom = meta.vBottom + meta.marginBottom;
    if (Number.isFinite(bottom)) {
      subtitleRoot.style.setProperty('bottom', `${bottom}px`, 'important');
    }
    subtitleRoot.style.setProperty('transform', 'none', 'important');
  };
}
