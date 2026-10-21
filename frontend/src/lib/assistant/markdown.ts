import { getDefaults, Lexer, type MarkedToken, type Token } from 'marked';

export type MarkdownInline =
  | { type: 'text'; text: string }
  | { type: 'break' }
  | { type: 'code'; text: string }
  | { type: 'strong' | 'emphasis' | 'delete'; children: MarkdownInline[] }
  | { type: 'link'; url: string; children: MarkdownInline[] };

export type MarkdownBlock =
  | { type: 'paragraph'; children: MarkdownInline[] }
  | { type: 'heading'; level: number; children: MarkdownInline[] }
  | { type: 'code'; language: string; text: string }
  | { type: 'quote'; children: MarkdownBlock[] }
  | { type: 'list'; ordered: boolean; start: number; items: MarkdownBlock[][] }
  | {
      type: 'table';
      align: ('left' | 'center' | 'right' | null)[];
      head: MarkdownInline[][];
      rows: MarkdownInline[][][];
    }
  | { type: 'rule' };

let entityDecoder: HTMLTemplateElement | undefined;

/**
 * Decode named character references once after `marked` handles numeric references.
 *
 * @param text - Text token with numeric references already decoded by `marked`.
 * @returns Plain text with complete named references decoded.
 */
function decodeEntities(text: string): string {
  return text.replace(/&[a-z][a-z\d]+;/gi, (entity) => {
    entityDecoder ??= document.createElement('template');
    // only matched references enter this inert attribute; unknown names cannot decode a shorter prefix
    entityDecoder.innerHTML = `<span title="${entity}"></span>`;
    return entityDecoder.content.firstElementChild!.getAttribute('title')!;
  });
}

/**
 * Convert inline lexer tokens into inert nodes.
 *
 * @param tokens - Inline tokens from `marked`; raw HTML tokens are kept as visible text.
 * @returns Inline nodes in source order.
 */
function toInlines(tokens: Token[] | undefined): MarkdownInline[] {
  return (tokens ?? []).flatMap<MarkdownInline>((item) => {
    const token = item as MarkedToken;
    switch (token.type) {
      case 'text':
        return token.tokens
          ? toInlines(token.tokens)
          : [{ type: 'text', text: token.escaped ? token.text : decodeEntities(token.text) }];
      case 'escape':
      case 'html':
        return [{ type: 'text', text: token.text }];
      case 'codespan':
        return [{ type: 'code', text: token.text }];
      case 'br':
        return [{ type: 'break' }];
      case 'strong':
        return [{ type: 'strong', children: toInlines(token.tokens) }];
      case 'em':
        return [{ type: 'emphasis', children: toInlines(token.tokens) }];
      case 'del':
        return [{ type: 'delete', children: toInlines(token.tokens) }];
      case 'link':
      case 'image':
        // autolinks already show their address as the label
        return [{ type: 'link', url: token.href === token.text ? '' : token.href, children: toInlines(token.tokens) }];
      default:
        return 'text' in item && typeof item.text === 'string' ? [{ type: 'text', text: item.text }] : [];
    }
  });
}

/**
 * Convert block lexer tokens into inert nodes.
 *
 * @param tokens - Block tokens from `marked`; spacing and link definitions are omitted.
 * @returns Blocks in source order.
 */
function toBlocks(tokens: Token[]): MarkdownBlock[] {
  return tokens.flatMap<MarkdownBlock>((item) => {
    const token = item as MarkedToken;
    switch (token.type) {
      case 'paragraph':
        return [{ type: 'paragraph', children: toInlines(token.tokens) }];
      case 'text':
        // tight list items hold bare text blocks with their own inline tokens
        return [{ type: 'paragraph', children: toInlines(token.tokens ?? [token]) }];
      case 'heading':
        return [{ type: 'heading', level: token.depth, children: toInlines(token.tokens) }];
      case 'code':
        return [{ type: 'code', language: token.lang ?? '', text: token.text }];
      case 'blockquote':
        return [{ type: 'quote', children: toBlocks(token.tokens) }];
      case 'list':
        return [
          {
            type: 'list',
            ordered: token.ordered,
            start: typeof token.start === 'number' ? token.start : 1,
            items: token.items.map((entry) => {
              const blocks = toBlocks(entry.tokens);
              if (entry.task) {
                const mark: MarkdownInline = { type: 'text', text: entry.checked ? '☑ ' : '☐ ' };
                if (blocks[0]?.type === 'paragraph') blocks[0].children.unshift(mark);
                else blocks.unshift({ type: 'paragraph', children: [mark] });
              }
              return blocks;
            })
          }
        ];
      case 'table':
        return [
          {
            type: 'table',
            align: token.align,
            head: token.header.map((cell) => toInlines(cell.tokens)),
            rows: token.rows.map((row) => row.map((cell) => toInlines(cell.tokens)))
          }
        ];
      case 'hr':
        return [{ type: 'rule' }];
      case 'html':
        return [{ type: 'paragraph', children: [{ type: 'text', text: token.text.trimEnd() }] }];
      case 'space':
      case 'def':
      case 'checkbox':
        return [];
      default:
        return 'text' in item && typeof item.text === 'string'
          ? [{ type: 'paragraph', children: [{ type: 'text', text: item.text }] }]
          : [];
    }
  });
}

/**
 * Parse assistant Markdown into inert nodes for component rendering.
 *
 * Uses the `marked` lexer with GitHub Flavored Markdown and chat-style line breaks, without generating HTML. Links
 * and images are kept as text with their targets, so model output cannot load resources or navigate the
 * application.
 *
 * @param source - Complete or partially streamed Markdown.
 * @returns Blocks in source order, or the source as plain text if the lexer rejects it.
 */
export function parseMarkdown(source: string): MarkdownBlock[] {
  try {
    // a fresh options object keeps the lexer from sharing tokenizer state across calls
    return toBlocks(Lexer.lex(source, { ...getDefaults(), breaks: true }));
  } catch {
    return [{ type: 'paragraph', children: [{ type: 'text', text: source }] }];
  }
}
