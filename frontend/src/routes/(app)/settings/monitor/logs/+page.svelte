<script lang="ts">
  import { api } from '$lib/api';
  import { Container, confirm } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { SystemLog, SystemLogLevel, SystemLogState } from '$lib/types';
  import { onMount, tick } from 'svelte';

  type ConnectionStatus = 'connecting' | 'connected' | 'reconnecting';
  type LogAction = 'pause' | 'resume' | 'clear';

  const MAX_LOGS = 500;
  const FOLLOW_THRESHOLD_PX = 16;
  const LEVELS: SystemLogLevel[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

  let logs: SystemLog[] = $state([]);
  let enabledLevels: SystemLogLevel[] = $state([...LEVELS]);
  let connectionStatus: ConnectionStatus = $state('connecting');
  let paused: boolean | null = $state(null);
  let runId: string | null = $state(null);
  let bufferId: number | null = $state(null);
  let following = $state(true);
  let terminal: HTMLDivElement;
  let logQueue: SystemLog[] = [];
  let flushFrame: number | null = null;

  const actionLoading = createLoading();

  let filteredLogs = $derived(logs.filter((log) => enabledLevels.includes(log.level)));
  let toggleAction: LogAction = $derived(paused ? 'resume' : 'pause');

  /**
   * Parse the JSON payload from an SSE message.
   *
   * @param event - The SSE message to parse.
   * @returns The parsed value, or `null` for malformed JSON.
   */
  function parseEvent(event: MessageEvent): unknown {
    try {
      return JSON.parse(event.data);
    } catch {
      return null;
    }
  }

  /**
   * Validate an SSE payload as a `SystemLog`.
   *
   * @param value - The payload to validate.
   * @returns Whether the payload is a system log.
   */
  function isSystemLog(value: unknown): value is SystemLog {
    if (typeof value !== 'object' || value === null) {
      return false;
    }
    const log = value as Record<string, unknown>;
    return (
      typeof log.id === 'number' &&
      typeof log.level === 'string' &&
      LEVELS.includes(log.level as SystemLogLevel) &&
      typeof log.logger === 'string' &&
      typeof log.message === 'string' &&
      typeof log.process_id === 'number' &&
      typeof log.process_name === 'string' &&
      typeof log.created === 'number'
    );
  }

  /**
   * Validate an SSE payload as a `SystemLogState`.
   *
   * @param value - The payload to validate.
   * @returns Whether the payload is a log state.
   */
  function isLogState(value: unknown): value is SystemLogState {
    if (typeof value !== 'object' || value === null) {
      return false;
    }
    const state = value as Record<string, unknown>;
    return (
      typeof state.paused === 'boolean' &&
      typeof state.run_id === 'string' &&
      state.run_id.length > 0 &&
      typeof state.buffer_id === 'number' &&
      Number.isSafeInteger(state.buffer_id) &&
      state.buffer_id >= 0
    );
  }

  /**
   * Queue a log for the next animation frame.
   *
   * @param log - The log to queue.
   */
  function enqueueLog(log: SystemLog) {
    logQueue.push(log);
    if (flushFrame === null) {
      flushFrame = requestAnimationFrame(flushLogQueue);
    }
  }

  /**
   * Flush queued logs in one reactive update.
   */
  function flushLogQueue() {
    const batch = logQueue;
    logQueue = [];
    flushFrame = null;

    // discard records that precede the latest ID reset
    const resetIndex = batch.findLastIndex((log, index) => {
      const previousId = index === 0 ? logs.at(-1)?.id : batch[index - 1]?.id;
      return previousId !== undefined && log.id < previousId;
    });
    const nextLogs = resetIndex < 0 ? [...logs, ...batch] : batch.slice(resetIndex);
    logs = nextLogs.slice(-MAX_LOGS);
    if (following) {
      scrollToEnd();
    }
  }

  /**
   * Clear queued logs and cancel the pending frame.
   */
  function clearLogQueue() {
    logQueue = [];
    if (flushFrame !== null) {
      cancelAnimationFrame(flushFrame);
      flushFrame = null;
    }
  }

  /**
   * Run a shared log-buffer action.
   *
   * @param action - The action to run.
   */
  async function runAction(action: LogAction) {
    // block duplicates before delayed loading feedback appears
    if ($actionLoading !== null) {
      return;
    }
    actionLoading.start();
    try {
      await api.post(`monitor/logs/${action}`);
    } catch {
      // request failures need no page-specific feedback
    } finally {
      actionLoading.end();
    }
  }

  /**
   * Open the clear-log confirmation dialog.
   */
  function confirmClear() {
    if ($actionLoading !== null) {
      return;
    }
    confirm({
      icon: icons.clear,
      title: $_('message.clear.title'),
      message: $_('logstream.clear_confirm'),
      onconfirm: () => runAction('clear')
    });
  }

  /**
   * Set whether a log level is visible.
   *
   * @param level - The level to update.
   * @param enabled - Whether to show the level.
   */
  function setLevel(level: SystemLogLevel, enabled: boolean) {
    enabledLevels = enabled ? [...enabledLevels, level] : enabledLevels.filter((value) => value !== level);
    if (following) {
      scrollToEnd();
    }
  }

  /**
   * Update follow mode from the terminal scroll position.
   */
  function handleScroll() {
    const distance = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight;
    following = distance <= FOLLOW_THRESHOLD_PX;
  }

  /**
   * Scroll to the latest rendered log.
   */
  async function scrollToEnd() {
    await tick();
    terminal?.scrollTo({ top: terminal.scrollHeight });
  }

  /**
   * Shorten a Sanic process name for display.
   *
   * @param processName - The full process name.
   * @returns The compact process name.
   */
  function formatProcess(processName: string): string {
    const worker = /^Sanic-Server-(\d+)(?:-\d+)?$/.exec(processName);
    if (worker) {
      return `Srv ${worker[1]}`;
    }
    return processName === 'MainProcess' ? 'Main' : processName;
  }

  /**
   * Format a Unix timestamp as local time with millisecond precision.
   *
   * @param created - The Unix timestamp in seconds.
   * @returns The formatted local timestamp.
   */
  function formatTime(created: number): string {
    const date = new Date(created * 1000);
    const pad = (value: number, length = 2) => String(value).padStart(length, '0');
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
  }

  onMount(() => {
    const eventSource = new EventSource('/_api/monitor/logs');
    eventSource.onopen = () => {
      connectionStatus = 'connected';
    };
    eventSource.onerror = () => {
      connectionStatus = 'reconnecting';
    };
    eventSource.addEventListener('state', (event) => {
      const state = parseEvent(event);
      if (!isLogState(state)) {
        return;
      }
      // clear stale rows when the backend or buffer changes
      if ((runId !== null && state.run_id !== runId) || (bufferId !== null && state.buffer_id !== bufferId)) {
        clearLogQueue();
        logs = [];
      }
      paused = state.paused;
      runId = state.run_id;
      bufferId = state.buffer_id;
    });
    eventSource.onmessage = (event) => {
      const log = parseEvent(event);
      if (isSystemLog(log)) {
        enqueueLog(log);
      }
    };
    return () => {
      clearLogQueue();
      eventSource.close();
    };
  });
</script>

<Container dvh padding="0.5rem" class="lg:px-2">
  <div class="log-shell flex min-h-0 flex-1 flex-col overflow-hidden rounded-box border shadow-sm">
    <div class="log-toolbar flex flex-wrap items-center border-b">
      <span class="connection-status inline-flex items-center rounded font-mono">
        <span class="connection-dot rounded-full" data-state={connectionStatus}></span>
        {$_(`logstream.connection.${connectionStatus}`)}
      </span>

      <span class="toolbar-divider -ml-1.5!"></span>

      <fieldset class="flex flex-wrap items-center gap-0.75">
        <legend class="sr-only">{$_('logstream.levels')}</legend>
        {#each LEVELS as level (level)}
          <label class="flex cursor-pointer">
            <input
              type="checkbox"
              class="sr-only"
              checked={enabledLevels.includes(level)}
              onchange={(event) => setLevel(level, event.currentTarget.checked)}
            />
            <span
              class="level-chip inline-flex items-center rounded-field font-mono font-semibold"
              data-level={level}
              data-enabled={enabledLevels.includes(level)}
            >
              {level}
            </span>
          </label>
        {/each}
      </fieldset>

      <div class="ml-auto flex items-center gap-1">
        <button
          type="button"
          class="toolbar-action inline-flex items-center rounded-field"
          onclick={() => runAction(toggleAction)}
          disabled={$actionLoading === true || paused === null}
        >
          <iconify-icon icon={paused ? icons.play : icons.pause} width="1rem"></iconify-icon>
          {$_(`action.${toggleAction}`)}
        </button>
        <button
          type="button"
          class="toolbar-action inline-flex items-center rounded-field"
          onclick={confirmClear}
          disabled={$actionLoading === true}
        >
          <iconify-icon icon={icons.clear} width="1rem"></iconify-icon>
          {$_('action.clear')}
        </button>
        <button
          type="button"
          class="toolbar-action inline-flex items-center rounded-field"
          onclick={() => {
            following = true;
            scrollToEnd();
          }}
          disabled={following}
        >
          <iconify-icon icon={icons.goEnd} class="rotate-90" width="1rem"></iconify-icon>
          {$_('logstream.follow')}
        </button>
      </div>
    </div>

    <div
      bind:this={terminal}
      onscroll={handleScroll}
      class="log-terminal min-h-0 flex-1 overflow-auto overscroll-none font-mono"
    >
      {#if filteredLogs.length > 0}
        <ul class="log-list min-w-176 py-1">
          {#each filteredLogs as log, index (index)}
            <li class="log-row flex items-start">
              <span class="log-process shrink-0 truncate" title={`${log.process_name} [${log.process_id}]`}>
                {formatProcess(log.process_name)}
              </span>
              <time class="log-time shrink-0">{formatTime(log.created)}</time>
              <span class="log-level shrink-0 font-semibold" data-level={log.level}>{log.level}:</span>
              <pre class="log-message"><span class="log-logger">[{log.logger}]</span> {log.message}</pre>
            </li>
          {/each}
        </ul>
      {:else}
        <div class="log-empty grid h-full min-h-40 place-items-center p-4 text-xs">
          {$_('logstream.empty')}
        </div>
      {/if}
    </div>
  </div>
</Container>

<style>
  .log-shell {
    border-color: #3c3c3c;
    background: #1e1e1e;
    color: #d4d4d4;
  }

  .log-toolbar {
    gap: 0.375rem;
    border-color: #3c3c3c;
    background: #252526;
    padding: 0.375rem 0.5rem;
  }

  .connection-status,
  .level-chip,
  .toolbar-action {
    padding: 0 0.5rem;
    font-size: 0.6875rem;
    line-height: 1;
  }

  .connection-status,
  .level-chip {
    height: 1.5rem;
  }

  .connection-status {
    gap: 0.375rem;
    color: #cccccc;
  }

  .connection-dot {
    width: 0.375rem;
    height: 0.375rem;
    background: #cca700;
  }

  .connection-dot[data-state='connected'] {
    background: #89d185;
  }

  .connection-dot[data-state='reconnecting'] {
    background: #f44747;
  }

  .toolbar-divider {
    align-self: center;
    width: 1px;
    height: 1rem;
    margin: 0 0.125rem;
    background: #4b4b4b;
  }

  .level-chip {
    color: #858585;
  }

  .level-chip,
  .toolbar-action {
    transition:
      color 120ms ease,
      background-color 120ms ease;
  }

  .level-chip[data-enabled='true'] {
    background: #3a3d41;
  }

  .level-chip[data-enabled='false']:hover {
    color: #cccccc;
    background: #2a2d2e;
  }

  .level-chip[data-level='DEBUG'][data-enabled='true'],
  .log-level[data-level='DEBUG'] {
    color: #569cd6;
  }

  .level-chip[data-level='INFO'][data-enabled='true'] {
    color: #d4d4d4;
  }

  .level-chip[data-level='WARNING'][data-enabled='true'],
  .log-level[data-level='WARNING'] {
    color: #cca700;
  }

  .level-chip[data-level='ERROR'][data-enabled='true'],
  .log-level[data-level='ERROR'] {
    color: #f44747;
  }

  .level-chip[data-level='CRITICAL'][data-enabled='true'],
  .log-level[data-level='CRITICAL'] {
    color: #ff6b6b;
  }

  .toolbar-action {
    height: 1.75rem;
    gap: 0.25rem;
    color: #cccccc;
    cursor: pointer;
  }

  .toolbar-action:hover:not(:disabled) {
    color: #ffffff;
    background: #3a3d41;
  }

  .toolbar-action:disabled {
    cursor: default;
    opacity: 0.3;
  }

  .log-terminal {
    font-size: 0.75rem;
    line-height: 1.45;
    scrollbar-color: #424242 #1e1e1e;
    scrollbar-width: thin;
  }

  .log-row {
    row-gap: 0;
    column-gap: 0.5rem;
    padding: 1px 0.5rem;
  }

  .log-row:hover {
    background: #2a2d2e;
  }

  .log-process {
    width: 3rem;
  }

  .log-time {
    width: 6rem;
  }

  .log-level {
    width: 4.5rem;
  }

  .log-message {
    min-width: 0;
    flex: 1;
    white-space: pre-wrap;
    overflow-wrap: break-word;
  }

  .log-logger {
    color: #4ec9b0;
  }

  .log-process,
  .log-time,
  .log-empty {
    color: #858585;
  }

  .log-terminal::-webkit-scrollbar {
    width: 10px;
    height: 10px;
  }

  .log-terminal::-webkit-scrollbar-track {
    background: #1e1e1e;
  }

  .log-terminal::-webkit-scrollbar-thumb {
    background: #424242;
  }

  .log-terminal::-webkit-scrollbar-thumb:hover {
    background: #4f4f4f;
  }

  @media (max-width: 40rem) {
    .level-chip {
      font-size: 0.5rem;
      height: 1.25rem;
    }

    .log-list {
      min-width: 0;
    }

    .log-row {
      flex-wrap: wrap;
    }

    .log-row:not(:last-child) {
      margin-bottom: 0.25rem;
    }

    .log-message {
      width: 100%;
      flex: none;
    }
  }
</style>
