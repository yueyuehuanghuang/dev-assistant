"""Fixed, fixture-backed tasks. Replay validates the harness; --live evaluates a model."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch

from agent import run_agent
from evidence import sources_for_turn
from tool_runtime import TOOL_MODELS

ROOT = Path(__file__).resolve().parent


def canonical_call(name, arguments):
    model = TOOL_MODELS.get(name)
    if model:
        try:
            arguments = model.model_validate(arguments).model_dump()
        except ValueError:
            pass
    return {'name': name, 'arguments': arguments}


def supported_url(url, evidence):
    if url in evidence:
        return True
    target = urlsplit(url)
    line = re.fullmatch(r'L([1-9][0-9]*)(?:-L([1-9][0-9]*))?', target.fragment)
    if not line or '/blob/' not in target.path:
        return False
    low, high = int(line[1]), int(line[2] or line[1])
    for source in evidence:
        parsed = urlsplit(source)
        span = re.fullmatch(r'L([1-9][0-9]*)(?:-L([1-9][0-9]*))?', parsed.fragment)
        if span and target._replace(fragment='') == parsed._replace(fragment=''):
            if int(span[1]) <= low <= high <= int(span[2] or span[1]):
                return True
    return False


async def evaluate_case(case, live=False, recorded=None):
    observed = []
    results = []

    async def execute(call, pins=None):
        arguments = json.loads(call.function.arguments)
        observed.append({'name': call.function.name, 'arguments': arguments})
        actual = canonical_call(call.function.name, arguments)
        match = next((t for t in case['tools'] if canonical_call(t['name'], t['arguments']) == actual), None)
        result = match['result'] if match else {'ok': False, 'error': 'Fixture does not contain this tool or arguments.'}
        results.append(result)
        return json.dumps(result)

    responses = []
    for index, tool in enumerate(recorded['observed_tools'] if recorded else case['tools']):
        responses.append({'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': f'eval_{index}', 'type': 'function', 'function': {
                'name': tool['name'], 'arguments': json.dumps(tool['arguments'])}}]})
    responses.append({'role': 'assistant', 'content': recorded['answer'] if recorded else case['replay_answer']})
    try:
        with patch.dict('os.environ', {'TOOL_TRANSPORT': 'direct'}), patch('agent.execute_tool', execute):
            if live:
                result = await run_agent([{'role': 'user', 'content': case['prompt']}])
            else:
                with patch('agent.request_completion', AsyncMock(side_effect=responses)):
                    result = await run_agent([{'role': 'user', 'content': case['prompt']}])
        answer = result.answer
        expected = Counter(json.dumps(canonical_call(t['name'], t['arguments']), sort_keys=True) for t in case['tools'])
        actual = Counter(json.dumps(canonical_call(t['name'], t['arguments']), sort_keys=True) for t in observed)
        matched = sum((expected & actual).values())
        precision = matched / sum(actual.values()) if actual else float(not expected)
        recall = matched / sum(expected.values()) if expected else float(not actual)
        citations = re.findall(r'https://github\.com/[^\s)\]<>]+', answer)
        evidence = {s['url'] for s in sources_for_turn([{'role': 'tool', 'content': json.dumps(r)} for r in results])}
        supported = all(supported_url(url, evidence) for url in citations)
        required_citations = all(url in citations and url in evidence for url in case.get('required_urls', []))
        content_ok = all(term.lower() in answer.lower() for term in case['answer_terms'])
        passed = actual == expected and content_ok and supported and required_citations
        return {'id': case['id'], 'passed': passed, 'tool_precision': precision, 'tool_recall': recall,
                'answer_terms_ok': content_ok, 'citations_supported': supported,
                'required_citations_present': required_citations, 'observed_tools': observed, 'answer': answer}
    except Exception as exc:
        return {'id': case['id'], 'passed': False, 'error_type': type(exc).__name__}


async def run(live=False, output=None, rescore=None):
    cases = json.loads((ROOT / 'evals' / 'cases.json').read_text(encoding='utf-8'))
    previous = json.loads(Path(rescore).read_text(encoding='utf-8')) if rescore else None
    recorded = {r['id']: r for r in previous['cases']} if previous else {}
    rows = []
    for case in cases:
        record = recorded.get(case['id'])
        if previous and (not record or 'answer' not in record):
            rows.append(record or {'id': case['id'], 'passed': False, 'error_type': 'MissingRecordedCase'})
        else:
            rows.append(await evaluate_case(case, live, record))
    mode = 'rescored-recorded-run' if previous else ('live-model-fixture-tools' if live else 'scripted-replay')
    report = {'mode': mode,
              'created_at': datetime.now(timezone.utc).isoformat(),
              'model': previous.get('model') if previous else (os.getenv('LLM_MODEL') if live else None),
              'rescore_source': str(rescore) if rescore else None,
              'model_evaluated': previous.get('model_evaluated', False) if previous else live,
              'passed': sum(r['passed'] for r in rows), 'total': len(rows),
              'limitations': 'Exact-call and keyword checks only; no semantic correctness or hallucination guarantee. Replay is not model quality evidence.',
              'cases': rows}
    target = Path(output) if output else ROOT / 'output' / ('eval-rescored.json' if previous else ('eval-live.json' if live else 'eval-replay.json'))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"{report['mode']}: {report['passed']}/{report['total']}; report: {target}")
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true', help='Call configured paid model; GitHub tools remain fixed fixtures.')
    mode.add_argument('--rescore', help='Re-score recorded tool calls/answers with current fixtures; no model calls. Fixtures must be unchanged.')
    parser.add_argument('--output')
    args = parser.parse_args()
    report = asyncio.run(run(args.live, args.output, args.rescore))
    raise SystemExit(0 if report['passed'] == report['total'] else 1)
