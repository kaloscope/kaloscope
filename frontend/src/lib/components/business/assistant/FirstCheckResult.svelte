<script lang="ts">
  import { api } from '$lib/api';
  import { _ } from '$lib/i18n';
  import { user } from '$lib/stores.svelte';
  import type { DownloadPlanCreation, FirstPlanExecution, Resp } from '$lib/types';
  import { describePlanExecution } from '$lib/utils/plan-execution';
  import { isHTTPError } from 'ky';

  const { planId }: { planId: number } = $props();
  let first: FirstPlanExecution | null = $state(null);
  let readError = $state(false);

  $effect(() => {
    const id = planId;
    const owner = user.current?.id;
    first = null;
    readError = false;
    if (!owner || user.current?.role !== 'admin') return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let delay = 1000;

    /**
     * Follow the first receipt through slow searches and transient read failures.
     * @returns A promise resolving after this read or scheduling its successor.
     */
    async function refresh(): Promise<void> {
      if (!document.hidden) {
        try {
          const { data } = await api
            .get(`download/plan/${id}`, { signal: controller.signal, retry: 0, context: { silentErrors: true } })
            .json<Resp<DownloadPlanCreation>>();
          if (controller.signal.aborted || user.current?.id !== owner) return;
          first = data.first_execution;
          readError = false;
          if (!['waiting', 'queued', 'scheduled'].includes(first.status) && first.stage !== 'checking') return;
        } catch (error) {
          if (controller.signal.aborted) return;
          readError = true;
          if (isHTTPError(error) && [401, 403, 404].includes(error.response.status)) return;
        }
      }
      timer = setTimeout(refresh, delay);
      delay = Math.min(delay * 2, 30000);
    }

    void refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  });
</script>

<p class="text-sm leading-6" role="status">
  {#if readError}{$_('assistant.plan.execution.read_error')}
  {:else if first}{$_('assistant.plan.first_report', describePlanExecution(first, $_))}{/if}
</p>
