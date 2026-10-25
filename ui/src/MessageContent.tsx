import { useState } from 'react';
import type { ComponentProps } from 'react';
import Markdown from 'react-markdown';
import type { Components, ExtraProps } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import Icon from './Icon';

export function CopyButton({ text, label, showLabel = false }: { text: string; label: string; showLabel?: boolean }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  async function copy() {
    try { await navigator.clipboard.writeText(text); setState('copied'); }
    catch { setState('failed'); }
  }
  return <span className="copy-control">
    <button type="button" className="copy-button" aria-label={label} title={label} onClick={copy}>
      <Icon name={state === 'copied' ? 'check' : 'copy'} size={16} />
      {showLabel ? <span>{state === 'copied' ? 'Скопировано' : 'Копировать'}</span> : null}
    </button>
    <span role="status" className={state === 'failed' ? 'copy-error' : 'sr-only'}>
      {state === 'failed' ? 'Не удалось скопировать. Выделите текст вручную.' : state === 'copied' ? 'Скопировано' : ''}
    </span>
  </span>;
}

function CodeBlock({ node, children }: ComponentProps<'pre'> & ExtraProps) {
  const code = node?.children.find(child => child.type === 'element' && child.tagName === 'code');
  if (!code || code.type !== 'element') return <pre tabIndex={0}>{children}</pre>;
  const text = code.children.map(child => child.type === 'text' ? child.value : '').join('');
  const match = /language-([\w#+.-]+)/.exec(String(code.properties.className ?? ''));
  return <div className="code-block">
    <div className="code-heading"><span><Icon name="code" size={15} />{match?.[1] ?? 'Обычный текст'}</span>
      <CopyButton text={text} label="Копировать код" showLabel /></div>
    <pre tabIndex={0}><code>{text}</code></pre>
  </div>;
}

const components: Components = {
  pre: CodeBlock,
  a: ({ href, children }) => href
    ? <a href={href} target="_blank" rel="noreferrer noopener">{children}</a> : <span>{children}</span>,
  // Images displayed in chat must come from actual CAD image events, never remote Markdown URLs.
  img: ({ alt }) => <span className="muted">{alt ?? 'Изображение'}</span>,
  table: ({ children }) => <div className="table-scroll" tabIndex={0}><table>{children}</table></div>,
  h1: 'h3', h2: 'h3',
};
const plugins = [remarkGfm];

export default function MessageContent({ text }: { text: string }) {
  return <div className="markdown"><Markdown remarkPlugins={plugins} components={components} skipHtml>{text}</Markdown></div>;
}
