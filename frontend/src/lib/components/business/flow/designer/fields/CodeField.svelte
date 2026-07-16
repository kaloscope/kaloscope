<script lang="ts" module>
  import type { Field, FlowGraphContext } from '$lib/types';

  /**
   * The supported languages for the code field.
   */
  type Language = 'yaml' | 'json' | 'jsonc' | 'jinja2' | 'python' | 'javascript';

  /**
   * Preload all template files from the templates directory.
   */
  const templates: Record<string, string> = import.meta.glob('/src/templates/**/*.jsonc', {
    eager: true,
    query: '?raw',
    import: 'default'
  });

  /**
   * Load a template file by its name.
   *
   * @param template - The name of the template file.
   */
  const loadTemplate = (template: string) => templates[`/src/templates/${template}`] ?? '';
</script>

<script lang="ts">
  import { CodeMirror, Label } from '$lib/components';
  import { jsonc } from '$lib/grammars/jsonc';
  import { nodeFormatter } from '$lib/i18n';
  import { javascript } from '@codemirror/lang-javascript';
  import { json } from '@codemirror/lang-json';
  import { python } from '@codemirror/lang-python';
  import { yaml } from '@codemirror/lang-yaml';
  import { StreamLanguage } from '@codemirror/language';
  import { jinja2 } from '@codemirror/legacy-modes/mode/jinja2';
  import { useSvelteFlow } from '@xyflow/svelte';
  import { getContext, hasContext, onMount } from 'svelte';

  let {
    data = '',
    ...field
  }: Field & {
    nodeId: string;
    data?: string;
    template: string | null;
    placeholder: string | null;
    language: Language | null;
    tabsize: number;
    lineLength: number;
    collapse: boolean;
    readonly: boolean;
    darkmode: boolean;
    copier: boolean;
    resetter: boolean;
    formatter: boolean;
    width: string | null;
  } = $props();

  const { updateNodeData } = useSvelteFlow();
  const { label, tooltip, placeholder } = nodeFormatter;

  /**
   * Get the language support object for CodeMirror.
   *
   * @param lang - The language name.
   */
  const languageSupport = (lang: string | null) => {
    switch (lang) {
      case 'yaml':
        return yaml();
      case 'json':
        return json();
      case 'jsonc':
        return jsonc();
      case 'jinja2':
        return StreamLanguage.define(jinja2);
      case 'python':
        return python();
      case 'javascript':
        return javascript();
      default:
        return null;
    }
  };

  // register the validator
  let validatorClass = $state('');
  // svelte-ignore state_referenced_locally
  if (field.required && hasContext('flow/graph')) {
    const context = getContext('flow/graph') as FlowGraphContext;
    context.addValidator(() => {
      const isNotEmpty = !!data.trim();
      if (!isNotEmpty) {
        validatorClass = 'animate-emphasis';
        setTimeout(() => (validatorClass = ''), 1000);
      }
      return isNotEmpty;
    });
  }

  // load template if data is empty and template is set
  let mounted = $state(false);
  onMount(() => {
    if (!data && field.template) {
      data = loadTemplate(field.template);
    }
    mounted = true;
  });
</script>

{#if field.collapse}
  <fieldset class="collapse-arrow collapse overflow-hidden border">
    <input type="checkbox" />
    <div class="collapse-title bg-base-150 py-2 text-base [&:after]:top-[1.4rem]!">
      {$label(field.label)}
    </div>
    <div class="collapse-content p-0!">
      {@render code('rounded-t-none border-x-0 border-b-0')}
    </div>
  </fieldset>
{:else}
  <fieldset class="fieldset">
    <Label required={field.required} tip={$tooltip(field.tooltip)}>
      {$label(field.label)}
    </Label>
    {@render code()}
  </fieldset>
{/if}

{#snippet code(_class?: string)}
  {#if mounted}
    <CodeMirror
      document={data}
      language={languageSupport(field.language)}
      placeholder={$placeholder(field.placeholder)}
      tabSize={field.tabsize}
      lineLength={field.lineLength}
      readOnly={field.readonly}
      darkMode={field.darkmode}
      title={$label(field.label)}
      copier={field.copier}
      resetter={field.resetter}
      formatter={field.formatter}
      evaluator={field.language === 'jinja2'}
      minWidth={field.width}
      maxWidth={field.width}
      class="{validatorClass} {_class}"
      editorClass="nodrag nowheel"
      onchange={(doc) => {
        updateNodeData(field.nodeId, { [field.id]: doc });
      }}
    />
  {/if}
{/snippet}
