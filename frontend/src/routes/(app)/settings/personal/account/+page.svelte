<script lang="ts">
  import { enhance } from '$app/forms';
  import { goto } from '$app/navigation';
  import { cropper } from '$lib/actions';
  import { api } from '$lib/api';
  import { Container, Image, Modal, Setting, alert } from '$lib/components';
  import { createFormSchema, createLoading } from '$lib/helpers';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { token, user } from '$lib/stores';
  import { loadFile } from '$lib/utils/browser';

  let cropSrc: string | null = $state(null);
  let cropImg: Promise<Blob | null> | null = null;
  let avatarDialog: Modal;
  let avatarInput: HTMLInputElement;
  let avatarSrc: string = $derived($user?.avatar ?? '');
  const avatarChanging = createLoading();

  /**
   * Submit cropped avatar image to server
   */
  async function submitCropAvatar() {
    avatarChanging.start();
    // construct form data
    const formData = new FormData();
    const avatar = cropSrc && (await cropImg);
    if (avatar) {
      formData.append('avatar', avatar);
    }
    // send request to change avatar
    api
      .post('user/change_avatar', { body: formData })
      .then(() => {
        // trigger refresh of user info
        user.set(null);
        cropSrc = null;
        avatarInput.value = '';
        avatarDialog.close();
      })
      .finally(() => {
        avatarChanging.end();
      });
  }

  const pwdChanging = createLoading();
  const pwdSchema = createFormSchema(({ password }) => ({
    cur_pwd: password().minlength(6).maxlength(64).autocomplete('current-password'),
    new_pwd: password().minlength(6).maxlength(64).autocomplete('new-password'),
    confirm_pwd: password().minlength(6).maxlength(64).autocomplete('new-password')
  }));

  /**
   * Change password with given data.
   *
   * @param data - Form data.
   */
  function changePwd(data: FormData) {
    if (data.get('new_pwd') !== data.get('confirm_pwd')) {
      alert({ level: 'error', message: 'passwords_mismatch' });
      return;
    }
    pwdChanging.start();
    api
      .post('user/change_pwd', { body: data })
      .then(() => {
        token.set(null);
        alert({ level: 'success', message: 'password_changed' });
        goto('/login');
      })
      .finally(() => {
        pwdChanging.end();
      });
  }
</script>

<Container type="settings">
  <div class="mt-2 -mb-12 flex-col-center gap-2">
    <Image
      circle
      shadow
      transparent
      src={$user?.avatar}
      icon={icons.user}
      width="8rem"
      class="hover:[&+button]:opacity-70"
      onclick={() => avatarDialog.show()}
    />
    <button
      class="btn btn-subtle opacity-0 transition-opacity duration-500 btn-xs hover:opacity-70"
      onclick={() => avatarDialog.show()}
    >
      <iconify-icon icon={icons.switch} width="1rem"></iconify-icon>
      {$_('action.change', $_('field.avatar'))}
    </button>
  </div>
  <Setting title={$_('password.change')}>
    <form
      method="post"
      use:enhance={({ formData, cancel }) => {
        cancel();
        changePwd(formData);
      }}
    >
      <fieldset class="fieldset">
        <label class="input w-full">
          <iconify-icon icon={icons.userFill}></iconify-icon>
          <input type="text" class="grow" value={$user?.username ?? ''} disabled />
        </label>
        <label class="input w-full">
          <iconify-icon icon={icons.key}></iconify-icon>
          <input placeholder={$_('password.current')} class="grow" {...pwdSchema.cur_pwd} />
        </label>
        <label class="input mt-8 w-full">
          <iconify-icon icon={icons.keyFilled}></iconify-icon>
          <input placeholder={$_('password.new')} class="grow" {...pwdSchema.new_pwd} />
        </label>
        <label class="input w-full">
          <iconify-icon icon={icons.keyFilled}></iconify-icon>
          <input placeholder={$_('password.confirm')} class="grow" {...pwdSchema.confirm_pwd} />
        </label>
        <button type="submit" class="btn mt-8 btn-submit" disabled={$pwdChanging !== null}>
          {$_('password.change')}
          {#if $pwdChanging}
            <span class="loading loading-xs loading-dots"></span>
          {/if}
        </button>
      </fieldset>
    </form>
  </Setting>
</Container>

<Modal title={$_('action.change', $_('field.avatar'))} bind:this={avatarDialog}>
  <div class="flex-col-center gap-4">
    {#if cropSrc}
      <div class="flex-center size-80 overflow-hidden rounded-full border">
        {#key cropSrc}
          <img src={cropSrc} alt="" class="size-full" use:cropper={(img) => (cropImg = img)} />
        {/key}
      </div>
    {:else}
      <div class="group relative size-80">
        <Image circle border src={avatarSrc} icon={icons.user} width="320px" />
        {#if avatarSrc}
          {@const opacityClass = 'opacity-0 group-hover:opacity-100 transition-opacity'}
          <button
            class="btn absolute top-7.5 right-7.5 btn-square btn-sm {opacityClass}"
            aria-label="Delete"
            onclick={(event) => {
              event.preventDefault();
              cropSrc = '';
              avatarSrc = '';
              avatarInput.value = '';
            }}
          >
            <iconify-icon icon={icons.delete} width="1.5rem"></iconify-icon>
          </button>
        {/if}
      </div>
    {/if}
    <input
      type="file"
      accept="image/*"
      class="file-input w-full max-w-sm file-input-sm"
      bind:this={avatarInput}
      onchange={async (event) => (cropSrc = await loadFile(event.currentTarget))}
    />
  </div>
  <div class="modal-action">
    <button class="btn" onclick={() => avatarDialog.close()}>
      {$_('message.cancel')}
    </button>
    <button
      class="btn btn-submit"
      disabled={cropSrc === null || $avatarChanging !== null}
      onclick={(event) => {
        event.preventDefault();
        submitCropAvatar();
      }}
    >
      {$_('message.confirm')}
      {#if $avatarChanging}
        <span class="loading loading-xs loading-dots"></span>
      {/if}
    </button>
  </div>
</Modal>
