<script lang="ts" module>
  import { api } from '$lib/api';
  import type { Resp } from '$lib/types';
  import type { Language, LanguageSupport } from '@codemirror/language';
  import type { Extension } from '@codemirror/state';
  import babel from 'prettier/plugins/babel';
  import estree from 'prettier/plugins/estree';
  import yaml from 'prettier/plugins/yaml';
  import * as prettier from 'prettier/standalone';

  export type CodeMirrorProps = Partial<{
    /** A language object manages parsing and per-language metadata. */
    language: LanguageSupport | Language | null;
    /** The document to display. */
    document: string;
    /** The placeholder text. */
    placeholder: string;
    /** The number of spaces per tab. */
    tabSize: number;
    /** The maximum length of a line. */
    lineLength: number;
    /** Whether the editor is read-only. */
    readOnly: boolean;
    /** Whether to use the dark theme. */
    darkMode: boolean;

    /** The class names for the container. */
    class: string;
    /** The class names for the panel. */
    panelClass: string;
    /** The class names for the editor. */
    editorClass: string;
    /** The minimum height of the editor. */
    minHeight: string | null;
    /** The maximum height of the editor. */
    maxHeight: string | null;
    /** The minimum width of the editor. */
    minWidth: string | null;
    /** The maximum width of the editor. */
    maxWidth: string | null;

    /** The title of the large view. */
    title: string;
    /** Whether to enable the large view feature. */
    enlarger: boolean;
    /** Whether to show the copy button in the panel. */
    copier: boolean;
    /** Whether to show the reset button in the panel. */
    resetter: boolean;
    /** Whether to show the format button in the panel. */
    formatter: boolean;
    /** Whether to show the evaluate button in the panel. */
    evaluator: boolean;
    /** The callback function when the document changes. */
    onchange: (doc: string) => void;
    /** The debounce time for the update listener in milliseconds. */
    debounceTime: number;
  }>;

  /**
   * Get the language name from the language object.
   *
   * @param language - The language object.
   * @returns The language name to display.
   */
  function getLanguageName(language: LanguageSupport | Language | null | undefined): string {
    // default to plain text
    let name = 'text';
    if (language) {
      name = 'name' in language ? language.name : language.language.name;
    }
    // mapping of language names not capitalized
    const mappings: Record<string, string> = {
      yaml: 'YAML',
      json: 'JSON',
      jsonc: 'JSONC',
      javascript: 'JavaScript'
    };
    // return mapped name or capitalized name
    return mappings[name] || name.charAt(0).toUpperCase() + name.slice(1);
  }

  /**
   * Replace the document in the editor view.
   *
   * @param view - The editor view.
   * @param newDoc - The new document.
   */
  function replace(view: EditorView, newDoc: string | undefined) {
    // https://codemirror.net/examples/change/
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: newDoc }
    });
  }

  /**
   * Format the document based on the language.
   *
   * @param view - The editor view.
   * @param language - The language of the document.
   * @param tabSize - The number of spaces per tab.
   * @param lineLength - The maximum length of a line.
   */
  async function format(view: EditorView, language: string, tabSize: number, lineLength: number) {
    try {
      const source = view.state.doc.toString();
      let formatted: string | null = null;
      switch (language.toLowerCase()) {
        case 'json':
          // format JSON using the built-in function
          formatted = JSON.stringify(JSON.parse(source), null, tabSize);
          break;
        case 'jsonc':
          // format JSON with Comments using Prettier
          // https://prettier.io/docs/en/options
          formatted = await prettier.format(source, {
            parser: 'jsonc',
            tabWidth: tabSize,
            printWidth: lineLength,
            trailingComma: 'none',
            plugins: [babel, estree]
          });
          break;
        case 'yaml':
          // format YAML using Prettier
          formatted = await prettier.format(source, {
            parser: 'yaml',
            tabWidth: tabSize,
            printWidth: lineLength,
            singleQuote: true,
            plugins: [yaml]
          });
          break;
        case 'javascript':
          // format JavaScript using Prettier
          formatted = await prettier.format(source, {
            parser: 'babel',
            tabWidth: tabSize,
            printWidth: lineLength,
            plugins: [babel, estree]
          });
          break;
        case 'python':
          // format Python using the REST API
          formatted = (
            await api
              .post('code/format', {
                json: {
                  source: source,
                  options: { indent_size: tabSize, max_line_length: lineLength }
                }
              })
              .json<Resp<string>>()
          ).data;
          break;
      }
      // replace the document with the formatted one
      if (formatted) {
        replace(view, formatted);
      }
    } catch (error) {
      console.error(error);
    }
  }

  /**
   * Evaluate the given Jinja2 template with the provided document.
   *
   * @param tmpl - The Jinja2 template to evaluate.
   * @param doc - The document to use for evaluation.
   * @param docName - The name of the document.
   */
  async function evaluate(tmpl: string, doc: string, docName: string | null) {
    try {
      return (
        await api
          .post('code/evaluate', {
            json: {
              template: tmpl,
              document: doc,
              doc_name: docName
            }
          })
          .json<Resp<string>>()
      ).data;
    } catch (error) {
      console.error(error);
    }
  }
</script>

<script lang="ts">
  import { Button, Modal } from '$lib/components';
  import { _ } from '$lib/i18n';
  import { icons } from '$lib/icons';
  import { debounce } from '$lib/utils';
  import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from '@codemirror/autocomplete';
  import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
  import { json } from '@codemirror/lang-json';
  import {
    bracketMatching,
    defaultHighlightStyle,
    foldGutter,
    foldKeymap,
    indentOnInput,
    indentUnit,
    syntaxHighlighting
  } from '@codemirror/language';
  import { lintKeymap } from '@codemirror/lint';
  import { highlightSelectionMatches, searchKeymap } from '@codemirror/search';
  import { EditorState } from '@codemirror/state';
  import { oneDark } from '@codemirror/theme-one-dark';
  import {
    crosshairCursor,
    drawSelection,
    dropCursor,
    EditorView,
    highlightActiveLine,
    highlightActiveLineGutter,
    highlightSpecialChars,
    keymap,
    lineNumbers,
    placeholder,
    rectangularSelection
  } from '@codemirror/view';
  import { onMount } from 'svelte';
  import CodeMirror from './CodeMirror.svelte';

  let {
    language,
    document = $bindable(),
    placeholder: _placeholder,
    tabSize = 2,
    lineLength = 80,
    readOnly = false,
    darkMode = false,

    class: _class,
    panelClass,
    editorClass,
    minHeight = '8rem',
    maxHeight = '24rem',
    minWidth = '32rem',
    maxWidth = '48rem',

    title = '',
    enlarger = !readOnly,
    copier = true,
    resetter = true,
    formatter = true,
    evaluator = false,
    onchange,
    debounceTime = 100
  }: CodeMirrorProps = $props();

  let editor: HTMLDivElement;
  let editorView: EditorView;
  let largerView: Modal | null = $state(null);
  let originalDoc = document;
  const languageName = $derived(getLanguageName(language));

  let evaluatorView: Modal | null = $state(null);
  let evalResult: CodeMirror | null = $state(null);
  let evalName: string | null = $state(null);
  let evalDoc: string | undefined = $state(undefined);

  /**
   * Translation of phrases in editor.
   */
  const phrases: Record<string, string> = {
    // search related translations
    Find: $_('codemirror.find'),
    Replace: $_('codemirror.replace'),
    next: $_('codemirror.next'),
    previous: $_('codemirror.previous'),
    all: $_('codemirror.all'),
    'match case': $_('codemirror.match_case'),
    regexp: $_('codemirror.regexp'),
    'by word': $_('codemirror.by_word'),
    replace: $_('codemirror.replace_action'),
    'replace all': $_('codemirror.replace_all'),
    close: $_('codemirror.close'),
    'current match': $_('codemirror.current_match'),
    'on line': $_('codemirror.on_line'),
    'replaced match on line $': $_('codemirror.replaced_match_on_line'),
    'replaced $ matches': $_('codemirror.replaced_matches'),
    // jump related translations
    'Go to line': $_('codemirror.go_to_line'),
    go: $_('codemirror.go')
  };

  // trigger the onchange callback with debounce
  // svelte-ignore state_referenced_locally
  const _onchange = debounce(() => {
    onchange?.(editorView.state.doc.toString());
  }, debounceTime);

  // evaluate the document in the evaluator view
  const _evaluate = debounce(() => {
    if (!document || !evalDoc) {
      evalResult?.setDocument('');
      return;
    }
    evaluate(document, evalDoc, evalName).then((res) => {
      if (res) {
        evalResult?.setDocument(res);
      }
    });
  });

  /**
   * Replace the current document with a new one.
   *
   * @param newDoc - The new document.
   * @param setOriginal - Whether to replace the original document.
   */
  export function setDocument(newDoc: string | undefined, setOriginal: boolean = false) {
    replace(editorView, newDoc);
    if (setOriginal) {
      originalDoc = newDoc;
    }
  }

  /**
   * The basic set of extensions for the editor.
   *
   * https://github.com/codemirror/basic-setup
   */
  const basicSetup: Extension = [
    lineNumbers(),
    indentOnInput(),
    history(),
    autocompletion(),
    closeBrackets(),
    bracketMatching(),
    dropCursor(),
    crosshairCursor(),
    drawSelection(),
    rectangularSelection(),
    EditorState.phrases.of(phrases),
    EditorState.allowMultipleSelections.of(true),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    highlightSpecialChars(),
    // foldGutter(),
    // highlightActiveLine(),
    // highlightActiveLineGutter(),
    // highlightSelectionMatches(),
    keymap.of([
      ...defaultKeymap,
      ...foldKeymap,
      ...historyKeymap,
      ...completionKeymap,
      ...closeBracketsKeymap,
      ...searchKeymap,
      ...lintKeymap
    ])
  ];

  /**
   * An extension that adds styles to the editor.
   *
   * https://codemirror.net/examples/styling/
   */
  const styleSheets: Extension = $derived([
    EditorView.baseTheme({
      '&': {
        userSelect: 'text'
      },
      '.cm-content, .cm-gutter': {
        cursor: 'text'
      },
      '.cm-scroller': {
        overflow: 'auto',
        overscrollBehavior: 'none'
      },
      '&.cm-focused': {
        outline: 'none'
      },
      '.cm-gutters': {
        backgroundColor: 'var(--color-base-200)',
        border: 'none'
      }
    }),
    EditorView.theme({
      '&': {
        minWidth: minWidth,
        maxWidth: maxWidth,
        maxHeight: maxHeight
      },
      '.cm-content, .cm-gutter': {
        minHeight: minHeight
      }
    })
  ]);

  /**
   * An extension to support the specified language.
   */
  const languageSupport: Extension = $derived(language ? language : []);

  /**
   * An extension to listen to the document changes.
   */
  const updateListener: Extension = EditorView.updateListener.of((update) => {
    if (update.docChanged) {
      if (onchange) {
        debounceTime > 0 ? _onchange() : onchange(update.state.doc.toString());
      } else {
        // if no onchange callback is provided, update the document prop directly
        document = update.state.doc.toString();
      }
    }
  });

  /**
   * An extension to handle the tab key.
   */
  const tabKeyHandler: Extension = $derived([keymap.of([indentWithTab]), indentUnit.of(' '.repeat(tabSize))]);

  /**
   * An extension to use the One Dark theme.
   */
  const darkModeHandler: Extension = $derived(darkMode ? oneDark : []);

  /**
   * An extension to make the editor read-only.
   */
  const readOnlyHandler: Extension = (() => {
    if (readOnly) {
      return [EditorState.readOnly.of(true), EditorView.editable.of(false)];
    }
    // only highlight the active line when the editor is editable
    return [foldGutter(), highlightActiveLine(), highlightActiveLineGutter(), highlightSelectionMatches()];
  })();

  /**
   * An extension to enable the placeholder text.
   */
  const placeholderHandler: Extension = $derived(_placeholder ? placeholder(_placeholder) : []);

  onMount(() => {
    // construct a new view
    editorView = new EditorView({
      parent: editor,
      // create a new state
      state: EditorState.create({
        doc: document,
        extensions: [
          basicSetup,
          styleSheets,
          languageSupport,
          updateListener,
          tabKeyHandler,
          darkModeHandler,
          readOnlyHandler,
          placeholderHandler
        ]
      })
    });
    return () => {
      editorView.destroy();
    };
  });
</script>

<svelte:window
  onkeydown={(event) => {
    if (readOnly) {
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.code === 'KeyU') {
      // update the document when undo/redo is triggered
      setTimeout(() => setDocument(document));
    }
  }}
/>

<div class="shrink-0 overflow-auto rounded-box border {_class}">
  <div class={editorClass} bind:this={editor}></div>
  <div class="flex items-center justify-between border-t px-2 py-0.5 {panelClass}">
    <span class="flex-center gap-1">
      <Button
        size="xs"
        icon={enlarger ? icons.fullScreenMaximizeFilled : icons.code}
        class="border-0 bg-transparent shadow-none {enlarger ? '' : 'pointer-events-none'}"
        onclick={() => enlarger && largerView?.show()}
      />
      <span class="text-xs font-semibold opacity-70">{languageName}</span>
    </span>
    <span class="flex-center gap-1">
      {#if !readOnly && resetter}
        <Button
          size="xs"
          icon={icons.arrowReset}
          class="border-0 bg-transparent shadow-none"
          onclick={() => replace(editorView, originalDoc)}
        />
      {/if}
      {#if !readOnly && language && formatter}
        <Button
          size="xs"
          icon={icons.alignRight}
          class="border-0 bg-transparent shadow-none"
          onclick={() => format(editorView, languageName, tabSize, lineLength)}
        />
      {/if}
      {#if !readOnly && evaluator}
        <Button
          size="xs"
          icon={icons.clipboardCode}
          class="border-0 bg-transparent shadow-none"
          onclick={() => {
            evaluatorView?.show();
            _evaluate();
          }}
        />
      {/if}
      {#if copier}
        <Button
          size="xs"
          icon={icons.documentCopy}
          class="border-0 bg-transparent shadow-none"
          onclick={() => document && navigator.clipboard && navigator.clipboard.writeText(document)}
        />
      {/if}
    </span>
  </div>
</div>

{#if enlarger}
  <Modal title={title || languageName} maxWidth="80rem" class="nodrag nowheel cursor-auto" bind:this={largerView}>
    <CodeMirror
      {language}
      {document}
      placeholder={_placeholder}
      {tabSize}
      {lineLength}
      {readOnly}
      {darkMode}
      minHeight="80dvh"
      maxHeight="calc(100dvh - 10rem)"
      minWidth="100%"
      maxWidth="100%"
      enlarger={false}
      {copier}
      {resetter}
      {formatter}
      onchange={(doc) => replace(editorView, doc)}
    />
  </Modal>
{/if}

{#if evaluator}
  <Modal
    icon={icons.clipboardCode}
    title={$_('code.evaluator')}
    maxWidth="80rem"
    class="nodrag nowheel cursor-auto"
    bind:this={evaluatorView}
  >
    <CodeMirror
      {language}
      {document}
      placeholder={_placeholder}
      {tabSize}
      {lineLength}
      {readOnly}
      {darkMode}
      minHeight="12rem"
      maxHeight="12rem"
      minWidth="100%"
      maxWidth="100%"
      {copier}
      {resetter}
      {formatter}
      onchange={(doc) => {
        replace(editorView, doc);
        _evaluate();
      }}
    />
    <div class="-mt-2.5 grid grid-cols-2 gap-1.5">
      <div>
        <label class="input w-full gap-3 rounded-t-box rounded-b-none ps-2 input-sm">
          <iconify-icon icon={icons.bracesVariable} width="1rem"></iconify-icon>
          <input
            type="text"
            class="grow truncate"
            placeholder={$_('code.var_name')}
            bind:value={evalName}
            oninput={_evaluate}
          />
        </label>
        <CodeMirror
          placeholder={$_('code.raw_doc')}
          minHeight="22rem"
          maxHeight="22rem"
          minWidth="100%"
          maxWidth="100%"
          enlarger={false}
          class="rounded-t-none border-t-0 pt-px"
          document={evalDoc}
          onchange={(doc) => {
            evalDoc = doc;
            _evaluate();
          }}
        />
      </div>
      <CodeMirror
        placeholder={$_('code.eval_result')}
        minHeight="24rem"
        minWidth="100%"
        maxWidth="100%"
        enlarger={false}
        readOnly={true}
        language={json()}
        bind:this={evalResult}
      />
    </div>
  </Modal>
{/if}
