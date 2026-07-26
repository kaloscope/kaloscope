import { icons, iconToSVG } from '$lib/icons';
import { isTranscodedStream, sniffer } from '$lib/utils';
import { I18N, type BasePlugin, type IPlayerOptions } from 'xgplayer';
import type { IUrl } from 'xgplayer/es/defaultConfig';
import FLV from 'xgplayer-flv';
import HLS from 'xgplayer-hls';
import MP4 from 'xgplayer-mp4';
import Shaka from 'xgplayer-shaka';
import Thumbnail from 'xgplayer/es/plugins/common/thumbnail';
import Enter from 'xgplayer/es/plugins/enter';
import Error from 'xgplayer/es/plugins/error';
import Fullscreen from 'xgplayer/es/plugins/fullscreen';
import Loading from 'xgplayer/es/plugins/loading';
import Mobile from 'xgplayer/es/plugins/mobile';
import PC from 'xgplayer/es/plugins/pc';
import PIP from 'xgplayer/es/plugins/pip';
import Play from 'xgplayer/es/plugins/play';
import PlayNext from 'xgplayer/es/plugins/playNext';
import Poster from 'xgplayer/es/plugins/poster';
import Progress from 'xgplayer/es/plugins/progress';
import MiniProgress from 'xgplayer/es/plugins/progress/miniProgress';
import ProgressPreview from 'xgplayer/es/plugins/progressPreview';
import Replay from 'xgplayer/es/plugins/replay';
import Rotate from 'xgplayer/es/plugins/rotate';
import Start from 'xgplayer/es/plugins/start';
import Time from 'xgplayer/es/plugins/time';
import TimeSegments from 'xgplayer/es/plugins/time/timesegments';
import Volume from 'xgplayer/es/plugins/volume';
import Chapters from './chapters';
import Danmu from './danmu';
import Definitions from './definitions';
import Gradient from './gradient';
import Keyboard from './keyboard';
import PlaybackRate from './playbackrate';
import TextTrack from './texttrack';
import TopBar from './topbar';

/**
 * A media pipeline plugin supported by the player preset.
 */
type VideoPlugin = typeof FLV | typeof HLS | typeof MP4 | typeof Shaka;

/**
 * App locale dictionaries eagerly loaded for xgplayer registration.
 */
const locales = import.meta.glob('$lib/locales/*.json', {
  eager: true,
  import: 'default'
});

// expose app media translations through xgplayer i18n
// https://h5player.bytedance.com/guide/i18n.html
Object.entries(locales).forEach(([path, content]) => {
  const lang = path.split('/').pop()?.slice(0, -5);
  const text = (content as { media?: Record<string, unknown> })?.media?.xgplayer;
  if (lang && text) {
    I18N.use({
      LANG: lang.toLowerCase(),
      TEXT: Object.fromEntries(Object.entries(text).map(([k, v]) => [k.toUpperCase(), v]))
    });
  }
});

/**
 * The base plugins for the video player.
 *
 * https://h5player.bytedance.com/guide/preset.html
 */
const BASE_PLUGINS = [
  Poster,
  Enter,
  Loading,
  Start,
  Play,
  PlayNext,
  Replay,
  Volume,
  Thumbnail,
  Fullscreen,
  Rotate,
  PIP,
  Error,
  TextTrack,
  Danmu,
  TopBar,
  Gradient,
  Definitions,
  Chapters,
  PlaybackRate
];

/**
 * Create the 12-bar spinner used by the `Loading` plugin.
 *
 * @returns The spinner element.
 */
function createLoadingSpinner(): HTMLElement {
  const enter = document.createElement('xg-enter');
  enter.className = 'xgplayer-enter xgplayer-loading-enter';
  const spinner = document.createElement('div');
  spinner.className = 'xgplayer-enter-spinner';
  for (let index = 1; index <= 12; index++) {
    const bar = document.createElement('div');
    bar.className = `xgplayer-enter-bar${index}`;
    spinner.appendChild(bar);
  }
  enter.appendChild(spinner);
  return enter;
}

/**
 * The custom icons for the player controls.
 *
 * https://h5player.bytedance.com/plugins/icons.html
 */
const ICONS = {
  play: iconToSVG(icons.playFilled),
  pause: iconToSVG(icons.pauseFilled),
  replay: iconToSVG(icons.redo, 'text-white drop-shadow-sm'),
  pipIcon: iconToSVG(icons.pictureInPictureEnter),
  pipIconExit: iconToSVG(icons.pictureInPictureExit),
  volumeLarge: iconToSVG(icons.speaker2Filled),
  volumeSmall: iconToSVG(icons.speaker1Filled),
  volumeMuted: iconToSVG(icons.speakerMuteFilled, 'text-white/40'),
  fullscreen: iconToSVG(icons.fullScreenMaximizeFilled),
  exitFullscreen: iconToSVG(icons.fullScreenMinimizeFilled),
  startPlay: iconToSVG(icons.playFilled, 'text-white !size-12'),
  startPause: iconToSVG(icons.pauseFilled, 'text-white !size-12'),
  loadingIcon: createLoadingSpinner
};

/**
 * App plugin preset with device and media pipeline selection.
 */
export default class DefaultPreset {
  /**
   * The plugins enabled for this player instance.
   */
  plugins: Partial<BasePlugin>[];

  /**
   * Build the player plugin list and shared media options.
   *
   * @param _ - The unused preset context supplied by xgplayer.
   * @param options - The player options updated by the preset.
   */
  constructor(_: unknown, options: IPlayerOptions) {
    this.plugins = [...BASE_PLUGINS];
    // live streams omit duration-based controls
    if (!options.isLive) {
      this.plugins.push(Time, TimeSegments, Progress, MiniProgress, ProgressPreview);
    }
    // select controls for the current input model
    if (sniffer.isMobile()) {
      this.plugins.push(Mobile);
    } else {
      this.plugins.push(PC, Keyboard);
    }
    if (sniffer.isIpad()) {
      // iPad needs desktop controls alongside touch gestures
      this.plugins.push(PC);
    }
    // select the media pipeline after device plugins are known
    this.plugins.push(...videoPlugins(options.videoType || guessVideoType(options.url), options.url));
    // prefer `ManagedMediaSource` when the browser supports it
    options.mp4Plugin = {
      preferMMS: true
    };
    options.flv = {
      preferMMS: true
    };
    options.hls = {
      preferMMS: true
    };
    options.icons = ICONS;
  }
}

/**
 * Guesses the video type based on the URL.
 *
 * @param url - The media resource URL.
 * @returns The guessed video type as a string.
 */
function guessVideoType(url?: IUrl): string | null {
  if (typeof url === 'string') {
    url = url.toLowerCase();
    if (url.indexOf('.mp4') > -1) {
      return 'mp4';
    } else if (url.indexOf('.flv') > -1) {
      return 'flv';
    } else if (url.indexOf('.m3u8') > -1) {
      return 'hls';
    } else if (url.indexOf('.mpd') > -1 || url.startsWith('data:application/dash+xml')) {
      return 'dash';
    }
  }
  return null;
}

/**
 * Gets the video plugins based on the video type.
 *
 * @param videoType - The video type, either from options or guessed from the URL.
 * @param url - The media URL, used to check if it's a transcoded stream.
 * @returns An array of video plugins.
 */
export function videoPlugins(videoType: string | null | undefined, url: IUrl | undefined): VideoPlugin[] {
  // server transcodes always expose an HLS stream
  if (isTranscodedStream(url)) {
    return HLS.isSupported() ? [HLS] : [];
  }

  videoType = videoType?.toLowerCase();
  if (videoType === 'mp4') {
    // https://h5player.bytedance.com/plugins/extension/xgplayer-mp4.html
    // native iOS playback is more reliable than the MP4 plugin
    const ios = sniffer.isIos();
    if (!ios) {
      return [MP4];
    }
  } else if (videoType === 'flv') {
    // https://h5player.bytedance.com/plugins/extension/xgplayer-flv.html
    const supported = FLV.isSupported();
    if (supported) {
      return [FLV];
    }
  } else if (videoType === 'hls') {
    // https://h5player.bytedance.com/plugins/extension/xgplayer-hls.html
    // prefer native HLS when the browser exposes it
    const native = document.createElement('video').canPlayType('application/vnd.apple.mpegurl');
    if (!native && HLS.isSupported()) {
      return [HLS];
    }
  } else if (videoType === 'dash') {
    // https://h5player.bytedance.com/plugins/extension/third-party-plugin.html#xgplayer-shaka
    const supported = Shaka.isSupported();
    if (supported) {
      return [Shaka];
    }
  }
  return [];
}
