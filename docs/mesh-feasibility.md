# Проверка основы STL/OBJ → CAD

Дата: 01.11.2026. Выполнялась задача 1
[плана](superpowers/plans/2026-11-01-mesh-to-cad.md).

## Численный backend

На Windows с Python 3.12.10 установлены готовые wheels: NumPy 2.5.4,
SciPy 1.18.1, Trimesh 5.2.0, Rtree 1.4.1 (libspatialindex 2.1.0), NetworkX 3.7.
Версии закреплены в `requirements.lock.txt`; `pip check` не нашёл конфликтов.
NetworkX понадобился для `Path3D.discrete`: его отсутствие выявилось настоящим
тестом сечения. Полный набор `trimesh[easy]` не устанавливался.

Восемь тестов `tests/test_mesh_dependencies.py` проверяют загрузку ASCII/binary
STL и OBJ из байтов, внешнюю границу и отверстие в сечении, ступенчатую втулку
и расстояние до поверхности большого треугольника. Для точки `[25,25,3]`
над треугольником `[0,0,0]–[100,0,0]–[0,100,0]` получено расстояние 3 мм;
ближайшая вершина не подменяет ближайшую точку поверхности.

Исходный RED: семь тестов отказали из-за отсутствующих пакетов. После установки
выявлена зависимость NetworkX. Тест benchmark также прошёл RED из-за отсутствия
модуля, затем GREEN. Собственные фикстуры `tests/mesh_fixtures.py` создаются
программно, чужие сканы и исходники ScanRuler не используются.

## Ресурсы

```powershell
.venv\Scripts\python.exe -m scripts.mesh_feasibility
```

Каждый размер запускается в отдельном процессе. Эталон — плоская регулярная
сетка 100×100 мм; 500 детерминированных запросов на высоте 3 мм. Измеряются
создание массивов/Trimesh, одно сечение, расстояния; это не benchmark загрузки
больших STL/OBJ или полного сравнения двух произвольных поверхностей.

| Грани | Вершины | Создание, с | Сечение, с | Расстояния, с | Пик памяти, МиБ |
|---|---|---|---|---|---|
| 9 800 | 5 041 | 0.0011 | 0.0086 | 0.0970 | 101.7 |
| 99 458 | 50 176 | 0.0027 | 0.0057 | 0.4983 | 180.4 |
| 999 698 | 501 264 | 0.0220 | 0.0358 | 4.8520 | 925.0 |

Во всех случаях ошибка относительно 3 мм равна нулю в полученных float64.
Память — `GetProcessMemoryInfo.PeakWorkingSetSize`, включая DLL и индексы.

Миллион граней почти исчерпывает 1 ГиБ уже на одной сетке и небольшой выборке.
Начальный предел загрузчика снижен до 250 000 граней и 250 000 вершин.
Это ограничение входа, не гарантия памяти произвольной операции. Для двух
поверхностей нужны ограничение процесса и порционные запросы. Полная выборка
50 000 точек и произвольные сетки пока не проверены.

## SolidWorks: подтверждённые сигнатуры

Локальная `sldworks.tlb` SolidWorks 2025 (major 33) прочитана через существующий
`com_call`/STA, без `_app()`, доступа к документам и запуска MCP.

| Член | COM-контракт |
|---|---|
| `IBody2.GetTessellation(FaceList)` | Вход VARIANT, результат Dispatch |
| `ITessellation.CurveChordTolerance` | double, чтение/запись |
| `ITessellation.SurfacePlaneTolerance` | double, чтение/запись |
| `ITessellation.ImprovedQuality` | bool, чтение/запись |
| `ITessellation.Tessellate()` | bool; вызывать через `GET` |
| `GetFacetCount()` / `GetVertexCount()` | int |
| `GetFacetFins(FacetId)` | VARIANT с ID рёбер facet |
| `GetFinVertices(FinId)` | VARIANT с парой ID вершин |
| `GetVertexPoint(VertexId)` | VARIANT с координатами |

```powershell
.venv\Scripts\python.exe -m scripts.mesh_feasibility --type-library 'C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\sldworks.tlb'
```

Через Context7 проверена официальная справка 2026 для
[CurveChordTolerance](https://help.solidworks.com/2026/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ITessellation~CurveChordTolerance.html),
[SurfacePlaneTolerance](https://help.solidworks.com/2026/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ITessellation~SurfacePlaneTolerance.html),
[ImprovedQuality](https://help.solidworks.com/2026/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ITessellation~ImprovedQuality.html)
и методов получения вершин. `CurveChordTolerance` ограничивает расстояние хорды
до исходной кривой; `ImprovedQuality=True` включает данные для анализа.
Краткая справка `SurfacePlaneTolerance` не удостоверяет двустороннюю границу
ошибки всей поверхности. Наличие метода в TLB не доказывает работу на теле.

## Непройденный барьер точности

**Точность CAD-триангуляции пока не подтверждена.** Получение сетки тела,
метровые координаты, readback допусков, ошибки Tessellate и отклонение
на независимом аналитическом цилиндре ещё не проверялись. Изменений документов
не выполнялось. Настройка допуска не доказывает достигнутую погрешность.

Измерение исходников может развиваться дальше. Для задачи 6 допустим только
исследовательский экспорт с `precision_known=false`, пока не появятся основания
для другой отметки. Сравнение при неизвестной CAD-точности даёт `unknown`,
никогда `pass`. Bbox и скриншот этот барьер не заменяют.

Условия зависимостей описаны в [UPSTREAM.md](../UPSTREAM.md). Лицензии `.dist-info`
сохраняются при установке wheels; MIT проекта их не заменяет.
