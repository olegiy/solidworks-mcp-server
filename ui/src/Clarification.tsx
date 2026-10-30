import Icon from './Icon';
import MessageContent, { CopyButton } from './MessageContent';
import { useI18n } from './i18n';

export default function Clarification({ runId, questions, text, onReply }: {
  runId: string; questions: string[]; text: string; onReply?: () => void;
}) {
  const { t } = useI18n();
  const titleId = `clarification-${runId}`;
  return <section className="message assistant clarification" role="alert" aria-labelledby={titleId}>
    <div className="clarification-heading">
      <h3 id={titleId}><Icon name="question" size={22} />{t('clarificationTitle')}</h3>
      <CopyButton text={text} label={t('copyAnswer')} />
    </div>
    <p>{t('clarificationHint')}</p>
    {questions.length ? <ol>{questions.map((question, index) =>
      <li key={index}><MessageContent text={question} /></li>)}</ol>
      : <MessageContent text={text} />}
    {onReply ? <button type="button" className="clarification-reply" aria-controls="chat-message"
      onClick={onReply}>{t('clarificationReply')}</button> : null}
  </section>;
}
