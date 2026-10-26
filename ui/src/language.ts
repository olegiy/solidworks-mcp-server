export type Language = 'ru' | 'en';

const KEY = 'cadnext.v1.language';

export const languages: { value: Language; label: string }[] = [
  { value: 'ru', label: 'Русский' },
  { value: 'en', label: 'English' },
];

export function detectLanguage(): Language {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === 'ru' || saved === 'en') return saved;
  } catch { /* Storage can be unavailable. */ }
  try {
    const primary = navigator.languages?.[0] ?? navigator.language ?? '';
    return primary.toLowerCase().startsWith('ru') ? 'ru' : 'en';
  } catch {
    return 'ru';
  }
}

export function storeLanguage(language: Language) {
  try { localStorage.setItem(KEY, language); } catch { /* The current tab still works. */ }
}

export function acceptLanguage(language: Language) {
  return language === 'ru' ? 'ru-RU,ru;q=0.9,en;q=0.5' : 'en-US,en;q=0.9,ru;q=0.5';
}

export function tr(ru: string, en: string) {
  return detectLanguage() === 'en' ? en : ru;
}
