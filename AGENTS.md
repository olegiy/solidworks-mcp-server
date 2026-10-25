# AGENTS.md

## Что это за проект

- Минимальная пересборка `aiforyouself/solidworks-mcp-server` (MIT, upstream-коммит `2ad21ce7581111d55dcc85db9de32f7366d201f7`); самостоятельный репозиторий рядом со старым CAD-Agent, импорт `cad_agent` запрещён.
- Источники истины: `docs/superpowers/specs/2026-10-25-cad-agent-next-design.md` и `docs/superpowers/plans/2026-10-25-cad-agent-next-implementation.md`. Статус исполнения — `.superpowers/progress.md` (в git не входит).
- Готовы слой MCP (`solidworks_mcp_server.py`, 27 инструментов, `cadnext/mcp.py`), настройки/провайдер/приватность, последовательный агент и задача 4: `cadnext/app.py`, `cadnext/__main__.py`, React-чат в `ui/`. Задача 5 (скрипты запуска и проверка выпуска) ещё не выполнена. Контракты описаны в `docs/models-and-privacy.md`, `docs/agent.md`, `docs/app.md`.
- `README.md` и `README.ru.md` — унаследованный текст upstream про однофайловый сервер; не считай их описанием текущего состояния.

## Команды

- Окружение: `.venv` (Python 3.12.10), версии зафиксированы в `requirements.lock.txt`. Запускай через `.venv\Scripts\python.exe`, а не `python` из PATH.
- Все тесты: `.venv\Scripts\python.exe -m pytest -q`
- Один файл / один тест: `... -m pytest tests/test_mcp.py -q`, `... -m pytest tests/test_mcp.py::test_nested_error_is_failure -q`
- Без SolidWorks: `... -m pytest -q -m "not live"` (маркер `live` объявлен, живых тестов пока нет)
- Линт: `.venv\Scripts\python.exe -m ruff check .`; `solidworks_mcp_server.py` исключён из ruff, его проверяй вручную. Типизации/mypy нет.
- `test_tools_list_over_real_stdio` запускает настоящий сервер через `sys.executable`: pytest должен идти из venv, где установлен `mcp`.
- API: `.venv\Scripts\python.exe -m pytest tests/test_app.py -q`. UI: в `ui/` — `npm ci`, `npm run build`, `npx playwright test`. Playwright запускает настоящий API/агент/провайдер на 8877 с фиктивными LLM/CAD; SolidWorks и ключи не нужны. Перед тестами нужна свежая сборка UI и установленный Chromium (`npx playwright install chromium`).

## Запуск и сборка

- Python-сборки нет. Развёртывание окружения с нуля повторяет текущий `.venv`:
  `python -m venv .venv` затем `.venv\Scripts\python.exe -m pip install -r requirements.lock.txt`.
  Lock-файл — точный срез `.venv` (включая pytest/ruff/fastapi/keyring); `requirements.txt` — только runtime upstream-сервера.
- MCP-сервер можно запустить отдельно по stdio: `.venv\Scripts\python.exe solidworks_mcp_server.py`. Окно он не открывает и к SolidWorks не подключается до первого инструмента; без MCP-клиента просто ждёт JSON-RPC в stdin.
- Для ручного запуска выставляй `$env:PYTHONUTF8=1`: иначе русские строки в stderr PowerShell отображаются как мусор (сами логи пишутся в UTF-8).
- Проверка запуска и каталога без SolidWorks: `.venv\Scripts\python.exe -m pytest tests/test_mcp.py::test_tools_list_over_real_stdio -q`.
- Подключение внешнего MCP-клиента: `command` — полный путь к `.venv\Scripts\python.exe`, `args` — полный путь к `solidworks_mcp_server.py`. `cadnext/mcp.py` делает это сам через `sys.executable` с окружением `PYTHONUTF8=1`, `SW_MCP_CALL_LOG=off`, `LOCALAPPDATA`.
- После сборки UI: `.venv\Scripts\python.exe -m cadnext`, затем открыть `http://127.0.0.1:8876`. Приложение проверяет и резервирует порт до создания MCP; привязка только к loopback. Ctrl+C штатно останавливает агент и MCP, не отменяя начатую COM-операцию. Браузер автоматически пока не открывается.
- `setup.bat`/`scripts/setup.ps1`, `start.bat`/`scripts/start.ps1`, `stop` относятся к задаче 5 и пока отсутствуют. Не выполнять их до реализации.

## Текущее состояние TDD

- План исполняется через TDD: сначала падающие тесты, затем реализация. Задача 2 завершена: её тесты настроек, провайдера и приватности должны проходить; отсутствие этих модулей теперь является регрессией.
- `Profile` неизменяем: запрос удерживает свой снимок настроек. `Provider.complete` сам фильтрует исходящую историю через `PrivacyContext`, сохраняя служебные поля native tools. Передавать ему нужно исходную локальную историю. Перед локальным MCP-вызовом восстанавливай значения аргументов через тот же `PrivacyContext.restore`.
- Согласие действует внутри контекста диалога и профиля; смена адреса API или модели требует нового контекста. Для облачных изображений нужны и `vision`, и отдельное согласие. Живой LLM-провайдер в задаче 2 не проверялся.
- `Agent.start(session_id=..., provider=..., message=..., max_calls=80)` синхронно резервирует единственный активный Run и создаёт задачу в текущем event loop. Ожидание — `await agent.wait(run.id)`, штатная остановка — `agent.stop(run.id)`, завершение — `await agent.aclose()`. Не отменяй приватный `run._task`; отмена ожидания `wait` не отменяет CAD.
- События `agent.events(run.id, after=seq)` возвращаются с возрастающим `seq`; закрытие генератора запрещает следующие шаги, но дожидается исхода уже отправленного COM. HTTP/SSE-адаптер использует `run.snapshot()`, `Last-Event-ID` и событие `update`. Разрыв SSE и `pagehide` вызывают stop. UI передаёт уникальный `request_id`: stop по нему работает даже до получения ответа POST запуска (см. `docs/app.md`).
- Подтверждение Python связано с точным восстановленным кодом и уникальным ID. `confirm` для согласия на внешнего провайдера поддерживает отдельный `images=True`. Контекст приватности закреплён также за диалогом.
- Проверяющие MCP-вызовы входят в лимит и резервируются до мутации. `Run.verification` отдельно от `status`: завершённый прогон может иметь `unverified`. Ошибка/таймаут не повторяются; `uncertain` блокирует новые прогоны до перезапуска. 67 сценариев агента автономны, живые мутации/LLM не проверялись.

## Инварианты COM-слоя (не ломать)

- Любой COM-вызов — только через `com_call`: единственный STA-поток (`ThreadPoolExecutor(max_workers=1)`), сериализация через `_submit_lock`. Не вызывай COM из async или других потоков и не добавляй параллельные вызовы.
- `COM_TIMEOUT = 55` с; по таймауту выставляется `_poisoned`, и все последующие вызовы падают с `ComOutcomeUnknown` до перезапуска процесса. Не сбрасывай флаг и не повторяй операцию автоматически.
- `_app()` подключается только к уже запущенному SolidWorks: нужен ровно один `sldworks.exe`, `GetProcessID` сверяется при каждом обращении; запуск и переключение экземпляра запрещены.
- Мутации без каскадов: неудачный `save_document` не повторяется другим методом (это закреплено тестом), fallback-цепочки изменяющих API запрещены планом.
- `_run()` превращает любое исключение в JSON `{status:"error", error_code:"CAD_ERROR"|"CAD_OPERATION_UNCERTAIN"}`; вложенный `{"status":"error"}` считается ошибкой даже без MCP `isError` (`normalize_result`).
- `cadnext/mcp.py` возвращает `{ok, value, images, error, uncertain}` и «залипает» в `uncertain` до перезапуска; изображения берутся только из MCP image-блоков (`screenshot` возвращает `Image`, а не JSON).
- Инструментов ровно 27: `tests/test_mcp.py` проверяет количество и наличие `execute_python`. Добавление/удаление инструмента — осознанное изменение контракта вместе с тестом.

## Единицы и локализация

- Наружу — мм и градусы, внутри COM — метры и радианы (`_mm`). Угловые размеры (`GetType() == 3`) переводятся через `math.radians` / `math.degrees` и не должны проходить через `_mm`.
- Имена плоскостей зависят от локализации SolidWorks: `create_sketch` по умолчанию использует русское `Спереди`; для английской установки передавай `Front Plane` и т.п.
- Язык: пользовательские строки, описания инструментов, ошибки и документы — по-русски; новые идентификаторы, тесты и комментарии в `cadnext/` — по-английски. Русские комментарии в `solidworks_mcp_server.py` сохраняй.

## Процесс и данные

- MCP-подпроцесс получает урезанное окружение (`PYTHONUTF8=1`, `SW_MCP_CALL_LOG=off`, `LOCALAPPDATA`), cwd — корень репозитория; не рассчитывай на наследование остальных переменных.
- `server.log` и `server_crash.log` пишутся в `%LOCALAPPDATA%\cad_agent_next\logs` (не рядом со скриптом, несмотря на docstring). Журнал вызовов по умолчанию выключен и включается через `SW_MCP_CALL_LOG=<путь>`.
- Секреты — только Windows Credential Manager, namespace `cad_agent_next`; в JSON-настройках, ответах API и логах ключей быть не должно.
- Не изменяй старый CAD-Agent; сохраняй MIT-уведомление и происхождение upstream (`UPSTREAM.md` ещё не создан).
