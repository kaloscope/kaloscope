<script lang="ts">
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { assistant, sendAssistant } from '$lib/assistant/session';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores.svelte';
  import type { QuestionAnswer, QuestionCard, Resp } from '$lib/types';
  import { onMount, tick, untrack } from 'svelte';

  let {
    card,
    callId,
    conversationId,
    oncard,
    onclose
  }: {
    card: QuestionCard;
    callId: string;
    conversationId: number;
    oncard: (card: QuestionCard) => void;
    onclose: () => void;
  } = $props();

  let index = $state(0);
  let answers = $state<QuestionAnswer[]>([]);
  let ready = $state(false);
  let submitting = $state(false);
  let issue = $state('');
  let legend: HTMLLegendElement | undefined = $state();
  let heading = $derived(card.questions[index]);
  let answer = $derived(answers[index]);
  const hasCustom = $derived(!!answer?.custom.trim());
  const lastQuestion = $derived(index === answers.length - 1);
  const key = $derived(`assistant-questions:${user.current?.id}:${conversationId}:${callId}:${card.version}`);

  onMount(() => {
    answers =
      card.answers?.map((v) => ({ ...v, selected: [...v.selected] })) ??
      card.questions.map(({ id }) => ({ id, selected: [], custom: '', skipped: false }));
    try {
      const stored = JSON.parse(sessionStorage.getItem(key) ?? 'null');
      if (
        stored &&
        card.state !== 'answered' &&
        Array.isArray(stored.answers) &&
        stored.answers.length === answers.length &&
        stored.answers.every(
          (v: QuestionAnswer, i: number) =>
            v?.id === answers[i].id &&
            typeof v.custom === 'string' &&
            Array.isArray(v.selected) &&
            v.selected.every((id) => typeof id === 'string') &&
            typeof v.skipped === 'boolean'
        )
      ) {
        answers = stored.answers;
        index = Math.min(Math.max(0, Number(stored.index) || 0), answers.length - 1);
      }
    } catch {
      /* storage may be unavailable */
    }
    ready = true;
  });

  $effect(() => {
    if (!ready) return;
    const draft = JSON.stringify({ index, answers });
    try {
      if (card.state === 'answered') sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, draft);
    } catch {
      /* keep the current draft in memory */
    }
  });

  /**
   * Move between questions while keeping selections local to the card.
   * @param next - Zero-based position to display.
   */
  async function move(next: number) {
    index = next;
    issue = '';
    await tick();
    legend?.focus();
  }

  /**
   * Select a listed value without submitting the question.
   * @param id - Immutable option identity.
   * @param checked - Whether the option is currently selected.
   */
  function choose(id: string, checked: boolean) {
    answer.selected = heading.multiple
      ? checked
        ? [...answer.selected, id]
        : answer.selected.filter((v) => v !== id)
      : [id];
    if (!heading.multiple) answer.custom = '';
    answer.skipped = false;
    issue = '';
  }

  /**
   * Save a draft answer or submit the completed group to its owning conversation.
   * @param skip - Whether to leave this question unanswered.
   */
  async function advance(skip: boolean) {
    if (!ready || submitting || $assistant.busy) return;
    if (card.state === 'answered') {
      await sendAssistant(page.url.pathname, undefined, $_('assistant.question.submitted'), callId);
      return;
    }
    if (skip) {
      answer.custom = '';
      answer.selected = [];
      answer.skipped = true;
    } else if (!answer.skipped && !answer.selected.length && !answer.custom.trim()) {
      issue = 'empty';
      return;
    }
    if (index < answers.length - 1) {
      await move(index + 1);
      return;
    }
    submitting = true;
    issue = '';
    try {
      const { data } = await api
        .post(`assistant/conversations/${conversationId}/questions/${callId}/answer`, {
          json: { version: card.version, items: answers },
          retry: 0,
          context: { silentErrors: true }
        })
        .json<Resp<{ card?: QuestionCard; issue?: string; id?: string }>>();
      if ($assistant.conversation?.id !== conversationId) return;
      if (!data.card) {
        const at = card.questions.findIndex((q) => q.id === data.id);
        if (at >= 0) await move(at);
        issue = data.issue ?? 'invalid';
        return;
      }
      oncard(data.card);
      if (data.card.state === 'answered') {
        await sendAssistant(page.url.pathname, undefined, $_('assistant.question.submitted'), callId);
      } else {
        await move(
          Math.max(
            0,
            card.questions.findIndex((q, i) => q.required && answers[i].skipped)
          )
        );
        issue = 'deferred';
      }
    } catch {
      issue = 'failed';
    } finally {
      submitting = false;
    }
  }

  $effect(() => {
    if (ready) untrack(() => legend?.focus({ preventScroll: true }));
  });
</script>

{#if ready && heading && answer}
  <section class="flex max-h-[60dvh] flex-col px-2.5 pt-2.5 pb-1">
    <div class="min-h-0 overflow-y-auto">
      <fieldset
        disabled={submitting || $assistant.busy || card.state === 'answered'}
        class="min-w-0"
        aria-describedby={card.subscription ? `subscription-${callId}` : undefined}
      >
        <legend bind:this={legend} tabindex="-1" class="mb-1 w-full px-1 outline-none">
          <span class="flex items-baseline justify-between gap-2">
            <span class="min-w-0 text-sm leading-snug font-semibold wrap-anywhere">
              {heading.title || $_('assistant.question.plan_title')}
            </span>
            <span class="shrink-0 text-end text-xs font-normal whitespace-nowrap text-base-content/65 tabular-nums">
              {$_('assistant.question.progress', [index + 1, answers.length])} · {$_(
                heading.required ? 'assistant.question.required' : 'assistant.question.optional'
              )}
            </span>
          </span>
        </legend>
        {#if card.subscription}
          {@const plan = card.subscription.plan}
          <p id={`subscription-${callId}`} class="mb-1 px-1 text-xs text-base-content/70">
            {$_('assistant.question.subscription_settings', [
              card.subscription.workflow_name,
              plan.interval_num,
              plan.batch_limit ?? $_('assistant.replacement.no_limit'),
              plan.total_limit ?? $_('assistant.replacement.no_limit')
            ])}
          </p>
        {/if}
        {#each heading.options as option (option.id)}
          {@const selected = answer.selected.includes(option.id)}
          <label
            class="flex min-h-8 cursor-pointer items-center gap-2 rounded-lg px-2 py-1 has-focus-visible:outline-2 has-focus-visible:-outline-offset-2 has-focus-visible:outline-primary {hasCustom
              ? 'text-base-content/65 hover:bg-base-200 has-checked:bg-base-content/5'
              : 'hover:bg-base-200 has-checked:bg-primary/8'}"
          >
            <input
              type={heading.multiple ? 'checkbox' : 'radio'}
              name={`${callId}-${heading.id}`}
              checked={selected}
              class="shrink-0 {heading.multiple ? 'checkbox checkbox-xs' : 'radio radio-xs'}"
              class:checkbox-primary={selected && heading.multiple && !hasCustom}
              class:radio-primary={selected && !heading.multiple && !hasCustom}
              onchange={(e) => choose(option.id, e.currentTarget.checked)}
            />
            <span class="min-w-0 text-start text-sm leading-snug wrap-anywhere">
              <span class="font-medium">{option.label}</span>{#if option.detail}<span
                  class="ms-2 text-xs {hasCustom ? 'text-base-content/60' : 'text-base-content/70'}"
                  >{option.detail}</span
                >{/if}
            </span>
          </label>
        {/each}
        <label
          class="mt-1 flex min-h-9 items-center gap-2 rounded-lg border px-2.5 focus-within:border-primary focus-within:outline-1 focus-within:-outline-offset-1 focus-within:outline-primary {hasCustom
            ? 'border-primary/55 bg-primary/5 text-primary'
            : 'border-border text-base-content/65'}"
        >
          <iconify-icon icon={icons.edit} width="0.875rem" class="size-3.5 shrink-0" aria-hidden="true"></iconify-icon>
          <span class="sr-only">{$_('assistant.question.custom')}</span>
          <input
            id={`custom-${callId}`}
            class="min-w-0 flex-1 bg-transparent text-base text-base-content outline-none placeholder:text-sm placeholder:text-base-content/60"
            maxlength="512"
            value={answer.custom}
            aria-invalid={!!issue && issue !== 'deferred'}
            aria-describedby={issue ? `issue-${callId}` : undefined}
            placeholder={heading.format === 'datetime'
              ? $_('assistant.question.time_hint')
              : $_('assistant.question.custom')}
            oninput={(event) => {
              answer.custom = event.currentTarget.value;
              if (!heading.multiple && answer.custom.trim()) answer.selected = [];
              answer.skipped = false;
              issue = '';
            }}
          />
        </label>
      </fieldset>
    </div>
    {#if issue}
      <p id={`issue-${callId}`} class="mt-1 px-1 text-xs" role="status">{$_(`assistant.question.${issue}`)}</p>
    {/if}
    <div class="mt-2 flex shrink-0 flex-wrap items-center justify-between gap-x-1 gap-y-1.5">
      <div class="flex items-center gap-0.5">
        <button
          class="btn gap-1 rounded-full btn-ghost px-2 font-medium text-base-content/75 btn-sm"
          type="button"
          disabled={submitting || $assistant.busy}
          onclick={onclose}
        >
          <iconify-icon icon={icons.arrowHookUpLeft} width="0.875rem" class="size-3.5" aria-hidden="true"
          ></iconify-icon>
          {$_('assistant.question.back_to_chat')}
        </button>
        {#if index > 0 && card.state !== 'answered'}
          <button
            class="btn gap-0.5 rounded-full btn-ghost px-2 font-medium text-base-content/75 btn-sm"
            type="button"
            disabled={submitting || $assistant.busy}
            onclick={() => move(index - 1)}
          >
            <iconify-icon icon={icons.chevronRight} width="0.875rem" class="size-3.5 rotate-180" aria-hidden="true"
            ></iconify-icon>
            {$_('assistant.question.back')}
          </button>
        {/if}
      </div>
      <div class="ms-auto flex items-center gap-1.5">
        {#if card.state !== 'answered'}
          <button
            class="btn rounded-full border-border bg-base-100 px-3 font-medium shadow-none btn-sm hover:bg-base-200"
            type="button"
            disabled={submitting || $assistant.busy}
            onclick={() => advance(true)}>{$_('assistant.question.skip')}</button
          >
        {/if}
        <button
          class="btn gap-1 rounded-full px-3 font-semibold shadow-none btn-neutral btn-sm"
          type="button"
          disabled={submitting || $assistant.busy}
          onclick={() => advance(false)}
        >
          {$_(
            card.state === 'answered'
              ? 'assistant.question.continue'
              : lastQuestion
                ? card.subscription
                  ? 'assistant.question.confirm_subscription'
                  : 'assistant.question.submit'
                : 'assistant.question.next'
          )}
          <iconify-icon
            icon={lastQuestion && card.state !== 'answered' ? icons.checkmark : icons.arrowRight}
            width="0.875rem"
            class="size-3.5"
            aria-hidden="true"
          ></iconify-icon>
        </button>
      </div>
    </div>
  </section>
{/if}
