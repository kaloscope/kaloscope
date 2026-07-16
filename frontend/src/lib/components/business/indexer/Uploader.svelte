<script lang="ts" module>
  type UploaderProps = Partial<{
    up: string | null;
    at: string | null;
    extra: string | null;
    class: string;
  }>;
</script>

<script lang="ts">
  let { up, at, extra, class: _class }: UploaderProps = $props();
</script>

<div class="flex items-center justify-between gap-2 pr-1 text-xs {_class}">
  {#if up || at}
    {@const info = [up && `UP: ${up}`, at].filter(Boolean).join(' ・ ')}
    <span class="line-clamp-2 opacity-50" title={info}>{info}</span>
  {/if}
  {#if extra}
    <span class="shrink-0 truncate pr-0.5 italic opacity-40 {up || at ? 'max-w-1/2' : 'max-w-full'}" title={extra}>
      {extra}
    </span>
  {/if}
</div>
