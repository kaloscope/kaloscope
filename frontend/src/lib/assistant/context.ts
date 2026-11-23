import type { AssistantPageContext, FlowGraph, NodeSchema } from '$lib/types';
import { requiresAssistantContext } from './navigation';

const readers: (() => AssistantPageContext | null)[] = [];
const encoder = new TextEncoder();

/**
 * Remove transient selection, measurements, and expanded schemas from a canvas.
 *
 * @param document - Current or saved editor document.
 * @returns Node and edge copies preserving authored data and layout.
 */
export function snapshotAssistantCanvas(document: NonNullable<FlowGraph['draft']>): NonNullable<FlowGraph['draft']> {
  return {
    nodes: document.nodes.map((node) => {
      const result = { ...node, data: { ...node.data } };
      delete result.selected;
      delete result.measured;
      delete result.dragging;

      const schema = result.data.$schema;
      if (schema && typeof schema === 'object') result.data.$schema = (schema as NodeSchema).node_type;

      return result;
    }),
    edges: document.edges.map((edge) => {
      const result = { ...edge };
      delete result.selected;

      return result;
    })
  };
}

/**
 * Register a live context reader for the lifetime of a supported page.
 *
 * @param read - Callback reading the current selection and canvas at send time.
 * @returns Cleanup that removes only this surface's reader.
 */
export function registerAssistantContext(read: () => AssistantPageContext | null): () => void {
  readers.push(read);

  return () => {
    const index = readers.indexOf(read);
    if (index >= 0) readers.splice(index, 1);
  };
}

/**
 * Copy a supported page once so a turn cannot observe later canvas edits.
 *
 * @param route - Current internal pathname, without arbitrary query parameters.
 * @returns An independent context snapshot or null for an unsupported page.
 * @throws If page metadata or its separate canvas attachment exceeds its budget.
 */
export function captureAssistantContext(route: string): AssistantPageContext | null {
  const pathname = route.replace(/\/$/, '');

  for (const read of [...readers].reverse()) {
    const context = read();
    if (context?.route !== pathname) continue;

    if (context.page_type === 'workflow_designer') {
      const { canvas, ...reference } = context;
      // serialize the canvas and metadata separately once so large graphs are never stringified twice
      const canvasJson = JSON.stringify(canvas);
      if (encoder.encode(canvasJson).length > 512 * 1024) {
        throw new Error('assistant_context_limit');
      }
      const metadataJson = JSON.stringify(reference);
      if (encoder.encode(metadataJson).length > 128 * 1024) {
        throw new Error('assistant_context_limit');
      }
      return {
        ...(JSON.parse(metadataJson) as Omit<typeof context, 'canvas'>),
        canvas: JSON.parse(canvasJson) as typeof canvas
      };
    }

    const json = JSON.stringify(context);
    if (encoder.encode(json).length > 128 * 1024) throw new Error('assistant_context_limit');

    return JSON.parse(json) as AssistantPageContext;
  }

  if (requiresAssistantContext(route)) {
    throw new Error('assistant_context_invalid');
  }

  return null;
}
