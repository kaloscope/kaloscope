<script lang="ts">
  import { afterNavigate, beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { tooltip } from '$lib/actions';
  import { assistantCanvas } from '$lib/assistant/canvas';
  import { assistantDestination } from '$lib/assistant/navigation';
  import {
    assistant,
    assistantHandoff,
    assistantRestoring,
    closeAssistant,
    loadAssistantModels,
    loadAssistantConversation,
    releaseAssistantHandoff,
    sendAssistant,
    stopAssistant,
    type AssistantTool as Tool,
    type AssistantTurn
  } from '$lib/assistant/session';
  import { Button, Dropdown, PlanEditor, Select } from '$lib/components';
  import { closeDropdowns } from '$lib/components/common/interaction/Dropdown.svelte';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores';
  import type { DownloadPlanCreation, DownloadPlanPreview } from '$lib/types';
  import { toDatetimeLocal } from '$lib/utils/datetime';
  import { normalizePathname } from '$lib/utils/url';
  import { onMount, tick, untrack } from 'svelte';
  import AssistantHistory from './AssistantHistory.svelte';
  import AssistantChanges from './AssistantChanges.svelte';
  import AssistantMarkdown from './AssistantMarkdown.svelte';
  import AssistantTool from './AssistantTool.svelte';

  let panel: HTMLDivElement | undefined = $state();
  let composer: HTMLTextAreaElement | undefined = $state();
  let transcript: HTMLOListElement | undefined = $state();
  let primary: HTMLButtonElement | undefined = $state();
  let mover: HTMLButtonElement | undefined = $state();
  let returnFocus: HTMLElement | null = null;
  let restoreAfterNavigation = false;
  let following = $state(true);
  let lastScrollTop = 0;
  let menuHeight = $state('16rem');
  let navigationFailure: string | null = $state(null);
  let planEditor: PlanEditor | undefined = $state();
  let planPreview: DownloadPlanPreview | null = $state(null);
  let planConversationId: number | undefined = $state();
  let createdPlan: DownloadPlanCreation | null = $state(null);
  let viewportHeight: number | undefined = $state();
  let position: { x: number; y: number } | null = $state(null);
  let drag: { x: number; y: number; left: number; top: number } | null = null;
  const turns = $derived($assistant.turns);
  $effect(() => {
    if (planConversationId !== $assistant.conversation?.id) {
      planPreview = null;
      createdPlan = null;
    }
  });
  const input = $derived($assistant.input);
  const lastTurn = $derived(turns.at(-1));
  const isOpen = $derived($assistant.open);
  const compact = $derived(viewportHeight !== undefined && viewportHeight < 540);
  const rows = $derived(compact ? 1 : 2);
  const modelsBusy = $derived($assistant.modelsLoading !== null);
  const canSend = $derived(
    !$assistantCanvas &&
      !modelsBusy &&
      !$assistantRestoring &&
      !$assistant.historyLoading &&
      !$assistant.stopping &&
      !$assistant.conversation?.busy &&
      !!input.trim() &&
      !!$assistant.model
  );
  const modelValue = $derived($assistant.model ? JSON.stringify($assistant.model) : null);
  const currentPage = $derived.by(() => {
    const pathname = page.url.pathname;
    if (/\/graphs\/(r\/)?\d+\/?$/.test(pathname)) return 'workflow_designer';
    if (/\/workflows\/graphs\/?$/.test(pathname)) return 'workflows';
    if (/^\/settings\/workflows\/schedules\/?$/.test(pathname)) return 'workflow_jobs';
    if (/^\/medialibs\/\d+\/\d+\/?$/.test(pathname)) return 'media_item';
    if (/^\/medialibs\/\d+\/?$/.test(pathname)) return 'media_library';
    if (/^\/downloads\/downloading\/?$/.test(pathname)) return 'active_downloads';
    if (/^\/downloads\/completed\/?$/.test(pathname)) return 'completed_downloads';
    if (/^\/downloads\/plans\/?$/.test(pathname)) return 'download_plans';
    return 'other';
  });
  const incomplete = $derived(!!lastTurn && lastTurn.status !== 'complete' && lastTurn.status !== 'streaming');
  const retryable = $derived(incomplete || !!lastTurn?.canvasError);
  const destinations = $derived.by(() => {
    const items: { path: string; label: string }[] = [];
    const pathname = normalizePathname(page.url.pathname);
    for (const { result } of lastTurn?.tools ?? []) {
      const destination = result && assistantDestination(result);
      if (!destination) continue;
      const { path, target } = destination;
      if (path === pathname || items.some((item) => item.path === path)) continue;
      const name = $_(`assistant.pages.${target.page}`);
      items.push({ path, label: $_('action.open', target.resource_id ? `${name} #${target.resource_id}` : name) });
    }
    return items;
  });
  // a created draft waiting for its designer belongs to the conversation that created it
  const handoff = $derived(
    $assistantHandoff?.conversationId === $assistant.conversation?.id ? $assistantHandoff : null
  );

  /**
   * Keep a dragged panel inside the visible viewport, including after resizing.
   *
   * @param x - Requested left coordinate.
   * @param y - Requested top coordinate.
   */
  function move(x: number, y: number): void {
    if (!panel) return;
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft ?? 0;
    const top = viewport?.offsetTop ?? 0;
    const width = viewport?.width ?? window.innerWidth;
    const height = viewport?.height ?? window.innerHeight;
    position = {
      x: Math.max(left + 8, Math.min(x, left + width - panel.offsetWidth - 8)),
      y: Math.max(top + 8, Math.min(y, top + height - panel.offsetHeight - 8))
    };
  }

  /**
   * Start a pointer drag on the dedicated move handle.
   *
   * @param event - Primary mouse, pen, or touch press.
   */
  function startDrag(event: PointerEvent): void {
    if (event.button !== 0 || !panel) return;
    const rect = panel.getBoundingClientRect();
    drag = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }

  /**
   * Move the floating panel without scrolling or changing the underlying page.
   *
   * @param event - Captured pointer movement.
   */
  function dragPanel(event: PointerEvent): void {
    if (drag) move(drag.left + event.clientX - drag.x, drag.top + event.clientY - drag.y);
  }

  /**
   * Handle panel shortcuts without sending keystrokes to the workflow canvas.
   *
   * @param event - Key press within the nonmodal conversation.
   */
  function panelKeydown(event: KeyboardEvent): void {
    event.stopPropagation();
    const menu = panel?.querySelector<HTMLDetailsElement>('details[open]:has(> .dropdown-content)');
    if (event.key === 'Escape' && menu) {
      // close the overflow menu first and keep focus on its trigger
      event.preventDefault();
      menu.open = false;
      menu.querySelector('summary')?.focus();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeAssistant();
    } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault();
      void sendAssistant(page.url.pathname);
    }
  }

  /**
   * Fit the panel to viewport changes, including the mobile keyboard.
   */
  function resize(): void {
    viewportHeight = window.visualViewport?.height ?? window.innerHeight;
    tick().then(() => {
      if (!panel) return;
      const rect = panel.getBoundingClientRect();
      const top = window.visualViewport?.offsetTop ?? 0;
      if (position || rect.bottom > top + viewportHeight! - 8 || rect.top < top + 8) move(rect.left, rect.top);
    });
  }

  /**
   * Grow the composer with its text until the CSS maximum height starts scrolling.
   */
  function fitComposer(): void {
    if (!composer) return;
    composer.style.height = 'auto';
    composer.style.height = `${composer.scrollHeight}px`;
  }

  /**
   * Follow the latest output near the bottom and stop following after the reader scrolls up.
   */
  function trackScroll(): void {
    if (!transcript) return;
    const { scrollTop, scrollHeight, clientHeight } = transcript;
    if (scrollHeight - scrollTop - clientHeight < 48) following = true;
    else if (scrollTop < lastScrollTop) following = false;
    lastScrollTop = scrollTop;
  }

  /**
   * Scroll to the newest conversation content and resume following it.
   *
   * @param smooth - Animate the scroll unless the reader prefers reduced motion.
   */
  function showLatest(smooth = false): void {
    if (!transcript) return;
    following = true;
    const animate = smooth && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    transcript.scrollTo({ top: transcript.scrollHeight, behavior: animate ? 'smooth' : 'instant' });
  }

  /**
   * Send the composed message, or stop the active reply from the same control.
   *
   * @param event - Click or keyboard activation of the primary composer action.
   */
  function submit(event: MouseEvent): void {
    // the second click of a double-click must not stop the reply it just started
    if (event.detail > 1) return;
    if ($assistant.busy) stopAssistant();
    else if (canSend) void sendAssistant(page.url.pathname);
  }

  /**
   * Send a turn's prompt again using the current page.
   *
   * @param turn - Incomplete turn to retry, or a completed turn whose edit could not be applied.
   */
  function retry(turn: AssistantTurn): void {
    // the action strip disappears while generating, so keep focus on the stop control
    primary?.focus({ preventScroll: true });
    void sendAssistant(page.url.pathname, turn.id);
  }

  /**
   * Open a validated destination through the existing unsaved-navigation guard.
   *
   * Ignore the current page so its designer does not enter the confirmed-leaving state.
   *
   * @param path - Registered internal path from a navigation tool result.
   * @param turn - Turn that offered the destination.
   * @returns A promise that resolves after skipping the current page or attempting navigation.
   */
  async function navigate(path: string, turn: string): Promise<void> {
    navigationFailure = null;
    if (normalizePathname(path) === normalizePathname(page.url.pathname)) return;
    try {
      await goto(path);
    } catch {
      navigationFailure = turn;
    }
  }

  /**
   * Open plain plan settings in the existing editor without searching or creating.
   * Keep ingestion reviews in the conversation so their binding is confirmed together.
   *
   * @param tool - Successful validation whose request identity is reused on save.
   * @returns A promise that resolves when the editor is open.
   */
  async function reviewPlan(tool: Tool): Promise<void> {
    if (!tool.result?.ok || !tool.result.data?.valid || tool.result.data.ingestion) return;
    if (planPreview?.request_id !== tool.result.data.request_id) {
      planPreview = null;
      await tick();
      planPreview = tool.result.data as unknown as DownloadPlanPreview;
      planConversationId = $assistant.conversation?.id;
      createdPlan = null;
      await tick();
    }
    try {
      await planEditor?.showModal();
    } catch {
      // the shared API client displays the recovery lookup error
    }
  }

  /**
   * Size the overflow menu to the space above its trigger before it opens.
   *
   * @param event - Click on the menu trigger.
   */
  function openMenu(event: MouseEvent): void {
    const trigger = event.currentTarget as HTMLElement;
    closeDropdowns(trigger);
    const top = window.visualViewport?.offsetTop ?? 0;
    menuHeight = `${Math.max(96, trigger.getBoundingClientRect().top - top - 12)}px`;
  }

  /**
   * Close the overflow menu and return focus to its trigger or another target.
   *
   * @param target - Element to focus instead of the menu trigger.
   */
  function closeMenu(target?: HTMLElement): void {
    closeDropdowns();
    (target ?? panel?.querySelector<HTMLElement>('.assistant-menu > summary'))?.focus({ preventScroll: true });
  }

  /**
   * Split a reply into text and tool runs in the order they were streamed.
   *
   * @param turn - Conversation turn with tool start offsets.
   * @returns Text segments and consecutive tool groups with stable keys.
   */
  function segments(turn: AssistantTurn): ({ key: string; text: string } | { key: string; tools: Tool[] })[] {
    const parts: ({ key: string; text: string } | { key: string; tools: Tool[] })[] = [];
    let offset = 0;
    for (const tool of turn.tools) {
      const text = turn.content.slice(offset, tool.offset);
      if (text.trim()) parts.push({ key: `text-${offset}`, text });
      offset = Math.max(offset, tool.offset);
      const previous = parts.at(-1);
      if (previous && 'tools' in previous) previous.tools.push(tool);
      else parts.push({ key: tool.id, tools: [tool] });
    }
    const rest = turn.content.slice(offset);
    if (rest.trim()) parts.push({ key: `text-${offset}`, text: rest });
    return parts;
  }

  /**
   * Describe the captured page before or after its snapshot upload.
   *
   * @param context - Snapshot captured when the turn was sent.
   * @returns A short localized label with the resource and draft state.
   */
  function contextLabel(context: NonNullable<AssistantTurn['context']>): string {
    const parts = [$_(`assistant.pages.${context.page_type}`)];
    if (context.page_type === 'workflow_designer') {
      parts.push(`#${context.resource_id}`, $_(context.unsaved ? 'assistant.unsaved' : 'assistant.saved'));
    } else if (context.page_type === 'media_library' || context.page_type === 'media_item') {
      parts.push(`#${context.resource_id}`);
    }
    return parts.join(' · ');
  }

  /**
   * Describe failures with recovery steps that apply to the assistant.
   *
   * @param code - Public application error code.
   * @returns Assistant-specific guidance, a shared message, or a generic fallback.
   */
  function errorMessage(code: string): string {
    return $_(`assistant.errors.${code}`, {
      default: $_(`alert.${code}`, { default: $_('assistant.request_failed') })
    });
  }

  beforeNavigate(() => {
    restoreAfterNavigation = !!panel?.contains(document.activeElement);
  });
  afterNavigate(({ from, to }) => {
    if ($assistant.open && restoreAfterNavigation) composer?.focus({ preventScroll: true });
    restoreAfterNavigation = false;
    if (from?.url && to?.url && from.url.pathname !== to.url.pathname) releaseAssistantHandoff(to.url.pathname);
  });

  onMount(() => {
    resize();
    window.visualViewport?.addEventListener('resize', resize);
    return () => window.visualViewport?.removeEventListener('resize', resize);
  });

  $effect(() => {
    if (isOpen) {
      returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      tick().then(() => {
        if ($assistant.open) {
          resize();
          (composer ?? panel)?.focus({ preventScroll: true });
        }
      });
    } else {
      tick().then(() => {
        if ($assistant.open) return;
        // restore focus only when it left with the panel, not after a click on the topbar trigger
        const lost = !document.activeElement || document.activeElement === document.body;
        if (lost && returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
        else if (lost && returnFocus) document.getElementById('assistant-trigger')?.focus({ preventScroll: true });
        returnFocus = null;
      });
    }
  });

  $effect(() => {
    // reopen at the latest message and keep it pinned when the composer or action strip resizes the transcript
    const element = transcript;
    if (!element) return;
    following = true;
    lastScrollTop = 0;
    const observer = new ResizeObserver(() => {
      if (following) element.scrollTop = element.scrollHeight;
    });
    observer.observe(element);
    return () => observer.disconnect();
  });

  $effect(() => {
    if (!turns.length) {
      following = true;
      lastScrollTop = 0;
    }
    if (transcript && turns) untrack(() => following && showLatest());
  });

  $effect(() => {
    // refit once the textarea shows the current text and row count, including changes made outside typing
    if (composer && composer.value === input && composer.rows === rows) fitComposer();
  });
</script>

<svelte:window onresize={resize} />

{#if isOpen}
  <div
    id="assistant-panel"
    role="dialog"
    aria-modal="false"
    aria-labelledby="assistant-title"
    aria-describedby="assistant-help"
    tabindex="-1"
    bind:this={panel}
    class="assistant-panel @container fixed layer-1 flex flex-col rounded-box bg-blur-90 text-base-content shadow-[var(--assistant-panel-shadow)] ring-1 ring-border"
    class:compact
    style:max-height={viewportHeight ? `${Math.max(0, viewportHeight - 16)}px` : undefined}
    style:left={position ? `${position.x}px` : undefined}
    style:top={position ? `${position.y}px` : undefined}
    style:right={position ? 'auto' : undefined}
    style:bottom={position ? 'auto' : undefined}
    onkeydown={panelKeydown}
  >
    <h2 id="assistant-title" class="sr-only">{$_('assistant.title')}</h2>
    <p id="assistant-help" class="sr-only">
      {$_($assistantCanvas ? 'assistant.canvas.locked' : 'assistant.keyboard')}
    </p>

    <div class="flex h-8 shrink-0 items-center px-1.5 pt-1">
      <!-- the left inset matches the close button so the grip stays centered on the panel -->
      <button
        bind:this={mover}
        type="button"
        class="group flex h-7 min-w-0 flex-1 cursor-move touch-none items-center justify-center rounded-field pl-6 focus-visible:outline-2 focus-visible:outline-primary"
        aria-label={$_('assistant.move')}
        onpointerdown={startDrag}
        onpointermove={dragPanel}
        onpointerup={() => (drag = null)}
        onpointercancel={() => (drag = null)}
        onlostpointercapture={() => (drag = null)}
      >
        <span
          class="h-1 w-9 rounded-full bg-base-content/20 transition-colors group-hover:bg-base-content/40 group-focus-visible:bg-primary/70"
          aria-hidden="true"
        ></span>
      </button>
      <button
        type="button"
        class="btn btn-circle btn-subtle opacity-80 btn-xs"
        aria-label={$_('assistant.close')}
        disabled={!!$assistantCanvas}
        onclick={closeAssistant}
      >
        <iconify-icon icon={icons.dismiss} width="1rem" aria-hidden="true"></iconify-icon>
      </button>
    </div>

    <AssistantHistory />

    <ol
      bind:this={transcript}
      class="transcript min-h-0 flex-1 [scrollbar-gutter:stable] space-y-6 overflow-y-auto overscroll-contain px-4 pt-2 pb-3"
      aria-label={$_('assistant.conversation')}
      onscroll={trackScroll}
    >
      {#if $assistant.turnsBefore && $assistant.conversation}
        <li class="text-center">
          <button
            type="button"
            class="btn btn-subtle btn-xs"
            disabled={$assistant.busy || $assistant.stopping || $assistant.historyLoading}
            onclick={() => {
              following = false;
              void loadAssistantConversation($assistant.conversation!.id, true);
            }}>{$_('assistant.load_older')}</button
          >
        </li>
      {/if}
      {#if !turns.length && !$assistant.historyLoading && !$assistantRestoring}
        <li class="flex min-h-full flex-col justify-center gap-2 pb-4">
          <iconify-icon icon={icons.botSquare} width="2rem" class="mb-1 size-8 text-base-content/60" aria-hidden="true"
          ></iconify-icon>
          <h3 class="text-base font-semibold">{$_('assistant.welcome')}</h3>
          <p class="text-xs leading-5 text-base-content/60">{$_('assistant.saved_history')}</p>
        </li>
      {/if}
      {#each turns as turn (turn.id)}
        <li class="space-y-3">
          <div class="flex flex-col items-end gap-1 pl-8">
            <p class="sr-only">{$_('assistant.you')}</p>
            <p
              class="max-w-full rounded-box bg-base-content/8 px-3 py-2 text-sm leading-6 break-words whitespace-pre-wrap shadow-[var(--assistant-message-shadow)] ring-1 ring-base-content/10"
            >
              {turn.prompt}
            </p>
            {#if turn.context}
              <p class="flex items-center gap-1 text-[0.6875rem] leading-4 text-base-content/50">
                <iconify-icon icon={icons.contentView} width="0.75rem" class="size-3" aria-hidden="true"></iconify-icon>
                {contextLabel(turn.context)}
              </p>
            {/if}
          </div>
          <div class="min-w-0 space-y-2">
            <p class="sr-only">{$_('assistant.title')}</p>
            {#each segments(turn) as segment (segment.key)}
              {#if 'tools' in segment}
                <ul class="-mx-2 space-y-0.5" aria-label={$_('assistant.activity')}>
                  {#each segment.tools as tool (tool.id)}
                    <li>
                      <AssistantTool
                        {tool}
                        unsaved={turn.context?.page_type === 'workflow_designer' && turn.context.unsaved}
                        onplan={$user?.role === 'admin' && !$assistant.busy && !$assistantCanvas && !$assistantRestoring
                          ? () => reviewPlan(tool)
                          : undefined}
                      />
                    </li>
                  {/each}
                </ul>
              {:else}
                <AssistantMarkdown source={segment.text} />
              {/if}
            {/each}
            {#if turn.status === 'streaming'}
              <p class="flex h-6 items-center gap-2 text-xs text-base-content/60">
                <span class="loading loading-xs loading-dots" aria-hidden="true"></span>
                {$_($assistant.compacting ? 'assistant.compacting' : 'assistant.generating')}
              </p>
            {:else if turn.status !== 'complete'}
              <div class="flex items-start gap-2 text-xs leading-5 text-base-content/60">
                <iconify-icon
                  icon={turn.status === 'stopped' ? icons.subtractCircle : icons.warning}
                  width="0.875rem"
                  class="mt-0.75 size-3.5 shrink-0"
                  aria-hidden="true"
                ></iconify-icon>
                <p>
                  <span class="font-medium">{$_(`assistant.status.${turn.status}`)}</span> · {$_(
                    'assistant.incomplete'
                  )}
                  {#if turn.error}
                    <span class="block">{errorMessage(turn.error)}</span>
                  {/if}
                </p>
              </div>
            {:else if turn.canvasError}
              <div class="flex items-start gap-2 text-xs leading-5 text-base-content/60">
                <iconify-icon icon={icons.warning} width="0.875rem" class="mt-0.75 size-3.5 shrink-0" aria-hidden="true"
                ></iconify-icon>
                <p>{errorMessage(turn.canvasError)}</p>
              </div>
            {/if}
          </div>
        </li>
      {/each}
    </ol>

    <footer class="relative shrink-0 rounded-b-box px-2 pb-2">
      {#if createdPlan && lastTurn}
        <p class="mb-2 px-2 text-sm" role="status">
          {$_('assistant.plan.created', createdPlan.plan_id)} ·
          {$_(`assistant.plan.first.${createdPlan.first_execution.status}`)}
          {#if currentPage !== 'download_plans'}
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              onclick={() => navigate('/downloads/plans', lastTurn.id)}
            >
              {$_('action.open', $_('assistant.pages.download_plans'))}
            </button>
          {/if}
        </p>
      {/if}
      {#if !following && turns.length}
        <button
          type="button"
          class="btn absolute bottom-full left-1/2 mb-2 -translate-x-1/2 gap-1 rounded-full bg-base-100 font-normal shadow-md btn-xs"
          onclick={() => showLatest(true)}
        >
          <iconify-icon icon={icons.arrowDown} width="0.875rem" class="size-3.5" aria-hidden="true"></iconify-icon>
          {$_('assistant.latest')}
        </button>
      {/if}

      {#if lastTurn && (retryable || destinations.length || handoff)}
        <section
          class="mb-1.5 flex flex-wrap items-center gap-x-1 gap-y-0.5 text-xs"
          aria-label={$_('assistant.turn_actions')}
        >
          {#if handoff}
            <p class="flex basis-full items-center gap-1.5 px-1 pb-0.5 text-base-content/70" role="status">
              {#if handoff.phase === 'opening'}
                <span class="loading loading-xs loading-spinner" aria-hidden="true"></span>
              {/if}
              {$_(`assistant.create.${handoff.phase}`, handoff.workflowId)}
            </p>
          {/if}
          {#if retryable}
            <button
              type="button"
              class="btn gap-1 btn-subtle font-normal btn-xs"
              disabled={$assistant.busy ||
                $assistant.historyLoading ||
                $assistant.stopping ||
                $assistant.conversation?.busy ||
                !!$assistantCanvas}
              onclick={() => retry(lastTurn)}
            >
              <iconify-icon icon={icons.arrowClockwise} width="0.875rem" class="size-3.5" aria-hidden="true"
              ></iconify-icon>
              {$_('assistant.retry')}
            </button>
          {/if}
          {#each destinations as destination (destination.path)}
            <button
              type="button"
              class="btn gap-1 btn-subtle font-normal btn-xs"
              onclick={() => navigate(destination.path, lastTurn.id)}
            >
              <iconify-icon icon={icons.arrowRight} width="0.875rem" class="size-3.5" aria-hidden="true"></iconify-icon>
              {destination.label}
            </button>
          {/each}
          {#if navigationFailure === lastTurn.id}
            <p class="basis-full pb-0.5 text-base-content/70" role="status">{$_('assistant.navigation_cancelled')}</p>
          {/if}
        </section>
      {/if}

      {#if $assistantCanvas}
        <AssistantChanges />
      {:else}
        <div
          class="rounded-box bg-base-100 shadow-[var(--assistant-composer-shadow)] ring-1 ring-border transition-shadow has-[textarea:focus]:ring-primary/80"
        >
          <label class="sr-only" for="assistant-input">{$_('assistant.input')}</label>
          <textarea
            id="assistant-input"
            bind:this={composer}
            bind:value={$assistant.input}
            {rows}
            readonly={$assistantRestoring}
            maxlength="32768"
            class="block w-full resize-none overflow-y-auto bg-transparent px-3 pt-2.5 pb-1 text-sm leading-6 outline-none placeholder:text-base-content/60"
            class:max-h-40={!compact}
            class:max-h-20={compact}
            placeholder={$_($assistantRestoring ? 'assistant.loading_history' : 'assistant.placeholder')}
            aria-describedby="assistant-help"></textarea>

          {#if $assistant.modelsError || $assistant.error}
            <div class="space-y-0.5 px-3 pt-1 text-xs leading-5 text-base-content/60">
              {#if $assistant.modelsError}
                <p role="alert">
                  {errorMessage($assistant.modelsError)}
                </p>
              {/if}
              {#if $assistant.error}
                <p role="alert">{errorMessage($assistant.error)}</p>
              {/if}
            </div>
          {/if}

          <div class="flex items-center gap-1 p-1.5">
            {#if currentPage !== 'other'}
              <span
                class="flex h-7 min-w-0 shrink items-center gap-1 rounded-full bg-base-content/5 px-2 text-xs text-base-content/70"
              >
                <iconify-icon icon={icons.contentView} width="0.875rem" class="size-3.5 shrink-0" aria-hidden="true"
                ></iconify-icon>
                <span class="sr-only">{$_('assistant.context')}:</span>
                <span class="truncate @max-xs:sr-only">{$_(`assistant.pages.${currentPage}`)}</span>
              </span>
            {/if}
            {#if $assistant.modelsLoaded && !$assistant.models.length}
              <Button
                icon={icons.redo}
                size="xs"
                class="size-7! min-h-7! shrink-0"
                loading={$assistant.modelsLoading}
                disabled={$assistant.busy || modelsBusy}
                onclick={loadAssistantModels}
              />
            {/if}
            <div class="flex min-w-0 shrink-[2] items-center" aria-busy={modelsBusy}>
              <label class="sr-only" for="assistant-model">{$_('assistant.model')}</label>
              <Select
                id="assistant-model"
                class="[field-sizing:content] h-7 min-h-7 w-auto max-w-full min-w-0 select-ghost pr-7 pl-2 text-xs select-sm disabled:border-transparent disabled:bg-transparent disabled:text-base-content/60"
                disabled={$assistant.busy || modelsBusy || $assistantRestoring || !$assistant.models.length}
                value={modelValue}
                onchange={(event) => ($assistant.model = JSON.parse((event.currentTarget as HTMLSelectElement).value))}
              >
                <option value={null} disabled>
                  {$assistant.modelsLoaded && !$assistant.models.length
                    ? $_('assistant.no_models')
                    : $_('action.select', $_('assistant.model').toLocaleLowerCase())}
                </option>
                {#each $assistant.models as provider (provider.id)}
                  <optgroup label={provider.name}>
                    {#each provider.model_ids as model (model)}
                      <option value={JSON.stringify({ provider_id: provider.id, model_id: model })}>{model}</option>
                    {/each}
                  </optgroup>
                {/each}
              </Select>
            </div>
            <span class="flex-1"></span>
            {#if position}
              <Dropdown
                class="assistant-menu dropdown-end dropdown-top"
                triggerClass="rounded-field focus-visible:outline-2 focus-visible:outline-primary"
                contentClass="mb-1 shadow-lg!"
                contentWidth="13rem"
                contentMaxHeight={menuHeight}
                onclick={openMenu}
              >
                {#snippet trigger()}
                  <span class="btn btn-square btn-subtle btn-sm">
                    <iconify-icon icon={icons.moreVertical} width="1.125rem" class="size-4.5" aria-hidden="true"
                    ></iconify-icon>
                    <span class="sr-only">{$_('assistant.more')}</span>
                  </span>
                {/snippet}
                <ul class="menu w-full gap-0.5 menu-sm p-1.5">
                  <li>
                    <button
                      type="button"
                      onclick={() => {
                        position = null;
                        closeMenu(mover);
                      }}
                    >
                      <iconify-icon icon={icons.focusCentered} width="1rem" class="size-4" aria-hidden="true"
                      ></iconify-icon>
                      {$_('assistant.restore_position')}
                    </button>
                  </li>
                </ul>
              </Dropdown>
            {/if}
            <button
              bind:this={primary}
              type="button"
              class="btn btn-circle shrink-0 btn-sm {$assistant.busy ? 'btn-neutral' : 'btn-primary'}"
              class:btn-disabled={!$assistant.busy && !canSend}
              aria-disabled={!$assistant.busy && !canSend}
              aria-label={$_($assistant.busy ? 'assistant.stop' : 'assistant.send')}
              aria-keyshortcuts={$assistant.busy ? undefined : 'Control+Enter Meta+Enter'}
              use:tooltip={{ content: $assistant.busy ? '' : $_('assistant.send_shortcut'), zIndex: 10000 }}
              onclick={submit}
            >
              <iconify-icon
                icon={$assistant.busy ? icons.stopFilled : icons.arrowUp}
                width={$assistant.busy ? '0.875rem' : '1.125rem'}
                aria-hidden="true"
              ></iconify-icon>
            </button>
          </div>
        </div>
      {/if}
    </footer>
    <span class="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {$assistant.busy ? $_('assistant.generating') : lastTurn ? $_(`assistant.status.${lastTurn.status}`) : ''}
    </span>
  </div>
{/if}

{#if planPreview && $user?.role === 'admin'}
  <PlanEditor
    bind:this={planEditor}
    {...planPreview.plan}
    graph_name={planPreview.workflow_name}
    interval_start={toDatetimeLocal(planPreview.plan.interval_start)}
    interval_end={toDatetimeLocal(planPreview.plan.interval_end)}
    creationRequestId={planPreview.request_id}
    oncreated={(result) => {
      createdPlan = result;
      window.dispatchEvent(new Event('download-plan-created'));
    }}
  />
{/if}

<style>
  .assistant-panel {
    --assistant-panel-shadow:
      0 8px 24px -6px light-dark(oklch(0% 0 0 / 16%), oklch(0% 0 0 / 36%)),
      0 24px 64px -12px light-dark(oklch(0% 0 0 / 28%), oklch(0% 0 0 / 56%));
    --assistant-message-shadow: 0 1px 2px light-dark(oklch(0% 0 0 / 6%), oklch(0% 0 0 / 18%));
    --assistant-composer-shadow:
      0 2px 6px -2px light-dark(oklch(0% 0 0 / 12%), oklch(0% 0 0 / 26%)),
      0 6px 16px -6px light-dark(oklch(0% 0 0 / 12%), oklch(0% 0 0 / 32%));
    right: 1rem;
    bottom: 1rem;
    width: min(28rem, calc(100vw - 1rem));
    height: min(42rem, calc(100dvh - 6rem));
    isolation: isolate;
  }
  @media (max-width: 639px) {
    .assistant-panel {
      right: 0.5rem;
      bottom: calc(var(--ks-dock-h) + 0.5rem);
      height: calc(100dvh - 8rem);
    }
  }
  .assistant-panel.compact {
    bottom: 0.5rem;
    height: calc(100dvh - 1rem);
  }
  .assistant-panel.compact .transcript {
    padding-block: 0.25rem 0.5rem;
  }
</style>
