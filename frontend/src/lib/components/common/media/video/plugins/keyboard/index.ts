import Keyboard from 'xgplayer/es/plugins/keyboard';

const PLAYBACK_RATE_HIDDEN = 'auto';
const PLAYBACK_RATE_VISIBLE = 'playbackrate';

/**
 * Keyboard plugin that reuses xgplayer mobile's playback-rate note for keyboard long-press forward.
 */
export default class KeyboardWithPlaybackRateNote extends Keyboard {
  private playbackRateTrigger: HTMLElement | null = null;
  private isForwardPlaybackRateActive = false;

  private onWindowBlur = () => {
    this.handleKeyUp(new Event('blur'));
  };

  private onWindowKeyUp = (event: KeyboardEvent) => {
    if (this.shouldStopForward(event)) {
      this.handleKeyUp(event);
    }
  };

  /**
   * Initializes the built-in keyboard shortcuts and registers cleanup for interrupted key presses.
   */
  afterCreate() {
    super.afterCreate();
    window.addEventListener('blur', this.onWindowBlur);
  }

  /**
   * Switches to the temporary forward playback rate and shows the matching note.
   *
   * @param event - The keyboard event that triggered the long-press action.
   */
  changePlaybackRate(event: Event) {
    super.changePlaybackRate(event);
    this.isForwardPlaybackRateActive = true;
    window.addEventListener('keyup', this.onWindowKeyUp, true);
    this.showPlaybackRateNote();
  }

  /**
   * Restores the original playback rate only when the forward key or window focus ends the long press.
   *
   * @param event - The keyup or blur event that may end the long-press action.
   */
  handleKeyUp(event: Event) {
    const shouldStop = this.shouldStopForward(event);
    if (this.isForwardPlaybackRateActive && !shouldStop) {
      this.resetKeyPressState();
      return;
    }
    super.handleKeyUp(event);
    if (shouldStop) {
      this.isForwardPlaybackRateActive = false;
      window.removeEventListener('keyup', this.onWindowKeyUp, true);
      this.hidePlaybackRateNote();
    }
  }

  /**
   * Removes listeners and the lazily-created note trigger when the player is destroyed.
   */
  destroy() {
    super.destroy();
    window.removeEventListener('blur', this.onWindowBlur);
    window.removeEventListener('keyup', this.onWindowKeyUp, true);
    this.playbackRateTrigger?.remove();
    this.playbackRateTrigger = null;
  }

  /**
   * Checks whether an event should stop the temporary forward playback rate.
   *
   * @param event - The keyup or blur event to check.
   * @returns Whether the event should restore the original playback rate.
   */
  private shouldStopForward(event: Event) {
    if (event.type === 'blur') {
      return true;
    }
    const keyboardEvent = event as KeyboardEvent;
    return keyboardEvent.key === 'ArrowRight';
  }

  /**
   * Resets the base key press state without restoring playback rate.
   */
  private resetKeyPressState() {
    this._keyState.isKeyDown = false;
    this._keyState.isPress = false;
    this._keyState.tt = 0;
  }

  /**
   * Shows the playback-rate note by switching the trigger action used by xgplayer mobile CSS.
   */
  private showPlaybackRateNote() {
    const trigger = this.getPlaybackRateTrigger();
    if (trigger) {
      trigger.dataset.xgAction = PLAYBACK_RATE_VISIBLE;
    }
  }

  /**
   * Hides the playback-rate note by restoring the trigger action to the idle state.
   */
  private hidePlaybackRateNote() {
    this.playbackRateTrigger?.setAttribute('data-xg-action', PLAYBACK_RATE_HIDDEN);
  }

  /**
   * Gets or creates the trigger element that uses xgplayer mobile's playback-rate note CSS.
   *
   * @returns The trigger element, or null if the player root is not ready.
   */
  private getPlaybackRateTrigger() {
    if (this.playbackRateTrigger) {
      this.updatePlaybackRateNote();
      return this.playbackRateTrigger;
    }
    if (!this.player.root) {
      return null;
    }

    const trigger = document.createElement('xg-trigger');
    trigger.className = 'keyboard-playbackrate-trigger';
    trigger.dataset.xgAction = PLAYBACK_RATE_HIDDEN;
    trigger.style.pointerEvents = 'none';
    trigger.setAttribute('aria-hidden', 'true');

    const note = document.createElement('div');
    note.className = 'xg-playbackrate xg-top-note';
    const span = document.createElement('span');
    const rate = document.createElement('i');
    span.appendChild(rate);
    note.appendChild(span);
    trigger.appendChild(note);
    this.player.root.appendChild(trigger);
    this.playbackRateTrigger = trigger;
    this.updatePlaybackRateNote();
    return trigger;
  }

  /**
   * Updates the note text from the current keyboard playback-rate config and xgplayer i18n.
   */
  private updatePlaybackRateNote() {
    const span = this.playbackRateTrigger?.querySelector('span');
    if (!span) {
      return;
    }
    let rate = span.querySelector('i');
    if (!rate) {
      rate = document.createElement('i');
      span.prepend(rate);
    }
    const i18n = this.i18n as unknown as { FORWARD?: string };
    rate.textContent = `${this.config.playbackRate}X`;
    span.replaceChildren(rate, i18n.FORWARD ?? 'forward');
  }
}
