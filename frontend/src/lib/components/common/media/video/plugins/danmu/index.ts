import { Events } from 'xgplayer';
import type Mp4Plugin from 'xgplayer-mp4';
import Danmu from 'xgplayer/es/plugins/danmu';

const MIN_RESTART_INTERVAL = 300;

/**
 * The built-in danmaku plugin stops and clears active comments for every seek.
 * xgplayer-mp4 also seeks forward by 0.5 seconds to recover from an in-buffer
 * stall, so that internal recovery must not be handled as a user seek.
 */
export default class DanmuWithMp4Recovery extends Danmu {
  private lastMp4WaitAdjustCount = 0;
  private ignoredRecoverySeek = false;
  private restartTimer: number | null = null;

  /**
   * Initializes the standard danmaku lifecycle with seek-source awareness.
   */
  afterCreate() {
    if (this.playerConfig.isLive) {
      this.config.isLive = true;
    }
    this.initDanmu();
    this.registerExtIcons();

    this.once(Events.TIME_UPDATE, () => {
      if (this.config.defaultOpen && !this.isUseClose) {
        this.start();
      }
    });
    this.on(Events.PAUSE, () => {
      if (this.isOpen && this.danmujs) {
        this.danmujs.pause();
      }
    });
    this.on(Events.PLAY, () => {
      if (this.isOpen && this.danmujs) {
        this.danmujs.play();
      }
    });
    this.on(Events.SEEKING, () => {
      this.handleSeeking();
    });
    this.on(Events.VIDEO_RESIZE, () => {
      this.resize();
    });
    this.on(Events.SEEKED, () => {
      this.handleSeeked();
    });
  }

  /**
   * Clears any pending danmaku restart before the base plugin is destroyed.
   */
  destroy() {
    this.clearRestartTimer();
    super.destroy();
  }

  /**
   * Stops active comments for real seeks while preserving them for the MP4
   * plugin's in-buffer recovery jump.
   */
  private handleSeeking() {
    this.ignoredRecoverySeek = this.isMp4RecoverySeek();

    if (this.ignoredRecoverySeek) {
      return;
    }
    this.seekCost = window.performance.now();
    if (!this.config.isLive && this.danmujs) {
      this.danmujs.stop();
    }
  }

  /**
   * Restarts comments after real seeks only.
   */
  private handleSeeked() {
    if (this.ignoredRecoverySeek) {
      this.ignoredRecoverySeek = false;
      return;
    }
    if (!this.danmujs || !this.isOpen) {
      return;
    }

    this.clearRestartTimer();
    const seekDuration = window.performance.now() - this.seekCost;
    const delay = seekDuration > MIN_RESTART_INTERVAL ? 100 : MIN_RESTART_INTERVAL;
    this.restartTimer = window.setTimeout(() => {
      this.danmujs?.start();
      this.restartTimer = null;
      if (this.player.paused) {
        this.danmujs?.pause();
      }
    }, delay);
  }

  /**
   * Identifies xgplayer-mp4's deterministic 0.5-second stall recovery seek
   * through the counter incremented immediately before that internal seek.
   */
  private isMp4RecoverySeek() {
    const mp4Plugin = this.player.getPlugin('mp4') as Mp4Plugin | null;
    if (!mp4Plugin) {
      return false;
    }

    const adjustCount = mp4Plugin._waitAdjustTimeCnt;
    if (adjustCount < this.lastMp4WaitAdjustCount) {
      // resynchronize defensively if the plugin clears its counter
      this.lastMp4WaitAdjustCount = adjustCount;
    }
    const hasNewAdjustment = adjustCount > this.lastMp4WaitAdjustCount;
    this.lastMp4WaitAdjustCount = adjustCount;

    return hasNewAdjustment;
  }

  /**
   * Cancels a delayed restart scheduled after a real seek.
   */
  private clearRestartTimer() {
    if (this.restartTimer !== null) {
      window.clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
  }
}
