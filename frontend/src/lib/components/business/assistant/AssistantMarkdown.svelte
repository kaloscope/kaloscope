<script lang="ts">
  import { parseMarkdown, type MarkdownBlock, type MarkdownInline } from '$lib/assistant/markdown';

  const { source }: { source: string } = $props();
  const blocks = $derived(parseMarkdown(source));
</script>

{#snippet inlines(nodes: MarkdownInline[])}
  {#each nodes as node, index (index)}
    {#if node.type === 'text'}
      {node.text}
    {:else if node.type === 'break'}
      <br />
    {:else if node.type === 'code'}
      <code>{node.text}</code>
    {:else if node.type === 'strong'}
      <strong>{@render inlines(node.children)}</strong>
    {:else if node.type === 'emphasis'}
      <em>{@render inlines(node.children)}</em>
    {:else if node.type === 'delete'}
      <del>{@render inlines(node.children)}</del>
    {:else if node.type === 'link'}
      <!-- link targets stay inert; navigation is offered only through validated tool results -->
      <span>{@render inlines(node.children)}</span>
      {#if node.url}
        <span class="text-base-content/50">({node.url})</span>
      {/if}
    {/if}
  {/each}
{/snippet}

{#snippet render(nodes: MarkdownBlock[])}
  {#each nodes as block, index (index)}
    {#if block.type === 'paragraph'}
      <p>{@render inlines(block.children)}</p>
    {:else if block.type === 'heading'}
      <svelte:element this={`h${Math.min(block.level + 2, 6)}`}>{@render inlines(block.children)}</svelte:element>
    {:else if block.type === 'code'}
      <pre><code>{block.text}</code></pre>
    {:else if block.type === 'quote'}
      <blockquote>{@render render(block.children)}</blockquote>
    {:else if block.type === 'list'}
      <svelte:element
        this={block.ordered ? 'ol' : 'ul'}
        start={block.ordered && block.start !== 1 ? block.start : undefined}
      >
        {#each block.items as item, position (position)}
          <li>
            {#each item as child, offset (offset)}
              {#if offset === 0 && child.type === 'paragraph'}
                <!-- keep single-paragraph items compact, like a tight Markdown list -->
                {@render inlines(child.children)}
              {:else}
                {@render render([child])}
              {/if}
            {/each}
          </li>
        {/each}
      </svelte:element>
    {:else if block.type === 'table'}
      <div class="overflow-x-auto">
        <table>
          <thead>
            <tr>
              {#each block.head as cell, column (column)}
                <th style:text-align={block.align[column]}>{@render inlines(cell)}</th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each block.rows as row, line (line)}
              <tr>
                {#each row as cell, column (column)}
                  <td style:text-align={block.align[column]}>{@render inlines(cell)}</td>
                {/each}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <hr />
    {/if}
  {/each}
{/snippet}

<div
  class="assistant-markdown prose prose-sm max-w-none min-w-0 leading-6 [overflow-wrap:anywhere] text-base-content/90"
>
  {@render render(blocks)}
</div>

<style>
  .assistant-markdown {
    :global(:where(p, ul, ol, blockquote, pre, table, hr)) {
      margin-block: 0.5rem;
    }
    :global(:where(h3, h4, h5, h6)) {
      margin-block: 0.875rem 0.375rem;
      font-size: 0.875rem;
      font-weight: 600;
    }
    :global(:where(li)) {
      margin-block: 0.125rem;
    }
    :global(:where(li > ul, li > ol)) {
      margin-block: 0.125rem;
    }
    :global(:where(pre)) {
      padding: 0.625rem 0.75rem;
      border-radius: var(--radius-field);
      font-size: 0.75rem;
      line-height: 1.25rem;
      overflow-wrap: normal;
    }
    :global(:where(blockquote)) {
      font-style: normal;
      font-weight: inherit;
      color: color-mix(in oklab, var(--color-base-content) 70%, transparent);
    }
    :global(:where(blockquote p)::before),
    :global(:where(blockquote p)::after) {
      content: none;
    }
    :global(:where(th, td)) {
      padding: 0.25rem 0.5rem;
    }
    > :global(:first-child) {
      margin-top: 0;
    }
    > :global(:last-child) {
      margin-bottom: 0;
    }
  }
</style>
