<script lang="ts">
  import { Empty } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { user } from '$lib/stores';
</script>

<!-- placeholder for an installation without downloaders -->
<Empty title={$_('empty.downloaders.title')} description={$_('empty.downloaders.tip')}>
  {#if $user?.role === 'admin'}
    <a href="/settings/system/downloaders" class="empty-action">
      <iconify-icon icon={icons.addCircle} width="1rem" aria-hidden="true"></iconify-icon>
      {$_('empty.downloaders.action')}
    </a>
  {/if}
</Empty>
