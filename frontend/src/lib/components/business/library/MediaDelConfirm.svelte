<script lang="ts">
  import { enhance } from '$app/forms';
  import { api } from '$lib/api';
  import { Modal, confirm, mediaTitle } from '$lib/components';
  import { createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import type { MediaItem } from '$lib/types';

  let { onconfirm }: { onconfirm?: () => void } = $props();
  let item: MediaItem | null = $state(null);
  let local = $state(false);

  // the modal dialog instance
  let modal: Modal;
  export const showModal = (target: MediaItem) => {
    item = target;
    local = false;
    modal.show();
  };

  // the loading state
  const loading = createLoading();

  /**
   * Confirm the deletion of local files.
   */
  function confirmLocalDelete() {
    local = true;
    confirm({
      shallow: false,
      message: $_('media.delete_local_confirm'),
      onconfirm: () => {
        local = true;
      },
      oncancel: () => {
        local = false;
      }
    });
  }

  /**
   * Delete a media item by ID.
   *
   * @param form - The form element.
   * @param data - The form data.
   */
  function del(form: HTMLFormElement, data: FormData) {
    if (!item) {
      return;
    }
    loading.start();
    api
      .post('media/delete', {
        json: { ids: [item.id], local: !!data.get('local') }
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

<Modal icon={icons.delete} title={$_('action.delete', item ? `[${mediaTitle(item)}]` : '')} bind:this={modal}>
  <form
    method="post"
    use:enhance={({ formElement, formData, cancel }) => {
      cancel();
      del(formElement, formData);
    }}
  >
    <fieldset class="mt-2 fieldset">
      <label class="mt-2 fieldset-label w-fit">
        <input
          type="checkbox"
          class="checkbox"
          checked={local}
          name="local"
          onchange={(event) => {
            event.currentTarget.checked ? confirmLocalDelete() : (local = false);
          }}
        />
        <span class="text-base text-base-content opacity-90">{$_('media.delete_local')}</span>
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
