# Ускорение CAD-Agent Next — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Сократить лишние циклы LLM, контекст и передачу состояния; добавить измеряемые эксперименты.

**Architecture:** Полная локальная история и один CAD-исполнитель сохраняются.
Провайдер строит компактную проекцию; UI применяет последовательные события.
Измерения и экспериментальные сетевые режимы отделены от исполнения CAD.

**Tech Stack:** Python 3.12, httpx, Pillow, FastAPI/SSE, React/TypeScript, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-26-performance-design.md`

## Global Constraints

- Один STA COM, 27 MCP-инструментов, uncertain до перезапуска, никаких повторов.
- `Provider.complete(messages, tools)` получает исходную локальную историю.
- Профиль неизменяем; фильтрация текста и восстановление аргументов сохраняются.
- Секреты только Credential Manager; диагностика только числовая.
- `.venv\Scripts\python.exe`; интерфейс ru/en, документация по-русски.

## Review Focus

- Старый снимок, выданный пользователем за чертёж, не должен удаляться как CAD-снимок.
- Дельты дерева при удалении/перестановке и неуспешные проверки остаются точными.
- Обрыв/дубликат/пропуск SSE не повторяет CAD и не теряет изображения.
- Оборванный stream не выдаёт частичный tool call как готовую команду.
- Остановка подготовки не отменяет Run или отправленный COM.

### Task 1: Метрики и каталог

**Files:** `cadnext/provider.py`, `cadnext/agent.py`, `cadnext/mcp.py`, `tests/test_performance.py`.

**Interfaces:** `Provider.last_metrics` содержит только числовые поля; `Run.metrics`
публикует model/cad/verification время и число вызовов, метрики модели идут
отдельным событием `model_metrics`. Каталог кэшируется на одну MCP-сессию.

- [x] RED: метрики реального исходящего HTTP-запроса, отсутствующий usage, ошибки без утечки, раздельный readback.
- [x] GREEN: измерения perf_counter, точные байты json-тела, валидированный usage.
- [x] Проверить `pytest tests/test_performance.py tests/test_provider.py tests/test_activity.py -q`.

### Task 2: Компактная проекция и изображения

**Files:** `cadnext/context.py`, `cadnext/images.py`, `cadnext/provider.py`, `cadnext/agent.py`, `tests/test_context.py`.

**Interfaces:** `model_context(messages)` возвращает новую проекцию, удаляя
служебную метку `_cadnext`; `optimize_image_url(url)` сохраняет читаемые пиксели.
Агент помечает только свои image-сообщения как `source=screenshot` и их набор видов.

- [x] RED: оригиналы/paths/tools неизменны; старые screenshot-наборы вытесняются, чертежи остаются; точные дельты дерева; белые поля/прозрачность/краевые метки.
- [x] GREEN: проекция внутри Provider до PrivacyContext; без дополнительных LLM.
- [x] Проверить `pytest tests/test_context.py tests/test_attachments.py tests/test_privacy.py tests/test_provider.py -q`.

### Task 3: Меньше циклов модели

**Files:** `cadnext/agent.py`, `solidworks_mcp_server.py`, `docs/agent.md`.

**Interfaces:** Существующие native tool calls и MCP-сигнатуры без изменений.

- [x] Уточнить ru/en инструкции: несколько известных действий в одном ответе, ожидание зависимых результатов, снимки после этапов, readback числовых изменений.
- [x] Проверить существующие batch/verification/stop тесты и каталог из 27 инструментов.

### Task 4: Событийное обновление UI

**Files:** `cadnext/agent.py`, `cadnext/app.py`, `ui/src/runEvents.ts`, `ui/src/App.tsx`, `ui/src/api.ts`, API/UI tests.

**Interfaces:** События сохраняют seq/type/at/data и добавляют `state` без истории;
SSE heartbeat каждые 2 с без seq. `Run.snapshot()` остаётся совместимым.

- [x] RED: состояние на событиях, replay, heartbeat без CAD; браузер обновляет ход и изображения без ежесекундных GET.
- [x] GREEN: reducer по seq; полный snapshot при запуске, разрыве или gap; heartbeat поддерживает индикатор связи.
- [x] Проверить API pytest; собрать UI и выполнить Playwright.

### Task 5: Измеряемые эксперименты

**Files:** `cadnext/provider.py`, `cadnext/preparation.py`, `scripts/benchmark_performance.py`, соответствующие pytest.

**Interfaces:** `Provider.complete(..., stream=False, compact=True)` — необязательные
keyword-only параметры; `prepare_images(provider, messages, is_current, stop)`
возвращает входной fingerprint и проверенные заметки без исполняемых команд.
Общий semaphore Provider ограничивает все completion-запросы двумя.

- [x] RED: фрагменты SSE, usage, truncation/error, максимум два запроса, отмена/устаревший вход, сопоставимые пары benchmark.
- [x] GREEN: собрать и валидировать stream целиком; прототип tool-free подготовки; CLI offline/live без CAD, секретов в отчёте и истории на диске.
- [x] Проверить тесты, выполнить offline-бенчмарк по пяти сценариям; live только при однозначно выбранном профиле.

### Task 6: Приёмка и документация

**Files:** `docs/performance.md`, `docs/verification.md`, `docs/app.md`, `docs/image-attachments.md`.

- [x] Описать метрики, протокол, лимиты, команды сравнения и границы доказательств.
- [x] Полный pytest, ruff, UI build и Playwright.
- [x] Независимое финальное ревью и исправление существенных замечаний.
