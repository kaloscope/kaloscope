<script lang="ts" module>
  import { api } from '$lib/api';
  import { MEDIA_STREAM_PREFIX } from '$lib/constants';
  import type { Chapter, Danmaku, Definition, MediaItem, Optional, Page, Resp } from '$lib/types';
  import { extractStreamPath } from '$lib/utils';
  import type OptionsPlugin from 'xgplayer/es/plugins/common/optionsIcon';
  import type FullscreenPlugin from 'xgplayer/es/plugins/fullscreen';
  import type MobilePlugin from 'xgplayer/es/plugins/mobile';

  /**
   * The type of the video player options.
   */
  export type VideoPlayerOptions = {
    /** The width of the container. */
    width?: string;
    /** The height of the container. */
    height?: string;
  };

  /**
   * The type of the video options.
   */
  export type VideoOptions = {
    url: string;
  } & Optional<{
    width: string | number;
    height: string | number;
    autoplay: boolean;
    startTime: number;
    /** The type of the video source, e.g., 'mp4', 'flv', 'hls', etc. */
    videoType: string;
    /** The danmakus (video comments) to be displayed on the video. */
    danmakus: Danmaku[];
    /** The video chapters for TV shows or multi-part videos. */
    chapters: Chapter[];
    chapterId: string;
    chapterChange: (chapter: Chapter) => void;
    /** The video definitions for different quality levels. */
    definitions: Definition[];
    /** The callback function when the back button is clicked. */
    back: () => void;
    /** Whether the video is the next chapter of the current video. */
    next: boolean;
    /** The title to be displayed on the top bar. */
    title: string;
    /** The uploader to be displayed on the top bar. */
    uploader: string;
    uploadedAt: string;
  }>;

  /**
   * Format the media title with season and episode if available.
   *
   * @param item - The media item.
   */
  export function mediaTitle(item: MediaItem | null | undefined): string {
    if (!item) {
      return '';
    }
    const title = item.title ?? item.name;
    if (item.season !== null && item.episode !== null) {
      return `S${item.season}E${item.episode} - ${title}`;
    }
    return title;
  }

  /**
   * Resolves app-level video URL values into xgplayer URL values.
   *
   * @param url - The app-level video URL.
   * @param videoType - The source video type.
   * @returns A URL value xgplayer can consume.
   */
  function resolvePlaybackUrl(url: string, videoType?: string | null): string {
    // dash sources are passed as inline MPD XML strings; normal URLs should stay untouched
    if (videoType?.toLowerCase() === 'dash' && /^\s*(?:<\?xml[\s\S]*?)?<MPD[\s>]/i.test(url)) {
      const origin = globalThis.location?.origin ?? '';
      if (origin) {
        // make app proxy paths absolute
        url = url.replace(/(<BaseURL>)\/_api\//g, `$1${origin}/_api/`);
      }
      // encode as UTF-8 first so btoa can safely handle non-Latin XML content
      const bytes = new TextEncoder().encode(url);
      let binary = '';
      for (const byte of bytes) {
        binary += String.fromCharCode(byte);
      }
      return `data:application/dash+xml;base64,${btoa(binary)}`;
    }
    return url;
  }

  /**
   * Records the user's watch history when the player is destroyed.
   *
   * @param player - The player instance.
   */
  function recordHistory(player: Player | null) {
    if (!player) {
      return;
    }
    const url = player.config.url;
    if (typeof url === 'string' && url.startsWith(MEDIA_STREAM_PREFIX)) {
      const path = extractStreamPath(url);
      let position = player.currentTime;
      let duration = player.duration;
      if (isNaN(position) || isNaN(duration) || position < 0 || duration <= 0) {
        return;
      }
      position = Math.floor(position);
      duration = Math.ceil(duration);
      if (position > duration) {
        position = duration;
      }
      const percentage = Math.floor((position / duration) * 100);
      api
        .get('media/list', { searchParams: { page_num: 0, path } })
        .json<Resp<Page<MediaItem>>>()
        .then(({ data }) => {
          for (const item of data.items) {
            api.post('user/history/record', {
              json: {
                rel_type: 'video',
                rel_id: item.id,
                position: position,
                percentage: percentage
              }
            });
          }
        });
    }
  }
</script>

<script lang="ts">
  import { freeze } from '$lib/stores';
  import { sniffer } from '$lib/utils';
  import { onMount } from 'svelte';
  import { v4 as uuidv4 } from 'uuid';
  import Player, { Events, SimplePlayer } from 'xgplayer';
  import DefaultPreset from './plugins/preset';
  import VideoSettings, { formatDanmakus } from './VideoSettings.svelte';

  const { width = '100%', height = '100%' }: VideoPlayerOptions = $props();
  // player ID
  const id: string = `player-${uuidv4()}`;
  // video container
  let container: HTMLDivElement;
  // danmaku container
  let danmakuContainer: HTMLDivElement;

  // the player instance
  let player: Player | null = $state(null);
  // the video settings instance
  let videoSettings: VideoSettings;

  // the plugins used in the player
  let mobilePlugin: MobilePlugin | null = $derived.by(() => player?.getPlugin('mobile'));
  let chaptersPlugin: OptionsPlugin | null = $derived.by(() => player?.getPlugin('chapters'));
  let definitionsPlugin: OptionsPlugin | null = $derived.by(() => player?.getPlugin('definitions'));
  let fullscreenPlugin: FullscreenPlugin | null = $derived.by(() => player?.getPlugin('fullscreen'));
  let playbackRatePlugin: OptionsPlugin | null = $derived.by(() => player?.getPlugin('playbackRate'));
  let texttrackPlugin: OptionsPlugin | null = $derived.by(() => player?.getPlugin('texttrack'));

  // the theme color before entering fullscreen mode
  let themeColor: string | null = null;
  // whether the player is in fullscreen mode
  let fullscreen: boolean = $state(false);
  // whether the screen orientation is locked in fullscreen mode
  let screenLocked: boolean = $state(false);
  // whether the player is in rotate fullscreen mode
  let rotateFullscreen: boolean = $state(false);
  // track the last URL that was auto-retried with transcode to prevent infinite loops
  let transcodeRetriedUrl: string | null = null;

  /**
   * Toggles the fullscreen state of the player.
   *
   * https://h5player.bytedance.com/plugins/internalplugins/fullscreen.html#switchcallback
   */
  const toggleFullscreen = () => {
    if (!player || !fullscreenPlugin) {
      return;
    }

    // update the fullscreen state
    fullscreen = !(player.isRotateFullscreen || player.cssfullscreen || player.fullscreen);

    if (player.isRotateFullscreen) {
      // exit rotate fullscreen mode
      player.exitRotateFullscreen();
      rotateFullscreen = player.isRotateFullscreen;
      fullscreenPlugin.animate(rotateFullscreen);
      videoSettings.toggleDirection();
    } else if (player.cssfullscreen) {
      // exit css fullscreen mode
      player.exitCssFullscreen();
      fullscreenPlugin.animate(player.cssfullscreen);
    } else if (player.fullscreen) {
      // exit native fullscreen mode
      player.exitFullscreen(container);
      if (screenLocked) {
        // unlock the screen orientation if it was locked
        fullscreenPlugin.unlockScreen();
        screenLocked = false;
      }
    } else {
      // enter fullscreen mode based on the device type and aspect ratio
      if (sniffer.isMobile() && player.aspectRatio > 1) {
        if (videoSettings.landscapeMode() === 'web_api' && !sniffer.isIos()) {
          // native fullscreen mode
          player.getFullscreen(container).catch(() => {});
          // try to lock the screen orientation
          fullscreenPlugin.lockScreen('landscape-primary');
          screenLocked = true;
        } else {
          // rotate fullscreen mode
          player.getRotateFullscreen();
          rotateFullscreen = player.isRotateFullscreen;
          fullscreenPlugin.animate(rotateFullscreen);
          videoSettings.toggleDirection();
        }
      } else if (sniffer.isIos()) {
        // css fullscreen mode
        player.getCssFullscreen(container);
        fullscreenPlugin.animate(player.cssfullscreen);
      } else {
        // native fullscreen mode
        player.getFullscreen(container).catch(() => {});
      }
    }
  };

  /**
   * Extracts the definitions from the video options.
   *
   * @param options - The video options.
   * @returns The list of definitions.
   */
  const extractDefinitions = (options: VideoOptions): Definition[] => {
    if (options.definitions && options.definitions.length > 0) {
      return options.definitions
        .filter((d) => d.url && d.definition)
        .map((d) => ({ ...d, url: resolvePlaybackUrl(d.url, options.videoType) }))
        .filter((d) => d.url);
    }
    return [];
  };

  /**
   * Extracts the chapters from the video options.
   *
   * @param options - The video options.
   * @returns The list of chapters.
   */
  const extractChapters = (options: VideoOptions): Chapter[] => {
    if (options.chapters && options.chapters.length > 0) {
      return options.chapters
        .filter((c) => (c.id || c.url) && c.title)
        .map((c) => ({
          id: c.id,
          url: c.url,
          title: c.title
        }));
    }
    return [];
  };

  /**
   * The playback rates available for the player.
   *
   * https://h5player.bytedance.com/plugins/internalplugins/playbackrate.html#list
   */
  const playbackRates = [
    { text: '2.0x', rate: 2.0 },
    { text: '1.5x', rate: 1.5 },
    { text: '1.25x', rate: 1.25 },
    { text: '1.0x', rate: 1.0, iconText: { en: '1.0x', zh: '倍速' } },
    { text: '0.75x', rate: 0.75 },
    { text: '0.5x', rate: 0.5 }
  ];

  /**
   * Mounts the player with the given options.
   *
   * @param options - The video options.
   */
  export async function mount(options: VideoOptions) {
    if (!options || !options.url) {
      return;
    }

    let url = resolvePlaybackUrl(options.url, options.videoType);

    // reset the transcode auto-retry flag so a new video gets its own retry
    transcodeRetriedUrl = null;

    // probe the media to get the duration and progress dots
    const { duration, progressDot } = await videoSettings.probeMedia(url);

    // if the player is already mounted, just switch the URL
    if (player) {
      player.getPlugin('progresspreview')?.updateAllDots(progressDot);
      if (options.next) {
        player.playNext({ url, topBar: { title: options.title }, customDuration: duration });
      } else {
        videoSettings.changePlaybackSource(url);
      }
      return;
    }

    // create a new player instance
    SimplePlayer.defaultPreset = DefaultPreset;
    player = new SimplePlayer({
      id: id,
      url: url,
      width: options.width ?? width,
      height: options.height ?? height,
      autoplay: options.autoplay ?? true,
      startTime: options.startTime ?? undefined,
      videoType: options.videoType,
      progressDot: progressDot,
      customDuration: duration,
      // bind the video settings component to the player config
      settings: videoSettings,
      topBarAutoHide: false,
      topBar: {
        back: options.back,
        title: options.title,
        uploader: options.uploader,
        uploadedAt: options.uploadedAt
      },
      danmu: {
        comments: formatDanmakus(options.danmakus, danmakuContainer),
        // the danmaku plugin will start automatically when the `TIME_UPDATE` event is emitted
        // https://github.com/bytedance/xgplayer/blob/main/packages/xgplayer/src/plugins/danmu/index.js#L91
        defaultOff: true,
        closeDefaultBtn: true,
        ext: {
          container: danmakuContainer
        }
      },
      volume: {
        index: 98,
        default: 1,
        showValueLabel: true
      },
      definitions: {
        index: 99,
        list: extractDefinitions(options)
      },
      chapters: {
        index: 100,
        list: extractChapters(options),
        chapterId: options.chapterId,
        chapterChange: options.chapterChange
      },
      texttrack: {
        index: 101,
        list: [],
        isDefaultOpen: false,
        style: {
          follow: true,
          mode: 'stroke',
          fitVideo: true,
          line: 'double'
        }
      },
      playbackRate: {
        index: 102,
        list: playbackRates
      },
      mobile: {
        gradient: 'none',
        gestureX: false,
        pressRate: 2,
        disablePress: true
      },
      controls: {
        mode: 'normal',
        initShow: true
      },
      fullscreen: {
        switchCallback: toggleFullscreen
      },
      keyboard: {
        seekStep: 5,
        playbackRate: 2,
        keyCodeMap: {
          left: {
            pressAction: 'seekBack'
          },
          right: {
            pressAction: 'changePlaybackRate'
          }
        }
      },
      miniprogress: true,
      pip: true
    });
    // customize the player
    listenEvents(player);
    usePluginHooks(player);
    // initialize the settings
    videoSettings.init();
  }

  /**
   * Listens to player events and executes the corresponding actions.
   *
   * @param player - The player instance.
   */
  function listenEvents(player: Player) {
    player.once(Events.PLAYING, () => {
      // enable the gestures on mobile devices
      if (mobilePlugin) {
        mobilePlugin.config.gestureX = true;
        mobilePlugin.config.disablePress = false;
      }
      // enable the auto-hide for the top bar
      player.root?.querySelector('.xg-top-bar')?.classList.add('top-bar-autohide');
    });

    player.on(Events.PLAYNEXT, () => {
      // update the danmakus when the next video is played
      videoSettings.loadLocalDanmakus();
      // update the subtitles when the next local video is played
      videoSettings.loadLocalSubtitles();
    });

    [Events.FULLSCREEN_CHANGE, Events.CSS_FULLSCREEN_CHANGE].forEach((event) => {
      player.on(event, (fullscreen) => {
        // remove the width style to fix the resizing issue
        if (fullscreen && player.root) {
          player.root.style.width = '';
        }
        // update the theme color meta tag
        const metaTag = document.querySelector('meta[name="theme-color"]');
        if (metaTag) {
          if (fullscreen && themeColor === null) {
            themeColor = metaTag.getAttribute('content');
            metaTag.setAttribute('content', '#000');
          } else if (!fullscreen && themeColor) {
            metaTag.setAttribute('content', themeColor);
            themeColor = null;
          }
        }
      });
    });

    if (mobilePlugin) {
      // disable the long press gesture when the player is paused
      player.on(Events.PAUSE, () => {
        mobilePlugin.config.disablePress = true;
      });

      // re-enable the long press gesture when the player starts playing
      player.on(Events.PLAY, () => {
        if (!mobilePlugin.config.gestureX) {
          // only re-enable the long press gesture after the first play
          return;
        }
        mobilePlugin.config.disablePress = false;
      });

      // fix the issue that the long press event is interrupted by the touchmove event
      player.on(Events.USER_ACTION, (data) => {
        if (data.pluginName === 'mobile' && data.eventType === 'press') {
          mobilePlugin.config.disablePress = true;
          mobilePlugin.config.disableGesture = true;
          player.config.enableSwipeHandler = () => {
            if (!player.paused) {
              mobilePlugin.config.disablePress = false;
              mobilePlugin.config.disableGesture = false;
              mobilePlugin.onPressEnd({});
            }
          };
        }
      });

      // handle the error event
      player.on(Events.ERROR, async (error) => {
        // only handle errors related to transcoding
        const errorCode = error.errorCode;
        if (![5103, 5104, 5105].includes(errorCode)) {
          // 5103 - decoding error
          // 5104 - format of media resource is not supported by platform
          // 5105 - current browser can't decode video
          return;
        }
        const url = player.config.url;
        // only handle direct stream URLs that are not already transcoded
        if (typeof url !== 'string' || !url.startsWith(MEDIA_STREAM_PREFIX) || url.includes('transcode=true')) {
          return;
        }
        // prevent infinite retry loop for the same URL
        if (transcodeRetriedUrl === url) {
          return;
        }
        transcodeRetriedUrl = url;
        try {
          const resp = await api.get('config/transcode.auto').json<Resp<boolean>>();
          if (resp.data) {
            videoSettings.changePlaybackMode('transcode');
          }
        } catch {
          // ignore the error and continue playing
        }
      });
    }
  }

  /**
   * Uses the plugin hooks to customize the player behavior.
   *
   * @param player - The player instance.
   */
  function usePluginHooks(player: Player) {
    player.usePluginHooks('mobile', 'videoClick', () => {
      if (player.isPlaying) {
        return true;
      }
      if (hideOptionPlugins()) {
        return false;
      }
      return true;
    });
  }

  /**
   * Hides active option menus.
   */
  function hideOptionPlugins() {
    for (const plugin of [definitionsPlugin, chaptersPlugin, playbackRatePlugin, texttrackPlugin]) {
      if (plugin && plugin.optionsList && plugin.isActive) {
        plugin.optionsList.hide();
        plugin.isActive = false;
        return true;
      }
    }
    return false;
  }

  /**
   * Handles the orientation change event.
   */
  function onorientationchange() {
    if (player?.isRotateFullscreen) {
      videoSettings.toggleDirection();
    }
  }

  onMount(() => {
    // add the event listener for orientation change on mobile devices
    const isMobile = sniffer.isMobile();
    if (isMobile) {
      window.addEventListener('orientationchange', onorientationchange);
    }
    // freeze the background to prevent scrolling when the player is active
    freeze.set(true);
    return () => {
      freeze.set(false);
      // destroy the player instance
      recordHistory(player);
      player?.destroy();
      // remove the event listener
      if (isMobile) {
        window.removeEventListener('orientationchange', onorientationchange);
      }
      // recover the theme color meta tag
      if (themeColor) {
        const metaTag = document.querySelector('meta[name="theme-color"]');
        if (metaTag) {
          metaTag.setAttribute('content', themeColor);
        }
        themeColor = null;
      }
    };
  });
</script>

<!-- The video player container. -->
<div bind:this={container} class="relative" style:width style:height>
  <div {id}></div>

  <!-- The danmaku container. -->
  <div
    bind:this={danmakuContainer}
    class="pointer-events-none absolute inset-0 layer-0 overflow-visible! text-stroke"
    class:h-dvh={rotateFullscreen}
    class:right-8={rotateFullscreen}
    class:top-10={!fullscreen}
  ></div>
</div>

<!-- The video settings component. -->
<VideoSettings bind:this={videoSettings} {player} />

<style>
  :global {
    .xgplayer {
      font-family: unset !important;

      &.xgplayer-mobile {
        @media only screen and (orientation: portrait) {
          .xg-side-list.xg-right-side {
            width: 30%;
            right: -15%;
          }
          &.xgplayer-rotate-fullscreen {
            width: 100dvh;
            height: 100dvw;
            .xg-side-list.xg-right-side {
              width: 20%;
              right: -10%;
            }
          }
        }
        @media only screen and (orientation: landscape) {
          .xg-side-list.xg-right-side {
            width: 20%;
            right: -10%;
          }
          &.xgplayer-rotate-fullscreen {
            width: 100dvw;
            height: 100dvh;
          }
        }
      }

      &.xgplayer-is-fullscreen,
      &.xgplayer-is-cssfullscreen,
      &.xgplayer-rotate-fullscreen {
        .xg-mini-progress {
          display: none;
        }
      }

      .xgplayer-loading {
        width: 100px;
        height: 100px;

        xg-loading-inner {
          animation: none;
          > xg-enter.xgplayer-enter.xgplayer-loading-enter {
            display: block;
            position: relative;
            z-index: auto;
            left: auto;
            top: auto;
            width: 100%;
            height: 100%;
            background: transparent;
          }
        }
      }

      &.xgplayer-mobile .xgplayer-loading {
        width: 70px;
        height: 70px;
      }

      &.xgplayer-isloading,
      &.xgplayer-playing {
        .xgplayer-start {
          display: none;
        }
      }

      &.xgplayer-switch-loading {
        .xgplayer-start {
          display: none;
        }
        .xgplayer-loading {
          display: block;
        }
        > xg-enter.xgplayer-enter:not(.xgplayer-loading-enter) .xgplayer-enter-spinner {
          display: none;
        }
      }

      &.xgplayer-nostart {
        .xg-top-bar {
          display: flex !important;
        }
      }

      &.xgplayer-inactive:not(.xgplayer-nostart) {
        .top-bar-autohide {
          display: flex !important;
          opacity: 0;
          visibility: hidden;
          pointer-events: none;
        }
      }

      .xg-top-bar {
        padding: 0 !important;
        height: 52px !important;
        left: 10px;
        right: 10px;
        cursor: default;
        pointer-events: auto;
        opacity: 1;
        visibility: visible;
        transition:
          opacity 0.5s ease,
          visibility 0.5s ease;
      }

      .xg-top-note {
        top: 56px !important;
        height: 28px !important;
        min-width: 96px !important;
        width: auto !important;
        margin-left: 0 !important;
        padding: 0 12px !important;
        transform: translateX(-50%);
        box-sizing: border-box;
        white-space: nowrap;
        background-color: hsla(0, 0%, 30%, 0.3) !important;
        span {
          height: 28px !important;
          line-height: 28px !important;
        }
      }

      .btn-text span {
        border-radius: 8px !important;
      }

      .xgplayer-controls {
        --xg-control-icon-gap: 8px;
        @media (min-width: 40rem) {
          --xg-control-icon-gap: 16px;
        }
        /* https://github.com/bytedance/xgplayer/issues/1460 */
        .xg-tips {
          display: none !important;
        }
        .xg-right-grid {
          > :first-child {
            margin-right: 0;
          }
          xg-icon {
            margin-left: 0;
            margin-right: var(--xg-control-icon-gap);
          }
        }
        .xg-left-grid {
          > :first-child {
            margin-left: 0;
          }
          xg-icon {
            margin-left: var(--xg-control-icon-gap);
            margin-right: 0;
          }
        }
        .xg-center-grid xg-icon {
          margin-left: var(--xg-control-icon-gap);
          margin-right: var(--xg-control-icon-gap);
        }
        cursor: default;
        background-image: initial !important;
      }

      .xgplayer-start {
        width: 70px !important;
        height: 70px !important;
        xg-start-inner {
          display: flex;
          align-items: center;
          justify-content: center;
          border-radius: 50% !important;
          background-color: hsla(0, 0%, 30%, 0.3) !important;
        }
      }

      .xgplayer-replay {
        xg-replay-txt {
          font-size: 18px !important;
          font-weight: 800;
          text-shadow: 0px 1px 1px rgba(0, 0, 0, 0.2);
        }
      }

      .xgplayer-volume {
        .xgplayer-value-label {
          border-top-left-radius: 2px;
          border-top-right-radius: 2px;
          background-color: hsla(0, 0%, 10%, 0.9);
        }
        .xgplayer-slider {
          border-bottom-left-radius: 2px;
          border-bottom-right-radius: 2px;
          background-color: hsla(0, 0%, 10%, 0.9);
        }
      }

      .xg-side-list {
        &.xg-options-list {
          opacity: 1;
          z-index: 11;
          font-size: 20px;
          background-color: hsla(0, 0%, 10%, 0.9) !important;
        }
      }
    }
  }
</style>
