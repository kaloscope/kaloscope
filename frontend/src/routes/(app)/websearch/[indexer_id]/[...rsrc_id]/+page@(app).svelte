<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { alert, ImageViewer, Overlay, TextViewer, VideoPlayer } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import type { BaseResp, Chapter, Resource, Resp } from '$lib/types';
  import { isDashSupported } from '$lib/utils';
  import { isHTTPError } from 'ky';
  import { onDestroy, onMount, tick } from 'svelte';
  import { queryParameters, ssp } from 'sveltekit-search-params';
  import { UAParser } from 'ua-parser-js';

  // the URL query parameters
  const query = queryParameters(
    {
      chapter_id: ssp.string(),
      media_type: ssp.string(),
      video_type: ssp.string()
    },
    {
      pushHistory: false
    }
  );

  // the websearch result
  let resource: Resource | null = $state(null);
  let mediaType: string | null = $derived.by(() => resource?.media_type ?? query.media_type);
  let videoType: string | null = $derived.by(() => resource?.video_type ?? query.video_type);

  let refreshKey: number = $state(0);
  // the text viewer instance
  let textViewer: TextViewer | null = $state(null);
  // the image viewer instance
  let imageViewer: ImageViewer | null = $state(null);
  // the video player instance
  let videoPlayer: VideoPlayer | null = $state(null);

  // the loading state
  const loading = createLoading();

  // the active chapter ID
  let activeChapterId: string | null = $state(null);
  // guard to limit fallback to first chapter once per load
  let chapterFallbackTried: boolean = false;

  // identify stale loads after a newer load starts or the page is left
  let requestVersion = 0;
  let abortController: AbortController | null = null;

  /**
   * Run a task with cancellation, loading state, and error handling.
   *
   * @param task - The task receiving the request identifier and cancellation signal.
   * @returns A promise that settles after loading or handling a failure.
   */
  async function withLoading(task: (requestId: number, signal: AbortSignal) => Promise<void>): Promise<void> {
    abortController?.abort();
    abortController = new AbortController();
    loading.start(0);
    const requestId = ++requestVersion;
    try {
      await task(requestId, abortController.signal);
    } catch (error) {
      if (requestId === requestVersion) {
        await returnToList(error);
      }
    } finally {
      if (requestId === requestVersion) {
        loading.end();
      }
    }
  }

  /**
   * Synchronize the chapter query before continuing a resource load.
   *
   * @param chapterId - The chapter identifier, or `null` to remove the parameter.
   * @returns A promise that settles after the query navigation.
   */
  async function updateChapterId(chapterId: string | null): Promise<void> {
    const url = new URL(page.url);
    if (chapterId !== null) {
      url.searchParams.set('chapter_id', chapterId);
    } else {
      url.searchParams.delete('chapter_id');
    }
    url.searchParams.sort();
    await goto(url, { replaceState: true, keepFocus: true, noScroll: true });
  }

  /**
   * Fetch resource content or the chapter directory from the workflow.
   *
   * @param chapterId - The requested chapter, or `null` for the directory.
   * @param signal - The signal cancelling a superseded request.
   * @returns The resource returned by the workflow, or `null` when unavailable.
   */
  async function fetchResource(chapterId: string | null, signal: AbortSignal): Promise<Resource | null> {
    const userAgent = UAParser(navigator.userAgent);
    const { data } = await api
      .post(`flow/graph/${page.params.indexer_id}/execute`, {
        signal,
        json: {
          $start: 'details_start',
          id: page.params.rsrc_id,
          chapter_id: chapterId,
          dash_supported: isDashSupported(),
          ua: {
            ...userAgent,
            navigator: {
              platform: navigator.platform,
              maxTouchPoints: navigator.maxTouchPoints
            }
          }
        }
      })
      .json<Resp<Resource | null>>();
    return data;
  }

  /**
   * Fetch and display the requested resource, including retries of the same chapter.
   *
   * @param chapterId - The chapter to request, defaulting to the current URL.
   * @returns A promise that settles after loading and mounting the resource.
   */
  async function load(chapterId: string | null = null): Promise<void> {
    if (chapterId === null) {
      chapterFallbackTried = false;
    }
    activeChapterId = chapterId ?? query.chapter_id ?? null;
    await withLoading(async (requestId, signal) => {
      if (chapterId !== null) {
        await updateChapterId(chapterId);
        if (requestId !== requestVersion) {
          return;
        }
      }
      const data = await fetchResource(activeChapterId, signal);
      if (requestId === requestVersion) {
        await mount(data, requestId);
      }
    });
  }

  /**
   * Mount available content after the matching viewer has rendered.
   *
   * Synchronize a workflow's resolved video chapter when its ID matches the returned chapter list.
   *
   * @param rsrc - The resource returned by the workflow.
   * @param requestId - The request identifier used to ignore superseded loads.
   * @param next - Whether to reuse the video player for a direct chapter change.
   * @returns A promise that settles after mounting or requesting the first chapter.
   */
  async function mount(rsrc: Resource | null, requestId: number, next: boolean = false): Promise<void> {
    if (!rsrc) {
      await returnToList();
      return;
    }
    resource = rsrc;
    const chapters = rsrc.chapters ?? [];
    const images = (rsrc.images ?? []).filter((url) => url.trim());
    const hasText = Array.isArray(rsrc.text) ? rsrc.text.some((text) => text.trim()) : !!rsrc.text?.trim();
    const hasContent =
      (mediaType === 'video' && !!rsrc.url?.trim()) ||
      (mediaType === 'image' && images.length > 0) ||
      (mediaType === 'text' && hasText);
    if (!hasContent) {
      // restore a selected direct chapter when the workflow returns only its directory
      const chapter =
        mediaType === 'video' && activeChapterId
          ? chapters.find((chapter) => (chapter.id || chapter.url) === activeChapterId && chapter.url?.trim())
          : undefined;
      // directory responses still open their first chapter on initial entry
      const firstChapter =
        !activeChapterId && !chapterFallbackTried ? chapters.find((chapter) => chapter.id || chapter.url) : undefined;
      if (chapter) {
        await onchange(chapter);
      } else if (firstChapter) {
        chapterFallbackTried = true;
        await onchange(firstChapter);
      } else {
        await returnToList();
      }
      return;
    }
    activeChapterId ??= chapters[0]?.id ?? null;
    if (!next) {
      refreshKey += 1;
    }
    // wait for the selected viewer to mount before loading its content
    await tick();
    if (requestId !== requestVersion) {
      return;
    }
    if (!next && mediaType === 'video' && rsrc.id && chapters.some((chapter) => chapter.id === rsrc.id)) {
      activeChapterId = rsrc.id;
      await updateChapterId(rsrc.id);
      if (requestId !== requestVersion) {
        return;
      }
    }
    const viewerOptions = { title: rsrc.title, chapters, chapterId: activeChapterId, chapterChange: onchange };
    if (mediaType === 'text' && textViewer) {
      textViewer.mount({
        ...viewerOptions,
        text: rsrc.text!
      });
    } else if (mediaType === 'image' && imageViewer) {
      imageViewer.mount({
        ...viewerOptions,
        images,
        image_count: rsrc.image_count
      });
    } else if (mediaType === 'video' && videoPlayer) {
      await videoPlayer.mount({
        ...viewerOptions,
        next,
        url: rsrc.url!,
        danmakus: rsrc.danmakus,
        videoType,
        definitions: rsrc.definitions,
        uploader: rsrc.uploader,
        uploadedAt: rsrc.uploaded_at
      });
    } else {
      await returnToList();
    }
  }

  /**
   * Display a selected chapter, allowing the current chapter to be retried.
   *
   * Prefer direct video URLs and synchronize the chapter query before loading.
   *
   * @param chapter - The chapter whose direct URL or workflow identifier to load.
   * @returns A promise that settles after loading the selected chapter.
   */
  async function onchange(chapter: Chapter): Promise<void> {
    const { id, url, title } = chapter;
    if (mediaType === 'video' && url?.trim()) {
      activeChapterId = id || url;
      await withLoading(async (requestId) => {
        await updateChapterId(id || null);
        if (requestId === requestVersion) {
          await mount({ ...resource, url, title, definitions: [], danmakus: [] }, requestId, true);
        }
      });
    } else if (id) {
      await load(id);
    } else {
      await returnToList();
    }
  }

  /**
   * Return to the resource list and report a load failure after arriving.
   *
   * @param error - An optional request or viewer initialization error.
   * @returns A promise that settles after the redirect and any applicable failure alert.
   */
  async function returnToList(error?: unknown): Promise<void> {
    // authentication failures are already redirected to login by the API client
    if (isHTTPError(error) && error.response.status === 401) {
      return;
    }
    const response = isHTTPError(error) ? (error.data as BaseResp | undefined) : undefined;
    const listPath = `/websearch/${page.params.indexer_id}`;
    try {
      await goto(listPath, { replaceState: true });
    } catch {
      // ignore failed redirects
      return;
    }
    // a superseded `goto` can resolve without reaching the list
    if (page.url.pathname !== listPath) {
      return;
    }
    // preserve the API client's existing error message without a second alert
    if (!response?.message && response?.status !== 500) {
      alert({ level: 'error', message: 'resource_load_failed', unique: true });
    }
  }

  beforeNavigate(({ from, to }) => {
    // invalidate pending loads before the destination starts loading, but allow chapter query changes
    if (from && (from.url.origin !== to?.url.origin || from.url.pathname !== to?.url.pathname)) {
      requestVersion += 1;
      abortController?.abort();
    }
  });

  onDestroy(() => {
    requestVersion += 1;
    abortController?.abort();
  });

  onMount(() => {
    load();
  });
</script>

<div class="history-back fixed inset-0 layer-1 {mediaType === 'video' ? 'max-sm:bottom-(--ks-dock-h)' : ''}">
  <Overlay black={mediaType !== 'text'} loading={$loading} />
  {#key refreshKey}
    {#if mediaType === 'text'}
      <TextViewer bind:this={textViewer} />
    {:else if mediaType === 'image'}
      <ImageViewer bind:this={imageViewer} />
    {:else if mediaType === 'video'}
      <VideoPlayer bind:this={videoPlayer} />
    {/if}
  {/key}
</div>
