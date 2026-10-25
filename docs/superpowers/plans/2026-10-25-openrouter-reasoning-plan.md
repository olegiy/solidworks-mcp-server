# OpenRouter Reasoning Levels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Получать поддерживаемые уровни рассуждения OpenRouter из каталога выбранной модели и показывать их в настройках и чате с безопасным fallback.

**Architecture:** `Provider` нормализует метаданные `/models` в отдельный каталог моделей. Loopback API отдаёт `/api/models` для имён моделей выбранного API и `/api/reasoning?model=...` для локального сопоставления с публичным каталогом OpenRouter без авторизации. React использует метаданные для динамического списка уровней и сбрасывает несовместимый выбор.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest; React/TypeScript, Playwright, Vite.

**Spec:** `docs/superpowers/specs/2026-10-25-openrouter-reasoning-design.md`

## Global Constraints

- Имена моделей запрашиваются у адреса текущего подключения. Уровни для всех API дополнительно берутся из публичного OpenRouter без ключей и истории.
- API-ключи не попадают в JSON-ответы, логи и сообщения модели.
- Пробные запросы к моделям для определения уровней запрещены.
- Неизвестные или некорректные поля каталога игнорируются; ручной ввод модели сохраняется.
- Для не-OpenRouter API используется точное или уникальное совпадение имени в публичном каталоге; при отсутствии совпадения остаётся fallback с пояснением.
- Все пользовательские строки и отчёты остаются на русском языке.

## Review Focus

- Модель без поля `reasoning` должна сохранить ручной выбор и fallback.
- Модель с `mandatory: true` должна выбрать допустимый уровень и не предлагать «Без рассуждения».
- Смена модели не должна отправить старый уровень в следующий запрос.
- Ошибка каталога не должна удалять введённое имя модели или секрет.
- Каталог OpenRouter не должен расширять разрешённые значения произвольными строками.

### Task 1: Backend catalog normalization

**Files:**
- Modify: `cadnext/provider.py`
- Test: `tests/test_provider.py`

**Interfaces:**
- Add `Provider.list_model_catalog() -> dict` returning `{"models": [{"id": str, "reasoning": {"mandatory": bool, "default_effort": str | None, "supported_efforts": list[str] | None}}]}`.
- Keep `Provider.list_models() -> list[str]` as a compatibility wrapper over the catalog.

- [x] Write failing tests for OpenRouter reasoning metadata, malformed metadata, and non-OpenRouter fallback.
- [x] Run the focused tests and observe the expected failures.
- [x] Implement host-gated normalization of `reasoning.supported_efforts`, `default_effort`, and `mandatory`; only allow existing `ReasoningEffort` values.
- [x] Run focused provider tests until green.

### Task 2: API catalog response

**Files:**
- Modify: `cadnext/app.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- `/api/models` GET and POST return `{"models": [...]}` with `reasoning` metadata per model; the selected profile model remains in `/api/profiles`, and existing clients may still receive IDs through the provider compatibility method only.

- [x] Add failing API tests proving catalog metadata is returned and keys are absent.
- [x] Implement endpoint serialization using `Provider.list_model_catalog()`.
- [x] Update existing API fixture expectations and run app tests.

### Task 3: Dynamic settings and composer UI

**Files:**
- Modify: `ui/src/api.ts`
- Modify: `ui/src/ModelPicker.tsx`
- Modify: `ui/src/App.tsx`
- Modify: `ui/tests/models.spec.ts`

**Interfaces:**
- `ModelCatalog` contains models with `id` and optional reasoning metadata.
- `ModelPicker` emits the selected model and optional selected model metadata to its parent.

- [x] Add failing Playwright tests for OpenRouter levels, mandatory reasoning, model switching, and fallback responses.
- [x] Implement catalog parsing, dynamic options, labels, and reset of unsupported selections.
- [x] Keep profile persistence through the existing `reasoning_effort` field and preserve manual model input.
- [x] Run the focused Playwright tests until green.

### Task 4: Documentation and full verification

**Files:**
- Modify: `docs/agent.md`
- Modify: `docs/verification.md`
- Modify: `.superpowers/progress.md` (ignored ledger)

- [x] Document OpenRouter metadata, fallback behavior, and no-probe rule.
- [x] Run full pytest, Playwright, UI build, Ruff, and `git diff --check`.
- [x] Review the final diff for secret leakage and confirm no live CAD/LLM calls.

### Исправление: уровни для сторонних API

- [x] Воспроизвести причину на существующих профилях: все используют сторонние API.
- [x] Добавить RED→GREEN тесты отдельного публичного каталога, точного/уникального
  сопоставления, неоднозначности и неизвестных псевдонимов; не передавать ключи.
- [x] Подключить автоматическую загрузку уровней в настройках и чате для любого API;
  показывать источник или причину стандартного списка.
- [x] Исправить сохранение и отправку до окончания загрузки метаданных; проверить
  настоящие уровни существующих профилей без изменения данных и CAD-вызовов.
