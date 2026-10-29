"""Real configured embeddings smoke test using temporary, synthetic documents."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
from knowledge import KnowledgeStore
from knowledge_service import KnowledgeService


async def main(ask=False):
    with TemporaryDirectory() as directory:
        service = KnowledgeService(KnowledgeStore(Path(directory) / 'knowledge.db'))
        kb = service.create('临时向量检索验收')['id']
        service.ingest(kb, '身份认证.md', '访问令牌会在签发后十五分钟失效。需要重新登录或使用刷新令牌换取新的访问令牌。'.encode())
        service.ingest(kb, '备份规则.md', '备份文件保留七天后删除。归档介质每月检查一次。'.encode())
        revision, chunks, config = service.start_index(kb)
        service.index_job(kb, revision, chunks, config)
        if service.get(kb)['index_state'] != 'ready':
            raise RuntimeError(service.get(kb)['index_error'])
        cases = []
        for query in ['凭证可以持续多长时间？', '授权凭据何时到期？']:
            keyword = service.search(kb, query, mode='bm25')
            vector = service.search(kb, query, mode='vector')
            hybrid = service.search(kb, query)
            relevant_found = any(r['name'] == '身份认证.md' for r in vector['results']) and any(r['name'] == '身份认证.md' for r in hybrid['results'])
            cases.append({'query': query, 'keyword_matches': keyword['total_matches'],
                          'vector_matches': vector['total_matches'], 'relevant_in_top5': relevant_found,
                          'top1_correct': bool(hybrid['results']) and hybrid['results'][0]['name'] == '身份认证.md',
                          'sources': [{'name': r['name'], 'vector_score': r.get('vector_score')} for r in hybrid['results']]})
        passed = all(c['keyword_matches'] == 0 and c['relevant_in_top5'] for c in cases)
        result = {'embedding_model': config.model, 'dimensions': config.dimensions,
                  'passed': passed, 'criterion': 'Relevant evidence recalled in top5 without lexical overlap; rank-1 quality reported separately.',
                  'retrieval_cases': cases,
                  'synthetic_documents': True, 'chat_model_called': ask}
        if ask:
            from agent import run_agent
            from evidence import sources_for_turn
            answer = await run_agent([{'role': 'user', 'content': '根据知识库回答：' + cases[0]['query'] + ' 请引用资料来源；没有证据则说明不知道。'}], knowledge=(service, kb))
            result['answer'] = answer.answer
            result['sources'] = sources_for_turn(answer.messages)
            result['passed'] = passed and bool(result['sources']) and ('十五' in answer.answer or '15' in answer.answer)
        target = ROOT / 'output' / 'knowledge-smoke.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ask', action='store_true', help='Also call the configured chat model; may incur API cost.')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.ask)) else 1)
