<script lang="ts" module>
  import type { Danmaku, Definition, Resp, Subtitle } from '$lib/types';
  import { isWhite } from '$lib/utils';
  import type { IconifyIcon } from 'iconify-icon';
  import { v4 as uuidv4 } from 'uuid';
  import type Player from 'xgplayer';
  import type { IUrl } from 'xgplayer/es/defaultConfig';
  import type DanmakuPlugin from 'xgplayer/es/plugins/danmu';
  import type PlaybackRatePlugin from 'xgplayer/es/plugins/playbackRate';
  import type RotatePlugin from 'xgplayer/es/plugins/rotate';
  import type StartPlugin from 'xgplayer/es/plugins/start';
  import type TextTrackPlugin from './plugins/texttrack';

  // video settings and types
  type LandscapeMode = 'rotate' | 'web_api';
  type PlaybackMode = 'direct' | 'transcode';
  type VideoSettings = {
    landscapeMode: LandscapeMode;
    playbackMode: PlaybackMode;
    pressRate?: number;
  };

  const DEFAULT_PRESS_RATE = 2;
  const PRESS_RATE_OPTIONS = Array.from({ length: 14 }, (_, index) => {
    const rate = 1.5 + index * 0.5;
    return { value: rate, label: `${rate.toFixed(1)}x` };
  });

  const video = persisted<VideoSettings>('video', {
    landscapeMode: 'rotate',
    playbackMode: 'direct',
    pressRate: DEFAULT_PRESS_RATE
  });

  // subtitle settings and types
  type SubtitleDisplayMode = 'stroke' | 'bg';
  type SubtitleSettings = {
    autoShow: boolean;
    languagePreference: string;
    displayMode: SubtitleDisplayMode;
    timeOffset: number;
    fontScale: number;
    verticalPosition: number;
  };

  const subtitle = persisted<SubtitleSettings>('subtitle', {
    autoShow: false,
    languagePreference: '',
    displayMode: 'stroke',
    timeOffset: 0,
    fontScale: 100,
    verticalPosition: 2
  });

  type TrackWrapper = {
    id?: string | number | null;
    _language?: string | number | null;
    label?: string | null;
    url?: string | null;
    isDefault?: boolean;
  };

  // danmaku settings and types
  type DanmakuMode = 'scroll' | 'top' | 'bottom' | 'color';
  type DanmakuSettings = {
    enabled: boolean;
    blocks: DanmakuMode[];
    area: number;
    opacity: number;
    fontSize: number;
    speed: number;
  };

  const danmaku = persisted<DanmakuSettings>('danmaku', {
    enabled: true,
    blocks: [],
    area: 75,
    opacity: 50,
    fontSize: 20,
    speed: 50
  });

  type DanmakuMeta = {
    anime_id: string;
    anime_title: string | null;
    episode_id: string;
    episode_title: string | null;
    type: string;
    type_description: string | null;
  };

  type DanmakuWrapper = {
    metadata: DanmakuMeta | null;
    comments: Danmaku[];
  };

  /**
   * Find the first subtitle track matching the language preference.
   *
   * @param tracks - The formatted subtitle tracks.
   * @param languagePreference - The regular expression used to match tracks.
   * @returns The preferred subtitle track, or the first track when no match is found.
   */
  export function findPreferredTrack<T extends TrackWrapper>(tracks: T[], languagePreference: string) {
    if (tracks.length === 0) {
      return null;
    }
    const pattern = languagePreference.trim();
    if (!pattern) {
      return tracks[0];
    }
    try {
      const regex = new RegExp(pattern, 'i');
      return (
        tracks.find((track) => {
          const t = [track.label, track._language].filter(Boolean).join('\n');
          return regex.test(t);
        }) ?? tracks[0]
      );
    } catch {
      return tracks[0];
    }
  }

  /**
   * Formats subtitle track metadata for the TextTrack plugin.
   *
   * @param subtitles - The list of subtitle tracks.
   * @param settings - The persisted subtitle settings.
   * @returns The formatted subtitles.
   */
  export function formatSubtitles(subtitles: Subtitle[] | null | undefined, settings?: SubtitleSettings | null) {
    if (!subtitles || subtitles.length === 0) {
      return [];
    }

    // format subtitle tracks
    const tracks = subtitles
      .filter((subtitle) => !!subtitle.url)
      .map((subtitle) => {
        return {
          id: subtitle.id,
          // Keep backend language in `_language` for app-side preference matching, but do not pass it to xgplayer:
          // xgplayer treats tracks with the same language as the same subtitle and may switch to the wrong track
          // when multiple subtitles share a language marker.
          _language: subtitle.language,
          // language: subtitle.language,
          label: subtitle.label,
          url: subtitle.url as string,
          isDefault: false
        };
      });

    // find the preferred track
    const preferredTrack = findPreferredTrack(tracks, settings?.autoShow ? settings.languagePreference : '');
    return tracks.map((track) => ({
      ...track,
      isDefault: track.id === preferredTrack?.id
    }));
  }

  /**
   * Formats the danmakus to the format required by the player.
   *
   * @param danmakus - The list of video comments.
   * @param container - The danmaku container element.
   * @param direction - The danmaku direction.
   * @returns The formatted danmakus.
   */
  export function formatDanmakus(
    danmakus: Danmaku[] | null | undefined,
    container?: HTMLDivElement,
    direction?: 'r2l' | 'b2t'
  ) {
    if (!danmakus || danmakus.length === 0) {
      return [];
    }

    // calculate dynamic duration based on container size
    let defaultDuration = 5000;
    if (container) {
      const size = direction === 'b2t' ? container.offsetHeight : container.offsetWidth;
      const duration = Math.round((size / 200) * 1000);
      // clamp between 5s and 10s
      defaultDuration = Math.max(5000, Math.min(10000, duration));
    }

    // https://github.com/bytedance/danmu.js
    return danmakus
      .filter((danmaku) => danmaku.text)
      .map(({ id, text, start, duration, mode, color }) => {
        return {
          id: id || uuidv4(), // unique id
          txt: text, // comment text
          start: start || 0, // start time in milliseconds
          duration: duration || defaultDuration, // duration in milliseconds
          mode: mode || 'scroll', // display mode: 'scroll', 'top', 'bottom'
          color: !!color && !isWhite(color), // mark the danmaku as colored
          style: {
            color: color || '#fff' // comment color
          }
        };
      });
  }

  /**
   * Probes the duration of a transcoded video stream.
   *
   * @param url - The URL of the transcoded video stream.
   * @returns The duration in seconds, or undefined if probing failed.
   */
  export async function probeDuration(url: IUrl): Promise<number | undefined> {
    let duration: number | undefined;
    if (isTranscodedStream(url)) {
      try {
        const path = extractStreamPath(url as string);
        const resp = await api.get('media/probe', { searchParams: { path } }).json<Resp<{ duration: number }>>();
        if (resp.data.duration > 0) {
          duration = resp.data.duration;
        }
      } catch {
        // probe failed, just ignore the error and let the player handle it
      }
    }
    return duration;
  }
</script>

<script lang="ts">
  import { tooltip } from '$lib/actions';
  import { api } from '$lib/api';
  import { Button, confirm, Modal, Overlay, Range, Select } from '$lib/components';
  import { EMPTY_SIGN, MEDIA_STREAM_PREFIX } from '$lib/constants';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { persisted } from '$lib/stores';
  import { extractStreamPath, isTranscodedStream, sniffer } from '$lib/utils';
  import { tick } from 'svelte';
  import { fade } from 'svelte/transition';
  import { Events } from 'xgplayer';
  import HLS from 'xgplayer-hls';

  let { player }: { player: Player | null } = $props();
  // whether the current video is a local media file
  let localMedia: boolean = $derived.by(() => {
    const url = player?.config.url;
    return typeof url === 'string' && url.startsWith(MEDIA_STREAM_PREFIX);
  });

  // the plugins used in the settings
  let startPlugin: StartPlugin | null = $derived.by(() => player?.getPlugin('start'));
  let rotatePlugin: RotatePlugin | null = $derived.by(() => player?.getPlugin('rotate'));
  let danmakuPlugin: DanmakuPlugin | null = $derived.by(() => player?.getPlugin('danmu'));
  let textTrackPlugin: TextTrackPlugin | null = $derived.by(() => player?.getPlugin('texttrack'));
  let playbackRatePlugin: PlaybackRatePlugin | null = $derived.by(() => player?.getPlugin('playbackRate'));

  // the settings states
  let playbackRates: Record<string, number> = $state({});
  let playbackRate: number = $state(1);
  let rotateDegrees: number[] = $state([0, 90, 180, 270]);
  let rotateDegree: number = $state(0);
  let definitions: Definition[] = $state([]);
  let definition: string = $state('');

  // the modal dialog instance
  let modal: Modal;
  // the current active tab in the settings modal
  let tabId: string = $state('video');
  // whether the player is in rotate fullscreen mode
  let rotateFullscreen: boolean = $state(false);

  // the danmaku metadata matched with the current video
  let danmakuMeta: DanmakuMeta | null = $state(null);
  // whether local danmaku cache exists for the current video
  let danmakuCache: boolean = $state(false);

  // the manual danmaku search states
  let animeTitle: string = $derived.by(() => danmakuMeta?.anime_title ?? '');
  let results: DanmakuMeta[] = $state([]);
  let index: number = $state(-1);
  const searching = createLoading();
  const confirming = createLoading();

  /**
   * Show the settings modal.
   */
  export const showModal = () => modal.show();

  /**
   * Get the current landscape mode setting.
   */
  export const landscapeMode = () => $video?.landscapeMode;

  /**
   * Toggle the rotation direction of the modal.
   */
  export function toggleDirection() {
    const portrait = screen.orientation.type.includes('portrait');
    if (player) {
      rotateFullscreen = portrait && player.isRotateFullscreen;
    }
    // toggle the scroll direction of the danmakus
    tick().then(() => {
      const danmujs = danmakuPlugin?.danmujs;
      if (danmujs) {
        const direction = portrait && rotateFullscreen ? 'b2t' : 'r2l';
        danmujs.stop();
        danmujs.setDirection(direction);
        danmujs.start();
        player?.paused && danmujs.pause();
      }
    });
  }

  /**
   * Initialize the settings.
   */
  export function init() {
    // playback mode
    if (localMedia && $video !== null) {
      const url = player?.config.url;
      $video.playbackMode = isTranscodedStream(url) ? 'transcode' : 'direct';
    }
    // playback rate
    changePressRate();
    if (playbackRatePlugin !== null) {
      for (const rate of [...playbackRatePlugin.config.list].reverse()) {
        playbackRates[rate.text] = rate.rate;
      }
      playbackRate = playbackRatePlugin.curValue;
      playbackRatePlugin.on(Events.RATE_CHANGE, () => {
        if (player?.playbackRate && playbackRate !== player.playbackRate) {
          playbackRate = player.playbackRate;
        }
      });
    }
    // definition
    definitions = ((player?.config.definitions as { list?: Definition[] } | undefined)?.list ?? []).filter(
      (d) => d.url && d.definition
    );
    if (definitions.length > 0 && typeof player?.config.url === 'string') {
      definition = player.config.url;
    }
    // subtitle
    applySubtitleSettings();
    loadLocalSubtitles();
    // danmaku
    if ($danmaku !== null && danmakuPlugin !== null) {
      danmakuPlugin.setArea({ start: 0, end: $danmaku.area / 100 || 0.1 });
      danmakuPlugin.setOpacity($danmaku.opacity / 100);
      danmakuPlugin.setFontSize($danmaku.fontSize, null);
      danmakuPlugin.danmujs?.setPlayRate('scroll', 1 + ($danmaku.speed - 50) / 100);
      for (const mode of $danmaku.blocks) {
        danmakuPlugin.hideMode(mode);
      }
      if (!$danmaku.enabled) {
        danmakuPlugin.stop();
      }
    }
    loadLocalDanmakus();
  }

  /**
   * Change the playback mode of the video.
   *
   * @param mode - The new playback mode.
   */
  export function changePlaybackMode(mode: PlaybackMode) {
    if (!player || !localMedia) {
      return;
    }
    const url = player.config.url as string;
    const isTranscoded = isTranscodedStream(url);
    if ((mode === 'transcode' && isTranscoded) || (mode === 'direct' && !isTranscoded)) {
      return;
    }
    // persist the new mode
    if ($video !== null) {
      $video.playbackMode = mode;
    }
    // switch the URL
    const newUrl = mode === 'transcode' ? url + '&transcode=true' : url.replace('&transcode=true', '');
    changeDefinition(newUrl);
  }

  /**
   * Ensures transcoded HLS playback has the correct media plugin.
   *
   * xgplayer chooses media plugins when the player is created. Local files are
   * initially mounted as `/media/stream`, so switching to `&transcode=true`
   * needs to install HLS before restarting the playback pipeline.
   */
  function prepareTranscodePlayback() {
    if (!player || sniffer.isIos() || player.getPlugin('hls') || !HLS.isSupported()) {
      return;
    }
    for (const pluginName of ['flv', 'mp4Plugin', 'ShakaPlugin']) {
      player.unRegisterPlugin(pluginName, true);
    }
    player.registerPlugin(HLS);
  }

  /**
   * Change the video definition.
   *
   * @param url - The new video URL.
   */
  export async function changeDefinition(url: IUrl) {
    if (!player) {
      return;
    }
    const duration = await probeDuration(url);

    // handle transcoded HLS streams
    if (isTranscodedStream(url) && typeof url === 'string') {
      prepareTranscodePlayback();
      player.playNext({ url, customDuration: duration });
      definition = url;
      return;
    }

    // handle DASH streams
    if (player.config.videoType === 'dash' && typeof url === 'string') {
      // use playNext because switchURL can leave the Shaka pipeline stuck after manifest changes
      player.playNext({ url, customDuration: duration });
      definition = url;
      return;
    }

    // handle other streams
    player.setConfig({ url, customDuration: duration });
    const seamless = typeof MediaSource !== 'undefined' && typeof MediaSource.isTypeSupported === 'function';
    if (seamless) {
      // use seamless switching if supported
      player.switchURL(url, { seamless });
    } else {
      const isPlaying = player.isPlaying;
      startPlugin && (startPlugin.config.disableAnimate = true);
      player.switchURL(url)?.then(() => {
        startPlugin && (startPlugin.config.disableAnimate = false);
        // start the player if it was not playing before
        !isPlaying && player?.play();
      });
    }

    if (typeof url === 'string') {
      if (definition !== url) {
        definition = url;
      } else {
        // definition is changed by the select component
        player.emit('url_change', url);
      }
    }
  }

  /**
   * Change the playback rate of the video.
   *
   * @param rate - The new playback rate.
   */
  function changePlaybackRate(rate: number) {
    if (!player) {
      return;
    }
    player.playbackRate = rate;
    playbackRate = rate;
  }

  /**
   * Change the press rate used by keyboard and mobile press gestures.
   */
  function changePressRate() {
    const rate = $video?.pressRate ?? DEFAULT_PRESS_RATE;
    if ($video !== null) {
      $video.pressRate = rate;
    }
    const keyboardPlugin = player?.getPlugin('keyboard');
    if (keyboardPlugin) {
      keyboardPlugin.config.playbackRate = rate;
    }
    const mobilePlugin = player?.getPlugin('mobile');
    if (mobilePlugin) {
      mobilePlugin.config.pressRate = rate;
    }
    const rateElement = player?.root?.querySelector<HTMLElement>('xg-trigger.trigger .xg-playbackrate i');
    if (rateElement) {
      rateElement.textContent = `${rate}X`;
    }
  }

  /**
   * Change the rotation degree of the video.
   *
   * @param degree - The new rotation degree.
   */
  function changeRotateDegree(degree: number) {
    if (!rotatePlugin) {
      return;
    }
    rotatePlugin.rotate(true, true, (degree - rotateDegree) / 90);
    rotateDegree = degree;
  }

  /**
   * Apply persisted subtitle settings to the text track plugin.
   *
   * @param autoSelect - Whether to select the preferred subtitle track.
   */
  function applySubtitleSettings(autoSelect: boolean = false) {
    if ($subtitle === null || textTrackPlugin === null) {
      return;
    }
    textTrackPlugin.applySubtitleSettings($subtitle);
    if (!autoSelect || !$subtitle.autoShow) {
      return;
    }

    // switch to the preferred subtitle track
    const tracks = (textTrackPlugin.config.list ?? []) as TrackWrapper[];
    const preferredTrack = findPreferredTrack(tracks, $subtitle.languagePreference);
    if (!preferredTrack) {
      return;
    }
    textTrackPlugin.switchSubTitle({ id: preferredTrack.id ?? undefined });
  }

  /**
   * Load the subtitle data for the current local video.
   */
  export function loadLocalSubtitles() {
    if (!localMedia || textTrackPlugin === null) {
      return;
    }
    // clear existing subtitles before loading new tracks
    textTrackPlugin.updateSubtitles([], true);
    const url = player?.config.url as string;
    const path = extractStreamPath(url);
    api
      .post('subtitle/tracks', { json: { path } })
      .json<Resp<Subtitle[]>>()
      .then(({ data }) => {
        updateSubtitles(data);
      });
  }

  /**
   * Update the subtitles for the current video.
   *
   * @param data - The subtitle tracks.
   */
  function updateSubtitles(data: Subtitle[]) {
    if (textTrackPlugin === null) {
      return;
    }
    const subtitles = formatSubtitles(data, $subtitle);
    textTrackPlugin.updateSubtitles(subtitles, true);
    applySubtitleSettings(true);
  }

  /**
   * Load the danmaku data for the current video.
   */
  export function loadLocalDanmakus() {
    if (!localMedia || danmakuPlugin === null) {
      return;
    }
    // clear the existing danmakus before loading new ones
    danmakuPlugin.clear();
    danmakuMeta = null;
    danmakuCache = false;
    // fetch the danmakus matched with the current video
    const url = player?.config.url as string;
    const path = extractStreamPath(url);
    api
      .post('danmaku/match', { json: { path } })
      .json<Resp<DanmakuWrapper>>()
      .then(({ data }) => {
        updateDanmakus(data);
      });
  }

  /**
   * Delete the locally cached danmakus for the current video.
   */
  function deleteLocalDanmakus() {
    if (!localMedia || !danmakuCache) {
      return;
    }
    const url = player?.config.url as string;
    const path = extractStreamPath(url);
    confirm({
      icon: icons.delete,
      title: `${$_('action.delete', $_('media.danmaku.cache'))}`,
      onconfirm: () => {
        api.post('danmaku/delete', { json: { path } }).then(() => {
          danmakuCache = false;
        });
      }
    });
  }

  /**
   * Search for episodes matching the given title.
   */
  function searchEpisodes() {
    if (!localMedia || $searching !== null || !animeTitle.trim()) {
      return;
    }
    searching.start();
    results = [];
    index = -1;
    const url = player?.config.url as string;
    const path = extractStreamPath(url);
    api
      .post('danmaku/search', {
        json: { path, title: animeTitle.trim() }
      })
      .json<Resp<DanmakuMeta[]>>()
      .then(({ data }) => {
        results = data;
      })
      .finally(() => {
        searching.end();
      });
  }

  /**
   * Confirm the selected episode match result.
   */
  function confirmEpisode() {
    if (!localMedia || danmakuPlugin === null || $confirming !== null || index < 0) {
      return;
    }
    const result = results[index];
    if (!result) {
      return;
    }
    confirming.start();
    const url = player?.config.url as string;
    const path = extractStreamPath(url);
    api
      .post('danmaku/confirm', {
        json: { path, metadata: result }
      })
      .json<Resp<DanmakuWrapper>>()
      .then(({ data }) => {
        updateDanmakus(data);
        index = -1;
      })
      .finally(() => {
        confirming.end();
      });
  }

  /**
   * Update the danmakus for the current video.
   *
   * @param data - The danmaku data wrapper.
   */
  function updateDanmakus(data: DanmakuWrapper) {
    if (danmakuPlugin === null) {
      return;
    }
    danmakuMeta = data.metadata;
    const comments = data.comments;
    if (comments && comments.length > 0) {
      danmakuCache = true;

      // update the comments
      danmakuPlugin.updateComments(
        formatDanmakus(comments, danmakuPlugin.danmujs?.container, danmakuPlugin.danmujs?.direction),
        true
      );

      // The font size needs to be reset after updating the comments;
      // this may be a bug in danmu.js where the style is reset after updating.
      if ($danmaku !== null) {
        danmakuPlugin.setFontSize($danmaku.fontSize, null);
      }
    } else {
      danmakuCache = false;
    }
  }
</script>

<Modal
  bind:this={modal}
  class="video-settings {rotateFullscreen ? 'inset-0 left-full h-dvw w-dvh origin-top-left rotate-90' : ''}"
  boxClass="bg-[hsla(0,0%,10%,0.9)] backdrop-blur-sm {rotateFullscreen ? '' : 'auto-margin-bottom'}"
  cornerClass="hidden"
>
  <div>
    <div class="video-settings-header">
      <div class="video-settings-tabs tabs-border tabs">
        {@render tabLabel('video', icons.videoFill, $_('media.video.settings'))}
        {#if localMedia}
          {@render tabLabel('subtitle', icons.subtitlesFilled, $_('media.xgplayer.texttrack'))}
        {/if}
        {@render tabLabel('danmaku', icons.danmakuFill, $_('media.danmaku.settings'))}
        {#if localMedia}
          {@render tabLabel('match', icons.boxMultipleSearchFilled, $_('media.danmaku.match'))}
        {/if}
      </div>

      <button type="button" class="video-settings-close" aria-label="Close" onclick={(event) => modal.close(event)}>
        <iconify-icon icon={icons.dismiss} width="1rem"></iconify-icon>
      </button>
    </div>

    <!-- The video settings tab. -->
    <div class="video-settings-content" hidden={tabId !== 'video'}>
      <div>
        {@render optionLabel($_('media.video.speed'))}
        <span
          class="join grid rounded-field"
          style="grid-template-columns: repeat({Object.keys(playbackRates).length}, minmax(0, 1fr));"
        >
          {#each Object.entries(playbackRates) as [text, rate] (rate)}
            <button
              class="btn join-item {playbackRate === rate ? 'btn-active' : 'hover:brightness-125'}"
              onclick={() => changePlaybackRate(rate)}
            >
              {text}
            </button>
          {/each}
        </span>
      </div>
      <div>
        {@render optionLabel($_('media.video.rotate'))}
        <span
          class="join grid rounded-field"
          style="grid-template-columns: repeat({rotateDegrees.length}, minmax(0, 1fr));"
        >
          {#each rotateDegrees as degree (degree)}
            <button
              class="btn join-item {rotateDegree === degree ? 'btn-active' : 'hover:brightness-125'}"
              onclick={() => changeRotateDegree(degree)}
            >
              {degree}°
            </button>
          {/each}
        </span>
      </div>
      <div>
        {@render optionLabel($_('media.video.definition'))}
        {#if definitions.length > 0}
          <Select
            native={!rotateFullscreen}
            options={definitions.map((d) => ({ value: d.url, label: String(d.definition) }))}
            bind:value={definition}
            onchange={() => changeDefinition(definition)}
            class="dropdown-top [&_p]:max-h-32!"
          />
        {:else}
          <Select translate options={[{ value: '', label: 'media.video.default' }]} disabled />
        {/if}
      </div>
      {#if $video !== null}
        <div>
          {@render optionLabel($_('media.video.press_rate'))}
          <Select
            native={!rotateFullscreen}
            options={PRESS_RATE_OPTIONS}
            bind:value={$video.pressRate}
            onchange={changePressRate}
            class="dropdown-top [&_p]:max-h-32!"
          />
        </div>
      {/if}
      {#if $video !== null}
        <div>
          {@render optionLabel($_('media.video.landscape.title'), $_('media.video.landscape.tip'))}
          <Select
            translate
            native={!rotateFullscreen}
            options={[
              { value: 'rotate', label: 'media.video.landscape.rotate' },
              { value: 'web_api', label: 'media.video.landscape.web_api' }
            ]}
            bind:value={$video.landscapeMode}
            class="dropdown-top [&_p]:max-h-32!"
          />
        </div>
        {#if localMedia}
          <div>
            {@render optionLabel($_('media.video.playback.title'), $_('media.video.playback.tip'))}
            <Select
              translate
              native={!rotateFullscreen}
              options={[
                { value: 'direct', label: 'media.video.playback.direct' },
                { value: 'transcode', label: 'media.video.playback.transcode' }
              ]}
              bind:value={$video.playbackMode}
              onchange={() => changePlaybackMode($video.playbackMode)}
              class="dropdown-top [&_p]:max-h-32!"
            />
          </div>
        {/if}
      {/if}
    </div>

    <!-- The subtitle settings tab. -->
    {#if localMedia}
      <div class="video-settings-content" hidden={tabId !== 'subtitle'}>
        {#if $subtitle !== null}
          <div>
            {@render optionLabel($_('media.subtitle.auto_show'))}
            <input
              type="checkbox"
              class="toggle"
              bind:checked={$subtitle.autoShow}
              onchange={() => applySubtitleSettings(true)}
            />
          </div>
          <div>
            {@render optionLabel(
              $_('media.subtitle.language_preference'),
              $_('media.subtitle.language_preference_tip')
            )}
            <input
              class="input subtitle-setting-field"
              bind:value={$subtitle.languagePreference}
              placeholder={$_('media.subtitle.language_preference_placeholder')}
              disabled={!$subtitle.autoShow}
              onchange={() => applySubtitleSettings(true)}
            />
          </div>
          <div>
            {@render optionLabel($_('media.subtitle.display_mode'))}
            <Select
              translate
              native={!rotateFullscreen}
              options={[
                { value: 'stroke', label: 'media.subtitle.display_mode_options.stroke' },
                { value: 'bg', label: 'media.subtitle.display_mode_options.bg' }
              ]}
              bind:value={$subtitle.displayMode}
              onchange={() => applySubtitleSettings()}
              class="dropdown-top [&_p]:max-h-32!"
            />
          </div>
          <div>
            {@render optionLabel($_('media.subtitle.time_offset'))}
            <label class="input subtitle-setting-field subtitle-time-offset-field">
              <input
                type="number"
                min="-3600"
                max="3600"
                step="0.1"
                bind:value={$subtitle.timeOffset}
                onchange={() => {
                  if ($subtitle !== null) {
                    const value = Number($subtitle.timeOffset);
                    const roundedValue = Math.round(value * 10) / 10;
                    $subtitle.timeOffset = Number.isFinite(value) ? Math.max(-3600, Math.min(3600, roundedValue)) : 0;
                    applySubtitleSettings();
                  }
                }}
              />
              <span class="subtitle-time-offset-unit">s</span>
            </label>
          </div>
          <div>
            {@render optionLabel($_('media.subtitle.font_scale'))}
            <Range
              bind:value={$subtitle.fontScale}
              min={50}
              max={200}
              step={5}
              class="pt-2"
              textClass="text-white/80"
              sliderClass="range-primary"
              onchange={() => applySubtitleSettings()}
            />
          </div>
          <div>
            {@render optionLabel($_('media.subtitle.vertical_position'))}
            <Range
              bind:value={$subtitle.verticalPosition}
              min={0}
              max={15}
              class="pt-2"
              textClass="text-white/80"
              sliderClass="range-primary"
              onchange={() => applySubtitleSettings()}
            />
          </div>
        {/if}
      </div>
    {/if}

    <!-- The danmaku settings tab. -->
    <div class="video-settings-content" hidden={tabId !== 'danmaku'}>
      {#if $danmaku !== null && danmakuPlugin !== null}
        <div>
          {@render optionLabel($_('media.danmaku.toggle'))}
          <input
            type="checkbox"
            class="toggle"
            bind:checked={$danmaku.enabled}
            onchange={() => {
              $danmaku.enabled ? danmakuPlugin.start() : danmakuPlugin.stop();
            }}
          />
        </div>
        <div>
          {@render optionLabel($_('media.danmaku.block'))}
          <div class="flex-center gap-6">
            {@render danmakuBlock('scroll', icons.mist)}
            {@render danmakuBlock('top', icons.alignBoxCenterTop)}
            {@render danmakuBlock('bottom', icons.alignBoxCenterBottom)}
            {@render danmakuBlock('color', icons.palette)}
          </div>
        </div>
        <div>
          {@render optionLabel($_('media.danmaku.area'))}
          <Range
            bind:value={$danmaku.area}
            values={[10, 25, 50, 75, 100]}
            class="pt-2"
            textClass="text-white/80"
            sliderClass="range-primary"
            onchange={(value) => {
              danmakuPlugin.setArea({ start: 0, end: value / 100 });
            }}
          />
        </div>
        <div>
          {@render optionLabel($_('media.danmaku.opacity'))}
          <Range
            bind:value={$danmaku.opacity}
            class="pt-2"
            textClass="text-white/80"
            sliderClass="range-primary"
            onchange={(value) => {
              danmakuPlugin.setOpacity(value / 100);
            }}
          />
        </div>
        <div>
          {@render optionLabel($_('media.danmaku.font_size'))}
          <Range
            bind:value={$danmaku.fontSize}
            min={5}
            max={50}
            unit="px"
            class="pt-2"
            textClass="text-white/80"
            sliderClass="range-primary"
            onchange={(value) => {
              danmakuPlugin.setFontSize(value, null);
            }}
          />
        </div>
        <div>
          {@render optionLabel($_('media.danmaku.speed'))}
          <Range
            bind:value={$danmaku.speed}
            values={[0.5, 0.75, '1.0', 1.25, 1.5]}
            unit="x"
            class="pt-2"
            textClass="text-white/80"
            sliderClass="range-primary"
            onchange={(value) => {
              danmakuPlugin.danmujs?.setPlayRate('scroll', value);
            }}
          />
        </div>
      {/if}
    </div>

    <!-- The danmaku match tab. -->
    {#if localMedia}
      {@const btnClass = 'border-0 bg-primary/30 text-white shadow-none hover:bg-base-300 hover:text-base-content'}
      {@const btnDisabledClass = 'disabled:bg-primary/15 disabled:text-white/15'}
      <div class="video-settings-content" hidden={tabId !== 'match'}>
        {#if danmakuCache}
          <div class="mb-4 flex-col items-start! gap-1!">
            {@render optionLabel($_('media.danmaku.cache'))}
            <div class="flex w-full items-center justify-between gap-4">
              {#if danmakuMeta}
                <div class="flex min-w-0 flex-col gap-0.5">
                  <span class="truncate text-xs font-medium text-white/50" title={danmakuMeta.anime_title}>
                    {danmakuMeta.anime_title}
                  </span>
                  <span class="truncate text-xs text-white/30" title={danmakuMeta.episode_title}>
                    {danmakuMeta.episode_title}
                  </span>
                </div>
              {/if}
              <Button
                icon={icons.delete}
                text={$_('action.delete', $_('media.danmaku.cache'))}
                class={btnClass}
                onclick={() => deleteLocalDanmakus()}
              />
            </div>
          </div>
        {/if}
        <div class="flex-col items-start! gap-1!">
          {@render optionLabel($_('media.danmaku.manual'))}
          <div class="flex w-full gap-2">
            <input class="input grow" placeholder={$_('field.title')} bind:value={animeTitle} />
            <Button
              ghost={false}
              square={false}
              icon={icons.search}
              text={$_('action.search')}
              class="min-w-18 {btnClass} {btnDisabledClass}"
              disabled={$searching || !animeTitle.trim()}
              onclick={() => searchEpisodes()}
            />
          </div>
          <div
            class="relative mt-2 h-40 overflow-y-auto rounded-box border"
            style="border-color: color-mix(in oklab, #fff 10%, transparent) !important;"
          >
            <Overlay loading={$searching} fixed={false} animation="spinner" />
            <table class="table table-fixed table-xs">
              <thead>
                <tr class="text-xs font-semibold text-white/40 uppercase">
                  <th class="w-6"></th>
                  <th class="w-16">{$_('field.type')}</th>
                  <th class="w-1/3">{$_('field.title')}</th>
                  <th>{$_('field.episode_title')}</th>
                </tr>
              </thead>
              <tbody>
                {#if results.length > 0}
                  {#each results as result, i (i)}
                    <tr
                      class="cursor-pointer hover:bg-white/5 {index === i ? 'bg-primary/15' : ''}"
                      onclick={() => (index = index === i ? -1 : i)}
                    >
                      <td>
                        <input
                          type="radio"
                          class="pointer-events-none radio border-white/10 radio-xs"
                          checked={index === i}
                        />
                      </td>
                      <td class="truncate text-white/40" title={result.type_description}>
                        {result.type_description || EMPTY_SIGN}
                      </td>
                      <td class="truncate font-medium text-white/80" title={result.anime_title}>
                        {result.anime_title || EMPTY_SIGN}
                      </td>
                      <td class="truncate text-white/60" title={result.episode_title}>
                        {result.episode_title || EMPTY_SIGN}
                      </td>
                    </tr>
                  {/each}
                {:else}
                  <tr>
                    <td colspan="4" class="h-32 text-center text-sm text-white/20">
                      {$_('data.nodata')}
                    </td>
                  </tr>
                {/if}
              </tbody>
            </table>
          </div>
          <div class="mt-2 flex w-full justify-end">
            <Button
              ghost={false}
              square={false}
              text={$_('message.confirm')}
              class="min-w-18 {btnClass} {btnDisabledClass}"
              loading={$confirming}
              disabled={$confirming !== null || index < 0}
              onclick={() => confirmEpisode()}
            />
          </div>
        </div>
      </div>
    {/if}
  </div>
</Modal>

<!-- The tab label rendering snippet. -->
{#snippet tabLabel(id: string, icon: IconifyIcon, name: string)}
  {@const checked = tabId === id}
  {@const tabClass = checked ? '!text-white/80' : '!text-white/20 hover:!text-white/80'}
  <label class="tab -mt-0.5 mb-4 h-8 gap-1 rounded-field px-1.75 transition-colors {tabClass}">
    <input type="radio" {checked} value={id} bind:group={tabId} />
    <iconify-icon {icon} width="1.125rem" class="mt-0.5 size-4.5"></iconify-icon>
    <span class="text-base sm:text-lg font-bold">{name}</span>
  </label>
{/snippet}

<!-- The option label rendering snippet. -->
{#snippet optionLabel(name: string, tip?: string)}
  <div class="max-w-40 min-w-20 shrink-0 text-white/60">
    <span class="text-base leading-tight font-semibold">{name}</span>
    {#if tip}
      <span class="option-label-tip" use:tooltip={{ content: tip, followCursor: true }}>
        <iconify-icon icon={icons.questionCircle} width="1.125rem"></iconify-icon>
      </span>
    {/if}
  </div>
{/snippet}

<!-- The danmaku block rendering snippet. -->
{#snippet danmakuBlock(mode: DanmakuMode, icon: IconifyIcon)}
  {@const blocked = $danmaku?.blocks.includes(mode)}
  <button
    class="flex-col-center cursor-pointer transition-all {blocked
      ? 'text-white/80'
      : 'text-white/20 hover:text-white/50'}"
    onclick={() => {
      if ($danmaku !== null && danmakuPlugin !== null) {
        if (blocked) {
          $danmaku.blocks = $danmaku.blocks.filter((m) => m !== mode);
          danmakuPlugin.showMode(mode);
        } else {
          $danmaku.blocks = [...$danmaku.blocks, mode];
          danmakuPlugin.hideMode(mode);
        }
      }
    }}
  >
    <span class="relative flex-center size-8">
      <iconify-icon {icon} width="1.75rem"></iconify-icon>
      {#if blocked}
        <iconify-icon
          icon={icons.line}
          width="2rem"
          class="absolute text-gray-200"
          rotate="90deg"
          transition:fade={{ duration: 150 }}
        ></iconify-icon>
      {/if}
    </span>
    <span class="text-xs">{$_(`media.danmaku.mode.${mode}`)}</span>
  </button>
{/snippet}

<style>
  :global {
    .video-settings {
      .auto-margin-bottom {
        @media (height >= 40rem) {
          margin-bottom: var(--ks-dock-h);
        }
      }

      .select {
        --size: 1.5rem;
        font-size: 0.6875rem;
        width: 8rem;
        color: color-mix(in oklab, #fff 80%, transparent);
        border-color: color-mix(in oklab, #fff 10%, transparent);
        background-color: hsla(0, 0%, 20%, 0.9);
        box-shadow: unset;
        &:is(:disabled, [disabled]) {
          opacity: 0.3;
        }
      }
    }
  }

  .btn {
    --size: 1.5rem;
    --btn-p: 0.5rem;
    --fontsize: 0.6875rem;
    color: color-mix(in oklab, #fff 20%, transparent);
    border-color: hsla(0, 0%, 20%, 0.9);
    background-color: hsla(0, 0%, 20%, 0.9);
    box-shadow: unset;
    &.btn-active {
      color: color-mix(in oklab, #fff 80%, transparent);
      border-color: hsla(0, 0%, 30%, 0.9);
      background-color: hsla(0, 0%, 30%, 0.9);
    }
  }

  .input {
    --size: 2rem;
    font-size: 0.75rem;
    color: color-mix(in oklab, #fff 80%, transparent);
    border-color: color-mix(in oklab, #fff 10%, transparent);
    background-color: color-mix(in oklab, #fff 5%, transparent);
    box-shadow: 0 0 #0000;
    &::placeholder {
      color: color-mix(in oklab, #fff 20%, transparent);
    }
  }

  .toggle {
    background-color: color-mix(in oklab, #fff 60%, transparent);
    box-shadow: unset;
    &:checked {
      color: var(--color-primary-content);
      border-color: var(--color-primary);
      background-color: var(--color-primary);
    }
  }

  .video-settings-header {
    display: flex;
    align-items: start;
    column-gap: 0.25rem;
  }

  .video-settings-tabs {
    flex: 1;
    min-width: 0;
  }

  .video-settings-close {
    display: flex;
    width: 1.75rem;
    height: 1.75rem;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    border-radius: calc(infinity * 1px);
    color: color-mix(in oklab, #fff 60%, transparent);
    cursor: pointer;
    transition: all var(--default-transition-duration) var(--default-transition-timing-function);

    &:is(:hover, :focus-visible) {
      color: color-mix(in oklab, #fff 90%, transparent);
      background-color: color-mix(in oklab, #fff 12%, transparent);
    }
  }

  .video-settings-content {
    max-height: 30rem;
    border-top: var(--border) solid color-mix(in oklab, #fff 10%, transparent);
    > div {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      min-height: 3.25rem;
      margin-top: 0.25rem;
      &:first-child {
        margin-top: 0.75rem;
      }
    }
  }

  .option-label-tip {
    display: inline-flex;
    margin-left: 0.25rem;
    line-height: 1;
    vertical-align: middle;
    color: currentColor;
    cursor: help;
    transform: translateY(-0.0625rem);
  }

  .subtitle-setting-field {
    --size: 1.5rem;
    width: 8rem;
    height: var(--size);
    min-height: var(--size);
    font-size: 0.6875rem;
  }

  .subtitle-time-offset-field {
    gap: 0;
    input {
      appearance: textfield;
      padding-right: 1.25rem;
      &::-webkit-inner-spin-button,
      &::-webkit-outer-spin-button {
        appearance: none;
        margin: 0;
      }
    }
  }

  .subtitle-time-offset-unit {
    position: absolute;
    right: 1.125rem;
    color: color-mix(in oklab, #fff 80%, transparent);
    pointer-events: none;
  }
</style>
