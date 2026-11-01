# Происхождение и лицензия

Основа: [aiforyouself/solidworks-mcp-server](https://github.com/aiforyouself/solidworks-mcp-server),
коммит `2ad21ce7581111d55dcc85db9de32f7366d201f7` («Add video links to README»).
Лицензия MIT, Copyright (c) 2026 AI for You. Полный текст сохранён в `LICENSE`,
исходный коммит — в истории Git этого репозитория.

Из основы сохранён `solidworks_mcp_server.py` и контракт 27 инструментов.
Изменения: подключение только к существующему закреплённому PID, один STA-поток,
блокировка после таймаута, отказ от каскадов изменяющих API, перевод углов,
уникальные временные файлы снимков, безопасные диагностические записи и выключенный
по умолчанию подробный журнал. Исторические комментарии основы сохранены; некоторые
описывают прежнее поведение, текущий контракт закреплён тестами.

Добавлены независимо: пакет `cadnext` (MCP-клиент, настройки, провайдер, приватность,
агент, API и запуск), React-чат, скрипты, тесты и документация. Старый CAD-Agent
не является зависимостью. Унаследованный README заменён описанием нового приложения.

Upstream сообщал о проверках SolidWorks 2024 SP5. В этом репозитории проверены
SolidWorks 2025 revision 33.5.0, новая листовая деталь, изменение/чтение размера,
снимок и SLDPRT/DXF. Живой LLM пока не подтверждён. Факты — `docs/verification.md`.

## Зависимости анализа полигональных моделей

Начиная с ветки `feat/mesh-to-cad`, численная основа устанавливается из wheels,
с закреплёнными версиями в `requirements.lock.txt`:

| Компонент | Версия | Лицензия |
|---|---|---|
| NumPy | 2.5.4 | BSD-3-Clause; дополнительные условия компонентов в wheel |
| SciPy | 1.18.1 | BSD-3-Clause; дополнительные условия компонентов в wheel |
| Trimesh, Michael Dawson-Haggerty | 5.2.0 | MIT |
| Rtree, Sean Gillies, Howard Butler и участники | 1.4.1 | MIT |
| libspatialindex, бинарные DLL wheel Rtree | 2.1.0 | MIT |
| NetworkX | 3.7 | BSD-3-Clause |

Лицензии проверены в метаданных и лицензионных файлах установленных wheels.
NumPy/SciPy включают OpenBLAS (BSD-3-Clause), LAPACK
(BSD-3-Clause-Open-MPI) и GCC runtime
(`GPL-3.0-or-later WITH GCC-exception-3.1`); перечисленные условия не заменяются
лицензией проекта. Остальные уведомления компонентов находятся в поставляемых
`numpy-*.dist-info/licenses/`, `scipy-*.dist-info/LICENSE.txt`,
`trimesh-*.dist-info/licenses/`, `rtree-*.dist-info/licenses/`
и `networkx-*.dist-info/licenses/`. При распространении окружения сохраняйте
полные уведомления и условия бинарных компонентов из соответствующего wheel.

ScanRuler (AGPL-3.0-only) исследовался как источник идей; его код, CAD-ядро,
шрифты и тестовые данные не включены в CAD-Agent-Next.
Результаты проверки зависимостей — [docs/mesh-feasibility.md](docs/mesh-feasibility.md).
