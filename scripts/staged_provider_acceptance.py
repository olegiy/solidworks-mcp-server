"""Explicit live agent acceptance; generated Python is recorded without editing."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from cadnext.agent import Agent
from cadnext.mcp import SolidWorksMcp
from cadnext.provider import Provider
from cadnext.settings import SettingsStore


class StopOnCadError:
    def __init__(self, cad, stop):
        self.cad, self.stop = cad, stop

    async def tools(self):
        return await self.cad.tools()

    async def call(self, name, arguments):
        result = await self.cad.call(name, arguments)
        if not result["ok"]:
            self.stop()
        return result


def prompts(folder):
    return {
        "bracket": (
            "Построй в НОВОЙ тестовой детали листовой кронштейн: толщина 2 мм, внутренний радиус "
            "сгиба 2 мм, наружные полки 60×40 мм, длина 80 мм. На середине 60-мм полки два "
            "сквозных отверстия Ø9 мм, по 20 мм от торцов. Нужны листовой элемент, развёртка, "
            "управляющие размеры/связи и понятное дерево. Проверь фактические размеры, цилиндры "
            "отверстий и положение. Затем измени длину 80→100 мм, перестрой, проверь, что оба "
            "отверстия остаются в 20 мм от торцов. Сохрани как " + str(folder / "agent-bracket.sldprt")),
        "bushing": (
            "Построй в НОВОЙ тестовой детали втулку ВРАЩЕНИЕМ: наружный Ø30 мм, внутренний Ø16 мм, "
            "длина 40 мм, две НАРУЖНЫЕ торцевые фаски 1×45°. Одно тело, управляющие размеры, "
            "понятные имена элементов. Проверь фактические цилиндры, длину, размеры и угол фасок. "
            "Сохрани как " + str(folder / "agent-bushing.sldprt")),
        "assembly": (
            "Создай НОВУЮ тестовую сборку втулки с валом. Используй только тестовую втулку из "
            + str(folder / "agent-bushing.sldprt") + ". Создай НОВУЮ деталь вала Ø15,8×60 мм, "
            "сохрани в " + str(folder / "agent-shaft.sldprt") + ". В сборке втулка закреплена, "
            "вал свободен; добавь концентрическое сопряжение и совпадение торцов. У вала должно "
            "остаться только вращение вокруг оси. Прочитай компоненты, преобразования и сопряжения. "
            "После проверки измени наружный диаметр втулки 30→34 мм, перестрой деталь и сборку, "
            "проверь геометрию и сохранение сопряжений. Сохрани изменённую тестовую втулку и сборку "
            "в " + str(folder / "agent-assembly.sldasm")),
    }


async def run_cases(cad, provider, store, folder, cases):
    agent = None
    guarded = StopOnCadError(cad, lambda: agent.stop(agent._active.id))
    agent = Agent(guarded, settings=store)
    session_id = "live-acceptance-" + uuid4().hex
    report = {"profile": provider.profile.name, "model": provider.profile.model,
              "status": "incomplete", "runs": []}
    try:
        for case in cases:
            text = ("Живая приёмка. Сначала задай план этапов; изучи локальные API и примеры. "
                    "Все изменения только в НОВЫХ тестовых документах и указанных файлах. "
                    "Не изменяй другие открытые документы. При любой ошибке остановись; "
                    "ручная правка сгенерированного кода запрещена. " + prompts(folder)[case])
            run = agent.start(session_id=session_id, provider=provider, message=text,
                              language="ru", max_calls=80)
            with (folder / f"{case}-events.jsonl").open("w", encoding="utf-8") as stream:
                async for event in agent.events(run.id):
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    stream.flush()
                    if event["type"] in {"status", "tool_start", "tool_result"}:
                        print(json.dumps({"case": case, "seq": event["seq"], "type": event["type"],
                                          "name": event["data"].get("name"), "status": run.status,
                                          "calls": run.call_count}, ensure_ascii=False), flush=True)
            await agent.wait(run.id)
            snapshot = run.snapshot()
            (folder / f"{case}-run.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            report["runs"].append({"case": case, "status": run.status,
                                    "verification": run.verification, "call_count": run.call_count})
            if run.status != "completed":
                break
        else:
            report["status"] = "completed_requires_acceptance_review"
    finally:
        await agent.aclose()
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


async def main():
    parser = argparse.ArgumentParser(description="Живая приёмка через настроенного агента.")
    parser.add_argument("--mutate-new-documents", action="store_true")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=["all", "bracket", "bushing", "assembly"], default="all")
    args = parser.parse_args()
    if not args.mutate_new_documents:
        parser.error("Требуется --mutate-new-documents.")
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        parser.error("Каталог результатов должен быть пустым.")
    store = SettingsStore()
    profile = next(p for p in store.list() if p.id == args.profile_id)
    if not store.python_execution_allowed:
        parser.error("Python не разрешён в настройках; приёмка не меняет разрешения.")
    provider = Provider(profile, store.secret(profile.id))
    try:
        async with SolidWorksMcp() as cad:
            report = await run_cases(cad, provider, store, folder,
                                     ["bracket", "bushing", "assembly"] if args.case == "all" else [args.case])
    finally:
        await provider.aclose()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed_requires_acceptance_review" else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
