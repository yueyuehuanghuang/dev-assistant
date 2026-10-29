import math
import os
import json
from urllib.parse import urlsplit

from fastapi import HTTPException

from embeddings import EmbeddingClient, EmbeddingConfig, normalize_vector


class KnowledgeService:
    def __init__(self, store, vectors=None):
        self.store = store
        self.vectors = vectors

    def __getattr__(self, name):
        return getattr(self.store, name)

    def config_status(self):
        try:
            config = EmbeddingConfig.from_env()
            return {'configured': True, 'model': config.model, 'dimensions': config.dimensions,
                    'location': 'local' if urlsplit(config.base_url).hostname in {'localhost', '127.0.0.1', '::1'} else 'remote'}
        except HTTPException:
            return {'configured': False, 'model': None, 'dimensions': None}

    def index_job(self, kb_id, revision, chunks, config):
        try:
            vectors = EmbeddingClient(config).embed([row['name'] + '\n' + row['content'] for row in chunks])
            if self.vectors:
                from uuid import uuid4
                generation = str(uuid4())
                self.vectors.write(kb_id, generation, chunks, vectors, config.contract)
                with self.store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    cursor = db.execute("UPDATE knowledge_bases SET indexed_revision=?,embedding_contract=?,vector_generation=?,index_state='ready',index_error=NULL WHERE id=? AND revision=?",
                        (revision, json.dumps(config.contract, sort_keys=True), generation, str(kb_id), revision))
                    if cursor.rowcount != 1:
                        raise HTTPException(409, '索引期间资料已变化，请重新构建。')
            else:
                self.store.finish_index(kb_id, revision, chunks, vectors, config.contract)
        except HTTPException as exc:
            self.store.fail_index(kb_id, exc.detail)
        except Exception:
            self.store.fail_index(kb_id, '索引构建失败，请检查数据库和服务状态后重试。')

    def start_index(self, kb_id):
        config = EmbeddingConfig.from_env()
        revision, chunks = self.store.begin_index(kb_id)
        return revision, chunks, config

    def search(self, kb_id, query, top_k=5, mode='hybrid'):
        if mode not in {'bm25', 'vector', 'hybrid'}:
            raise HTTPException(422, '检索模式必须为 bm25、vector 或 hybrid。')
        if type(top_k) is not int or not 1 <= top_k <= 8:
            raise HTTPException(422, 'top_k 必须为 1–8。')
        initial_revision = self.store.get(kb_id)['revision']
        lexical = self.store.search(kb_id, query, 8 if mode == 'hybrid' else top_k)
        if mode == 'bm25':
            self.store.check_revision(kb_id, initial_revision)
            lexical['results'] = lexical['results'][:top_k]
            return lexical
        config = EmbeddingConfig.from_env()
        if self.vectors:
            kb = self.store.get(kb_id)
            if kb['index_state'] != 'ready' or kb['revision'] != kb['indexed_revision'] or not kb.get('vector_generation'):
                raise HTTPException(409, 'Milvus 索引尚未就绪，请重新构建索引。')
            if json.loads(kb['embedding_contract'] or 'null') != config.contract:
                raise HTTPException(409, 'Embedding 配置与索引不一致，请重新构建。')
            revision, rows = kb['revision'], []
        else:
            revision, rows = self.store.vector_snapshot(kb_id, config.contract)
        if revision != initial_revision:
            raise HTTPException(409, '检索期间文档发生变化，请重试。')
        query_text = f'Instruct: {config.query_instruction}\nQuery:{query}' if config.query_instruction else query
        query_vector = EmbeddingClient(config).embed([query_text])[0]
        try:
            minimum = float(os.getenv('EMBEDDING_MIN_SIMILARITY', '0.35'))
            if not math.isfinite(minimum) or not -1 <= minimum <= 1:
                raise ValueError()
        except ValueError:
            raise HTTPException(503, 'EMBEDDING_MIN_SIMILARITY 应为 -1 到 1 的有限数值。') from None
        vectors = []
        if self.vectors:
            for chunk_id, similarity in self.vectors.search(kb_id, kb['vector_generation'], query_vector, config.contract):
                if similarity >= minimum:
                    row = self.store.get_chunk(kb_id, chunk_id)
                    row.update(title=row['name'], vector_score=round(similarity, 6), url=f"/knowledge/{kb_id}/chunks/{row['id']}")
                    vectors.append(row)
        for row in rows:
            try:
                vector = normalize_vector(json.loads(row.pop('vector')), config.dimensions)
            except (ValueError, TypeError):
                raise HTTPException(503, '已存向量无效，请重新构建索引。') from None
            similarity = sum(a * b for a, b in zip(query_vector, vector))
            if similarity >= minimum:
                row.pop('term_counts'); row.pop('token_count')
                row.update(title=row['name'], vector_score=round(similarity, 6),
                           url=f"/knowledge/{kb_id}/chunks/{row['id']}")
                vectors.append(row)
        vectors.sort(key=lambda row: (-row['vector_score'], row['id']))
        candidates = {}
        for rank, row in enumerate(vectors[:30], 1):
            candidates[row['id']] = {**row, 'score': 1 / (60 + rank), 'keyword_score': None}
        if mode == 'hybrid':
            for rank, row in enumerate(lexical['results'], 1):
                if row['id'] in candidates:
                    candidates[row['id']]['score'] += 1 / (60 + rank)
                    candidates[row['id']]['keyword_score'] = row['score']
                else:
                    candidates[row['id']] = {**row, 'keyword_score': row['score'], 'vector_score': None, 'score': 1 / (60 + rank)}
        self.store.check_revision(kb_id, revision)
        ranked = sorted(candidates.values(), key=lambda row: (-row['score'], row['id']))
        return {'knowledge_base_id': str(kb_id), 'query': query, 'retrieval_mode': mode,
                'results': ranked[:top_k], 'total_matches': len(ranked), 'min_similarity': minimum,
                'embedding_model': config.model,
                'note': '向量使用余弦相似度；混合模式以 RRF 合并 BM25 与向量候选。分数不是答案置信度，阈值需按模型校准。'}
