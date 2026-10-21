<script lang="ts">
  import { afterNavigate, beforeNavigate, goto } from '$app/navigation';
  import { page } from '$app/state';
  import { tooltip } from '$lib/actions';
  import { assistantDestination } from '$lib/assistant/navigation';
  import {
    assistant,
    closeAssistant,
    newAssistantConversation,
    sendAssistant,
    stopAssistant,
    toggleAssistant,
    type AssistantTool as Tool,
    type AssistantTurn
  } from '$lib/assistant/session';
  import { Dropdown, Select } from '$lib/components';
  import { closeDropdowns } from '$lib/components/common/interaction/Dropdown.svelte';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { AssistantDestination, AssistantPageContext } from '$lib/types';
  import { normalizePathname } from '$lib/utils/url';
  import { onMount, tick, untrack } from 'svelte';
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
  let viewportHeight: number | undefined = $state();
  let position: { x: number; y: number } | null = $state(null);
  let drag: { x: number; y: number; left: number; top: number } | null = null;
  const turns = $derived($assistant.turns);
  const input = $derived($assistant.input);
  const lastTurn = $derived(turns.at(-1));
  const isOpen = $derived($assistant.open);
  const compact = $derived(viewportHeight !== undefined && viewportHeight < 540);
  const rows = $derived(compact ? 1 : 2);
  const modelsBusy = $derived($assistant.modelsLoading !== null);
  const canSend = $derived(!modelsBusy && !!input.trim() && !!$assistant.model);
  const modelValue = $derived($assistant.model ? JSON.stringify($assistant.model) : null);
  const currentPage = $derived(
    /\/graphs\/(r\/)?\d+\/?$/.test(page.url.pathname)
      ? 'workflow_designer'
      : /\/workflows\/graphs\/?$/.test(page.url.pathname)
        ? 'workflows'
        : 'other'
  );
  const incomplete = $derived(!!lastTurn && lastTurn.status !== 'complete' && lastTurn.status !== 'streaming');
  const destinations = $derived.by(() => {
    const items: { path: string; label: string }[] = [];
    const pathname = normalizePathname(page.url.pathname);
    for (const { result } of lastTurn?.tools ?? []) {
      const path = result && assistantDestination(result);
      if (!path || path === pathname || items.some((item) => item.path === path)) continue;
      const target = result.data!.target as AssistantDestination;
      const name = $_(`assistant.pages.${target.page}`);
      items.push({ path, label: $_('action.open', target.resource_id ? `${name} #${target.resource_id}` : name) });
    }
    return items;
  });

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
   * Provide arrow-key positioning for users who cannot drag a pointer.
   *
   * @param event - Key press on the move handle.
   */
  function moveWithKeyboard(event: KeyboardEvent): void {
    if (!panel || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home'].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === 'Home') {
      position = null;
      return;
    }
    const rect = panel.getBoundingClientRect();
    const step = event.shiftKey ? 40 : 10;
    move(
      rect.left + (event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0),
      rect.top + (event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0)
    );
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
    } else if (event.key === 'Escape' || (event.altKey && event.code === 'KeyA')) {
      event.preventDefault();
      closeAssistant();
    } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault();
      void sendAssistant(page.url.pathname);
    }
  }

  /**
   * Toggle the global assistant with its documented keyboard shortcut.
   *
   * @param event - Application-wide key press.
   */
  function globalKeydown(event: KeyboardEvent): void {
    if (
      event.altKey &&
      event.code === 'KeyA' &&
      !event.ctrlKey &&
      !event.metaKey &&
      !document.querySelector('dialog[open]')
    ) {
      event.preventDefault();
      toggleAssistant();
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
   * Replace an incomplete reply with a new attempt using the current page.
   *
   * @param turn - Incomplete turn whose prompt is sent again.
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
   * Describe the page snapshot submitted with a turn.
   *
   * @param context - Snapshot captured when the turn was sent.
   * @returns A short localized label with the resource and draft state.
   */
  function contextLabel(context: AssistantPageContext): string {
    const parts = [$_(`assistant.pages.${context.page_type}`)];
    if (context.page_type === 'workflow_designer') {
      parts.push(
        `#${context.resource_id}`,
        `v${context.draft_version}`,
        $_(context.unsaved ? 'assistant.unsaved' : 'assistant.saved')
      );
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
  afterNavigate(() => {
    if ($assistant.open && restoreAfterNavigation) composer?.focus({ preventScroll: true });
    restoreAfterNavigation = false;
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
          composer?.focus({ preventScroll: true });
        }
      });
    } else {
      untrack(() => {
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

<svelte:window onkeydown={globalKeydown} onresize={resize} />

{#if $assistant.open}
  <div
    id="assistant-panel"
    role="dialog"
    aria-modal="false"
    aria-labelledby="assistant-title"
    aria-describedby="assistant-help"
    tabindex="-1"
    bind:this={panel}
    class="assistant-panel @container fixed layer-1 flex flex-col rounded-box bg-blur-90 text-base-content shadow-2xl ring-1 ring-border"
    class:compact
    style:max-height={viewportHeight ? `${Math.max(0, viewportHeight - 16)}px` : undefined}
    style:left={position ? `${position.x}px` : undefined}
    style:top={position ? `${position.y}px` : undefined}
    style:right={position ? 'auto' : undefined}
    style:bottom={position ? 'auto' : undefined}
    onkeydown={panelKeydown}
  >
    <h2 id="assistant-title" class="sr-only">{$_('assistant.title')}</h2>
    <p id="assistant-help" class="sr-only">{$_('assistant.keyboard')}</p>

    <div class="flex h-8 shrink-0 items-center px-1.5 pt-1">
      <!-- the left inset matches the close button so the grip stays centered on the panel -->
      <button
        bind:this={mover}
        type="button"
        class="group flex h-7 min-w-0 flex-1 cursor-move touch-none items-center justify-center rounded-field pl-6 focus-visible:outline-2 focus-visible:outline-primary"
        aria-label={$_('assistant.move')}
        aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown Home"
        onpointerdown={startDrag}
        onpointermove={dragPanel}
        onpointerup={() => (drag = null)}
        onpointercancel={() => (drag = null)}
        onlostpointercapture={() => (drag = null)}
        onkeydown={moveWithKeyboard}
      >
        <span
          class="h-1 w-9 rounded-full bg-base-content/20 transition-colors group-hover:bg-base-content/40 group-focus-visible:bg-primary/70"
          aria-hidden="true"
        ></span>
      </button>
      <button
        type="button"
        class="btn btn-square btn-subtle opacity-80 btn-xs"
        aria-label={$_('assistant.close')}
        onclick={closeAssistant}
      >
        <iconify-icon icon={icons.dismiss} width="1.125rem" aria-hidden="true"></iconify-icon>
      </button>
    </div>

    <ol
      bind:this={transcript}
      class="transcript min-h-0 flex-1 [scrollbar-gutter:stable] space-y-6 overflow-y-auto overscroll-contain px-4 pt-2 pb-3"
      aria-label={$_('assistant.conversation')}
      onscroll={trackScroll}
    >
      {#if !turns.length}
        <li class="flex min-h-full flex-col justify-center gap-2 pb-4">
          <iconify-icon icon={icons.botSquare} width="2rem" class="mb-1 size-8 text-base-content/60" aria-hidden="true"
          ></iconify-icon>
          <h3 class="text-base font-semibold">{$_('assistant.welcome')}</h3>
          <p class="text-xs leading-5 text-base-content/60">{$_('assistant.memory_only')}</p>
        </li>
      {/if}
      {#each turns as turn (turn.id)}
        <li class="space-y-3">
          <div class="flex flex-col items-end gap-1 pl-8">
            <p class="sr-only">{$_('assistant.you')}</p>
            <p
              class="max-w-full rounded-box bg-base-content/8 px-3 py-2 text-sm leading-6 break-words whitespace-pre-wrap"
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
                    <li><AssistantTool {tool} /></li>
                  {/each}
                </ul>
              {:else}
                <AssistantMarkdown source={segment.text} />
              {/if}
            {/each}
            {#if turn.status === 'streaming'}
              <p class="flex h-6 items-center gap-2 text-xs text-base-content/60">
                <span class="loading loading-xs loading-dots" aria-hidden="true"></span>
                {$_('assistant.generating')}
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
            {/if}
          </div>
        </li>
      {/each}
    </ol>

    <footer class="relative shrink-0 rounded-b-box px-2 pb-2">
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

      {#if lastTurn && (incomplete || destinations.length)}
        <section
          class="mb-1.5 flex flex-wrap items-center gap-x-1 gap-y-0.5 text-xs"
          aria-label={$_('assistant.turn_actions')}
        >
          {#if incomplete}
            <button
              type="button"
              class="btn gap-1 btn-subtle font-normal btn-xs"
              disabled={$assistant.busy}
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

      <div class="rounded-box bg-base-100 ring-1 ring-border transition-shadow has-[textarea:focus]:ring-primary/80">
        <label class="sr-only" for="assistant-input">{$_('assistant.input')}</label>
        <textarea
          id="assistant-input"
          bind:this={composer}
          bind:value={$assistant.input}
          {rows}
          maxlength="32768"
          class="block w-full resize-none overflow-y-auto bg-transparent px-3 pt-2.5 pb-1 text-sm leading-6 outline-none placeholder:text-base-content/60"
          class:max-h-40={!compact}
          class:max-h-20={compact}
          placeholder={$_('assistant.placeholder')}
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
          <div class="relative flex min-w-0 shrink-[2] items-center" aria-busy={modelsBusy}>
            <label class="sr-only" for="assistant-model">{$_('assistant.model')}</label>
            <Select
              id="assistant-model"
              class="[field-sizing:content] h-7 min-h-7 w-auto max-w-full min-w-0 select-ghost pr-7 pl-2 text-xs select-sm disabled:border-transparent disabled:bg-transparent disabled:text-base-content/60"
              disabled={$assistant.busy || modelsBusy || !$assistant.models.length}
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
            {#if $assistant.modelsLoading}
              <span
                class="loading pointer-events-none absolute top-1/2 right-2 loading-xs -translate-y-1/2 loading-spinner text-base-content/60"
                aria-hidden="true"
              ></span>
            {/if}
          </div>
          <span class="flex-1"></span>
          {#if turns.length || $assistant.error || position}
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
                {#if turns.length || $assistant.error}
                  <li class:menu-disabled={$assistant.busy}>
                    <button
                      type="button"
                      disabled={$assistant.busy}
                      onclick={() => {
                        newAssistantConversation();
                        closeMenu(composer);
                      }}
                    >
                      <iconify-icon icon={icons.chatAdd} width="1rem" class="size-4" aria-hidden="true"></iconify-icon>
                      {$_('assistant.new_chat')}
                    </button>
                  </li>
                {/if}
                {#if position}
                  <li>
                    <button
                      type="button"
                      onclick={() => {
                        position = null;
                        closeMenu(mover);
                      }}
                    >
                      <iconify-icon icon={icons.arrowReset} width="1rem" class="size-4" aria-hidden="true"
                      ></iconify-icon>
                      {$_('assistant.restore_position')}
                    </button>
                  </li>
                {/if}
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
    </footer>
    <span class="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {$assistant.busy ? $_('assistant.generating') : lastTurn ? $_(`assistant.status.${lastTurn.status}`) : ''}
    </span>
  </div>
{/if}

<style>
  .assistant-panel {
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
