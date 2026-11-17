<script lang="ts">
  import { Empty } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores';
</script>

<!-- placeholder for an installation without media libraries -->
<div class="flex-center min-h-[60vh] w-full">
  <Empty title={$_('empty.medialibs.title')} description={$_('empty.medialibs.tip')}>
    {#if $user?.role === 'admin'}
      <a href="/settings/system/medialibs" class="empty-action">
        <iconify-icon icon={icons.addCircle} width="1rem" aria-hidden="true"></iconify-icon>
        {$_('empty.medialibs.action')}
      </a>
    {/if}
  </Empty>
</div>
