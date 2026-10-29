"""Temporary synthetic memory acceptance; --live calls the configured chat model."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import run_agent
from evidence import sources_for_turn
from memory import ConversationStore


def tool(identifier, name, arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identifier, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


async def main(live=False):
    with TemporaryDirectory() as directory:
        store = ConversationStore(Path(directory) / 'memory.db')
        seed = AsyncMock(return_value='Synthetic fixture response, not a model conclusion.')
        identifier, _ = await store.chat('本轮诊断标记是 ORCHID-7429，目前没有确认原因。', None, seed, memory_project='memory-acceptance')
        await store.chat('记住：本项目统一使用 Python 3.12。', identifier, seed)
        for number in range(12):
            await store.chat(f'合成后续轮次 {number}', identifier, seed)
        candidate = store.repository.list_memories(identifier)[0]
        store.repository.change_memory(identifier, candidate['id'], 'approve', candidate['version'])

        async def generate(messages):
            return await run_agent(messages)

        async def exercises():
            other, answer = await store.chat('请先调用 search_memory 查找 Python 版本，再根据已确认的项目记忆回答并引用来源。', None, generate, memory_project='memory-acceptance')
            _, historical = await store.chat('请先调用 search_history 搜索 ORCHID-7429，再调用 read_history 读取命中的原始轮次，最后复述标记并引用历史来源。', identifier, generate)
            return other, answer, historical

        if live:
            other, answer, historical = await exercises()
        else:
            responses = [tool('recall-memory', 'search_memory', {'query': 'Python'}), {'role': 'assistant', 'content': 'Python 3.12'},
                         tool('recall-history', 'search_history', {'query': 'ORCHID-7429'}), tool('read-original', 'read_history', {'turn_number': 1}),
                         {'role': 'assistant', 'content': 'ORCHID-7429'}]
            with patch.dict('os.environ', {'TOOL_TRANSPORT': 'direct'}), patch('agent.request_completion', AsyncMock(side_effect=responses)):
                other, answer, historical = await exercises()
        memory_sources = sources_for_turn(store.get_history(other))
        history_sources = sources_for_turn(store.get_history(identifier))
        state = store.get_memory_state(identifier)
        passed = ('3.12' in answer and 'ORCHID-7429' in historical and bool(memory_sources) and bool(history_sources)
                  and state['archived_turns'] == 15 and bool(state['summary']))
        report = {'mode': 'live-model' if live else 'scripted-replay', 'passed': passed,
                  'synthetic_fixtures': True, 'temporary_database': True,
                  'archived_turns': state['archived_turns'], 'active_turns': sum(m['role'] == 'user' for m in store.get_history(identifier)),
                  'summary_mode': 'extractive', 'cross_session_answer': answer, 'historical_answer': historical,
                  'memory_sources': memory_sources, 'history_sources': history_sources}
        target = ROOT / 'output' / ('memory-smoke-live.json' if live else 'memory-smoke-replay.json')
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return passed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use the configured model; incurs API usage.')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.live)) else 1)
