import { icons, iconToSVG } from '$lib/icons';
import { isTranscodedStream, sniffer } from '$lib/utils';
import { I18N, type BasePlugin, type IPlayerOptions } from 'xgplayer';
import type { IUrl } from 'xgplayer/es/defaultConfig';

import FLV from 'xgplayer-flv';
import HLS from 'xgplayer-hls';
import MP4 from 'xgplayer-mp4';
import Thumbnail from 'xgplayer/es/plugins/common/thumbnail';
import Danmu from 'xgplayer/es/plugins/danmu';
import Enter from 'xgplayer/es/plugins/enter';
import Error from 'xgplayer/es/plugins/error';
import Fullscreen from 'xgplayer/es/plugins/fullscreen';
import Keyboard from 'xgplayer/es/plugins/keyboard';
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
import Gradient from './gradient';
import PlaybackRate from './playbackrate';
import TopBar from './topbar';

// import all i18n json files from locales directory
const locales = import.meta.glob('$lib/locales/*.json', {
  eager: true,
  import: 'default'
});

// register i18n languages
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
  Danmu,
  TopBar,
  Gradient,
  Chapters,
  PlaybackRate
];

/**
 * The custom SVG icons for the player control bar.
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
  loadingIcon: iconToSVG(icons.loading, 'text-white')
};

/**
 * Default preset plugins for the video player.
 */
export default class DefaultPreset {
  plugins: Partial<BasePlugin>[];

  constructor(_: unknown, options: IPlayerOptions) {
    // set base plugins
    this.plugins = [...BASE_PLUGINS];
    if (!options.isLive) {
      this.plugins.push(Time, TimeSegments, Progress, MiniProgress, ProgressPreview);
    }
    if (sniffer.isMobile()) {
      this.plugins.push(Mobile);
    } else {
      this.plugins.push(PC, Keyboard);
    }
    if (sniffer.isIpad()) {
      this.plugins.push(PC);
    }
    // set video plugins
    this.plugins.push(...videoPlugins(options.videoType || guessVideoType(options.url), options.url));
    options.mp4Plugin = {
      preferMMS: true
    };
    options.flv = {
      preferMMS: true
    };
    options.hls = {
      preferMMS: true
    };
    // set icons
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
function videoPlugins(videoType: string | null | undefined, url: IUrl | undefined): Partial<BasePlugin>[] {
  // if it's a transcoded stream, use HLS plugin if supported since transcoded streams are delivered via HLS
  if (isTranscodedStream(url)) {
    return HLS.isSupported() ? [HLS] : [];
  }

  videoType = videoType?.toLowerCase();
  if (videoType === 'mp4') {
    // https://h5player.bytedance.com/plugins/extension/xgplayer-mp4.html
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
    const native = document.createElement('video').canPlayType('application/vnd.apple.mpegurl');
    if (!native && HLS.isSupported()) {
      return [HLS];
    }
  }
  return [];
}
