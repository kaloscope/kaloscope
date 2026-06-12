<script lang="ts">
  import { CodeMirror, Label } from '$lib/components';
  import { nodeFormatter } from '$lib/i18n';
  import type { Field, FlowGraphContext } from '$lib/types';
  import { javascript } from '@codemirror/lang-javascript';
  import { python } from '@codemirror/lang-python';
  import type { LanguageSupport } from '@codemirror/language';
  import { useSvelteFlow } from '@xyflow/svelte';
  import { getContext, hasContext } from 'svelte';

  /**
   * The JavaScript code template.
   */
  const JAVASCRIPT_TEMPLATE = `
/**
 * @param nodeId   - The node ID string.
 * @param nodeData - The node data object.
 * @param context  - The global context object.
 */
function execute(nodeId, nodeData, context) {
    // write your code here
}
`.trimStart();

  /**
   * The Python code template.
   */
  const PYTHON_TEMPLATE = `
def execute(node_id, node_data, context):
    """
    Args:
        node_id:   The node ID string.
        node_data: The node data dictionary.
        context:   The global context dictionary.
    """
    # write your code here
`.trimStart();

  /**
   * The supported script languages.
   */
  type Language = 'python' | 'javascript';

  /**
   * The node data for the script field.
   */
  type NodeData = {
    language: Language;
    code: string | null;
  };

  /**
   * The converted script data.
   */
  type ScriptData = {
    language: Language;
    support: LanguageSupport;
    code: string;
  };

  let {
    data: _data = { language: 'python', code: null },
    ...field
  }: Field & {
    nodeId: string;
    data?: NodeData;
    placeholder: string | null;
    tabsize: number;
    lineLength: number;
    darkmode: boolean;
    copier: boolean;
    resetter: boolean;
    formatter: boolean;
  } = $props();

  let data: ScriptData = $derived(scriptData(_data.language, _data.code));
  const { label, tooltip, placeholder } = nodeFormatter;
  const { updateNodeData } = useSvelteFlow();

  /**
   * Get the script data.
   *
   * @param language - The language of the script.
   * @param code - The code of the script.
   * @returns The script data object.
   */
  function scriptData(language: Language, code: string | null = null): ScriptData {
    if (language === 'javascript') {
      return {
        language: 'javascript',
        support: javascript(),
        code: code ?? JAVASCRIPT_TEMPLATE
      };
    } else {
      return {
        language: 'python',
        support: python(),
        code: code ?? PYTHON_TEMPLATE
      };
    }
  }

  // register the validator
  let validatorClass = $state('');
  // svelte-ignore state_referenced_locally
  if (field.required && hasContext('flow/graph')) {
    const context = getContext('flow/graph') as FlowGraphContext;
    context.addValidator(() => {
      const isNotEmpty = !!data.code.trim();
      if (!isNotEmpty) {
        validatorClass = 'animate-emphasis';
        setTimeout(() => (validatorClass = ''), 1000);
      }
      return isNotEmpty;
    });
  }
</script>

<fieldset class="fieldset">
  <Label required={true}>{$label('language')}</Label>
  <select
    class="nodrag select w-full appearance-none select-sm"
    value={data.language}
    onchange={(event) => {
      data = scriptData(event.currentTarget.value as Language);
      updateNodeData(field.nodeId, {
        [field.id]: { language: data.language, code: data.code }
      });
    }}
  >
    <option value="python">Python</option>
    <option value="javascript">JavaScript</option>
  </select>
  <Label required={field.required} tip={$tooltip(field.tooltip)}>
    {$label(field.label)}
  </Label>
  {#key data.language}
    <CodeMirror
      document={data.code}
      language={data.support}
      placeholder={$placeholder(field.placeholder)}
      tabSize={field.tabsize}
      lineLength={field.lineLength}
      darkMode={field.darkmode}
      title={$label(field.label)}
      copier={field.copier}
      resetter={field.resetter}
      formatter={field.formatter}
      minWidth="28rem"
      class={validatorClass}
      editorClass="nodrag nowheel"
      onchange={(doc) => {
        updateNodeData(field.nodeId, {
          [field.id]: { language: data.language, code: doc }
        });
      }}
    />
  {/key}
</fieldset>
