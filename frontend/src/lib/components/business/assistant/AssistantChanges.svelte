<script lang="ts">
  import { assistantCanvas, discardAssistantCanvas, keepAssistantCanvas } from '$lib/assistant/canvas';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
</script>

{#if $assistantCanvas}
  {@const state = $assistantCanvas}
  <section class="rounded-box border bg-base-100 p-3 text-sm" aria-label={$_('assistant.canvas.title')}>
    <div class="flex items-center gap-2" role="status">
      {#if state.phase === 'editing' || state.phase === 'saving'}
        <span class="loading loading-sm loading-spinner" aria-hidden="true"></span>
      {:else}
        <iconify-icon icon={icons.documentEdit} width="1rem" aria-hidden="true"></iconify-icon>
      {/if}
      <span class="font-semibold">{$_(`assistant.canvas.${state.phase}`)}</span>
    </div>
    <div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-base-content/70">
      {#if state.phase !== 'editing'}
        <span class="inline-flex items-center gap-1.5">
          <iconify-icon icon={icons.flowchart} width="1rem" aria-hidden="true"></iconify-icon>
          {$_('assistant.canvas.changes', state.changes)}
        </span>
      {/if}
      <span class="inline-flex items-center gap-1.5">
        <iconify-icon icon={icons.lockClosed} width="1rem" aria-hidden="true"></iconify-icon>
        {$_('assistant.canvas.readonly')}
      </span>
      {#if state.issues}
        <span class="validation-note inline-flex items-center gap-1.5">
          <iconify-icon icon={icons.warning} width="1rem" aria-hidden="true"></iconify-icon>
          {$_('assistant.canvas.issues', state.issues)}
        </span>
      {/if}
    </div>
    {#if state.phase !== 'editing'}
      {#if state.error}
        <p class="mt-2 text-error" role="alert">
          {$_(`assistant.errors.${state.error}`, {
            default: $_(`alert.${state.error}`, { default: $_('assistant.errors.assistant_canvas_save_failed') })
          })}
        </p>
      {/if}
      <div class="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-3">
        {#if state.phase === 'review' && !state.uncertain}
          <span class="text-xs text-base-content/70">{$_('assistant.canvas.unsaved')}</span>
        {/if}
        <div class="ml-auto flex gap-2">
          <button
            class="btn btn-ghost btn-sm"
            disabled={state.phase === 'saving' || state.uncertain}
            onclick={discardAssistantCanvas}>{$_('assistant.canvas.revert')}</button
          >
          <button class="btn btn-primary btn-sm" disabled={state.phase === 'saving'} onclick={keepAssistantCanvas}>
            {$_(state.uncertain ? 'assistant.canvas.check_save' : 'assistant.canvas.keep')}
          </button>
        </div>
      </div>
    {:else}
      <div class="mt-2 flex justify-end">
        <button class="btn btn-ghost btn-sm" onclick={discardAssistantCanvas}>{$_('assistant.stop')}</button>
      </div>
    {/if}
  </section>
{/if}

<style>
  .validation-note {
    color: color-mix(in oklab, var(--color-warning) 40%, var(--color-base-content));
  }
</style>
