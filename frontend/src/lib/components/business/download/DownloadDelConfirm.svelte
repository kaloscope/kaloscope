<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { Modal } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';

  let { onconfirm }: { onconfirm?: () => void } = $props();
  let taskIds = $state<number[]>([]);

  // the modal dialog instance
  let modal: Modal;
  export const showModal = (ids: number | number[]) => {
    taskIds = Array.isArray(ids) ? ids : [ids];
    modal.show();
  };

  // the loading state
  const loading = createLoading();

  /**
   * Delete download tasks by ID.
   *
   * @param form - The form element.
   * @param data - The form data.
   */
  function del(form: HTMLFormElement, data: FormData) {
    loading.start();
    api
      .post('download/delete', {
        json: { ids: taskIds, local: !!data.get('local') }
      })
      .then(() => {
        modal.close();
        onconfirm?.();
        setTimeout(() => form.reset(), 200);
      })
      .finally(() => {
        loading.end();
      });
  }
</script>

<Modal icon={icons.delete} title={$_('action.delete', $_('entity.download'))} bind:this={modal}>
  <form
    method="post"
    use:enhance={({ formElement, formData, cancel }) => {
      cancel();
      del(formElement, formData);
    }}
  >
    <fieldset class="mt-2 fieldset">
      <label class="mt-2 fieldset-label w-fit">
        <input type="checkbox" class="checkbox" checked={false} name="local" />
        <span class="text-base text-base-content opacity-90">{$_('download.delete_local')}</span>
      </label>
    </fieldset>
    <div class="modal-action">
      <button type="button" class="btn" onclick={() => modal.close()}>
        {$_('message.cancel')}
      </button>
      <button type="submit" class="btn btn-submit" disabled={$loading !== null}>
        {$_('message.confirm')}
        {#if $loading}
          <span class="loading loading-xs loading-dots"></span>
        {/if}
      </button>
    </div>
  </form>
</Modal>
