import type { AssistantStreamEvent } from '$lib/types';

/**
 * Validate public event fields before changing the conversation state.
 *
 * @param event - SSE event name.
 * @param source - Untrusted SSE data to decode only for recognized events.
 * @returns A typed event, or null for an unknown extension event.
 */
function parseEvent(event: string, source: string): AssistantStreamEvent | null {
  if (!['start', 'compacting', 'text_delta', 'tool_start', 'tool_result', 'done', 'error'].includes(event)) return null;
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    throw new Error('assistant_stream_invalid');
  }
  if (!value || typeof value !== 'object') throw new Error('assistant_stream_invalid');
  const data = value as Record<string, unknown>;
  const hasTool = typeof data.id === 'string' && typeof data.name === 'string';
  const valid =
    (event === 'start' && typeof data.turn_id === 'string') ||
    (event === 'compacting' && typeof data.active === 'boolean') ||
    (event === 'text_delta' && typeof data.text === 'string') ||
    (event === 'tool_start' && hasTool) ||
    (event === 'tool_result' &&
      hasTool &&
      typeof data.ok === 'boolean' &&
      (data.ok
        ? data.data !== null && typeof data.data === 'object'
        : !!data.error && typeof (data.error as Record<string, unknown>).code === 'string')) ||
    (event === 'done' && typeof data.tool_calls === 'number') ||
    (event === 'error' && typeof data.code === 'string');
  if (!valid) throw new Error('assistant_stream_invalid');
  return { event, data } as AssistantStreamEvent;
}

/**
 * Consume fragmented UTF-8 SSE frames and require an explicit terminal event.
 *
 * @param body - Streaming response body owned by this turn.
 * @param receive - Callback for validated public events.
 * @param activity - Callback for bytes, including comment heartbeats.
 * @returns The terminal event name after canceling the response reader.
 */
export async function consumeAssistantStream(
  body: ReadableStream<Uint8Array>,
  receive: (event: AssistantStreamEvent) => void,
  activity: () => void = () => {}
): Promise<'done' | 'error'> {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '';
  let event = '';
  let data: string[] = [];
  let eventSize = 0;
  let totalBytes = 0;
  let terminal: 'done' | 'error' | undefined;

  /**
   * Process one complete SSE line without interpreting comment heartbeats.
   *
   * @param line - A line with its terminator removed.
   */
  function consumeLine(line: string): void {
    if (!line) {
      if (data.length) {
        const parsed = parseEvent(event, data.join('\n'));
        if (parsed) {
          receive(parsed);
          if (parsed.event === 'done' || parsed.event === 'error') terminal = parsed.event;
        }
      }
      event = '';
      data = [];
      eventSize = 0;
      return;
    }
    eventSize += line.length;
    if (eventSize > 512 * 1024) throw new Error('assistant_stream_invalid');
    if (line.startsWith(':')) return;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    if (field === 'data') data.push(value);
  }

  try {
    while (!terminal) {
      const { value, done } = await reader.read();
      if (value) {
        totalBytes += value.byteLength;
        if (totalBytes > 4 * 1024 * 1024) throw new Error('assistant_stream_invalid');
        activity();
      }
      pending += decoder.decode(value, { stream: !done });
      let match: RegExpExecArray | null;
      while (!terminal && (match = /\r\n|\r|\n/.exec(pending))) {
        // wait for the next chunk when a CR may be the first half of CRLF
        if (!done && match[0] === '\r' && match.index === pending.length - 1) break;
        const line = pending.slice(0, match.index);
        pending = pending.slice(match.index + match[0].length);
        consumeLine(line);
      }
      if (pending.length + eventSize > 512 * 1024) throw new Error('assistant_stream_invalid');
      if (done && !terminal) throw new Error('assistant_stream_interrupted');
    }
    return terminal;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
