<script lang="ts">
  import { api } from '$lib/api';
  import { Button, CodeMirror, Modal, Overlay } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { FlowLog, Resp } from '$lib/types';
  import { json } from '@codemirror/lang-json';
  import type { LanguageSupport } from '@codemirror/language';

  // the loading state
  const loading = createLoading();
  // the groups of flow logs
  let groups: FlowLog[][] = $state([]);

  // the modal dialog instance
  let modal: Modal;
  export const showModal = (id: number) => {
    getLogs(id);
    modal.show();
  };

  // the document modal to show the log details
  let documentModal: Modal;
  let documentTitle: string = $state('');
  let documentLang: LanguageSupport | null = $state(null);
  let document: string = $state('');
  let refreshKey: number = $state(0);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const showDocModal = (doc: any, title: string) => {
    documentTitle = title;
    if (typeof doc === 'string') {
      documentLang = null;
      document = doc;
    } else {
      documentLang = json();
      document = JSON.stringify(doc, null, 2);
    }
    refreshKey = new Date().getTime();
    documentModal.show();
  };

  /**
   * Get the flow logs.
   *
   * @param graphId - The flow graph ID.
   */
  function getLogs(graphId: number) {
    groups = [];
    loading.start();
    api
      .get(`flow/graph/${graphId}/logs`)
      .json<Resp<FlowLog[][]>>()
      .then(({ data }) => {
        groups = data;
      })
      .finally(() => {
        loading.end();
      });
  }

  /**
   * Format the date to a string in the format of `MM/dd HH:mm:ss.SSS`.
   *
   * @param value - The date value to format.
   */
  function formatDate(value: number | string | Date): string {
    const date = new Date(value);
    const pad = (v: number, l: number = 2) => String(v).padStart(l, '0');
    return (
      `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`
    );
  }
</script>

<Modal icon={icons.documentSearch} title={$_('action.view', $_('entity.logs'))} bind:this={modal}>
  <div class="relative min-h-[50vh] overflow-y-auto rounded-box border">
    <Overlay loading={$loading} />
    {#if groups.length > 0}
      {#each groups as logs, i (i)}
        <ul class="list">
          {#each logs as log, j (j)}
            {@const end = log.type === 'end'}
            {@const start = log.type === 'start'}
            {@const title = $_(`flow.exec.${start ? 'bootparams' : end ? 'retval' : 'exc_info'}`)}

            <li class="list-row items-center gap-2 rounded-none p-2 opacity-80 hover:bg-base-200">
              <span class="w-2 text-base font-thin opacity-50">{j === 0 ? i + 1 : ''}</span>
              <iconify-icon icon={start ? icons.arrowRouting : end ? icons.record : icons.warning}></iconify-icon>
              <span class="max-sm:w-24">{formatDate(log.at)}</span>
              <span class="flex items-center gap-1 font-medium list-col-grow sm:pl-2">
                {$_(`flow.node.${start || end ? 'group' : 'name'}.${log.type}`)}
                {#if log.data}
                  <Button
                    icon={icons.eye}
                    text={$_('flow.exec.node_data')}
                    onclick={() => showDocModal(log.data, $_('flow.exec.node_data'))}
                  />
                {/if}
              </span>
              <Button
                disabled={!log.document}
                icon={start || end ? icons.search : icons.searchInfo}
                text={title}
                onclick={() => showDocModal(log.document, title)}
              />
            </li>
          {/each}
        </ul>
        {#if i < groups.length - 1}
          <div class="divider my-0 h-0"></div>
        {/if}
      {/each}
    {/if}
  </div>
</Modal>

<Modal title={documentTitle} maxWidth={documentLang ? '36rem' : '48rem'} bind:this={documentModal}>
  {#key refreshKey}
    <CodeMirror
      minHeight="24rem"
      minWidth="100%"
      maxWidth="100%"
      enlarger={false}
      readOnly={true}
      language={documentLang}
      {document}
    />
  {/key}
</Modal>
