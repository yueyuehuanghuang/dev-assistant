"""Milvus HNSW indexes, isolated by embedding contract and immutable generation."""
import hashlib
import json
import os
from uuid import UUID

from fastapi import HTTPException
from embeddings import normalize_vector


class MilvusVectors:
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from pymilvus import MilvusClient
            self._client = MilvusClient(uri=os.getenv('MILVUS_URI', 'http://127.0.0.1:19530'),
                                        token=os.getenv('MILVUS_TOKEN', ''), timeout=30)
        return self._client

    @staticmethod
    def collection(contract):
        return 'devassistant_' + hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()[:24]

    def write(self, kb_id, generation, chunks, vectors, contract):
        from pymilvus import DataType
        name = self.collection(contract)
        dimensions = contract['dimensions']
        if len(chunks) != len(vectors):
            raise ValueError('Vector count mismatch')
        if not self.client.has_collection(name):
            schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field('id', DataType.VARCHAR, is_primary=True, max_length=80)
            schema.add_field('chunk_id', DataType.VARCHAR, max_length=36)
            schema.add_field('kb_id', DataType.VARCHAR, max_length=36)
            schema.add_field('generation', DataType.VARCHAR, max_length=36)
            schema.add_field('vector', DataType.FLOAT_VECTOR, dim=dimensions)
            indexes = self.client.prepare_index_params()
            indexes.add_index('vector', index_type='HNSW', metric_type='COSINE', params={'M': 16, 'efConstruction': 128})
            self.client.create_collection(name, schema=schema, index_params=indexes, consistency_level='Strong')
        data = [{'id': generation + ':' + chunk['id'], 'chunk_id': chunk['id'], 'kb_id': str(UUID(str(kb_id))),
                 'generation': str(UUID(generation)), 'vector': normalize_vector(vector, dimensions)}
                for chunk, vector in zip(chunks, vectors)]
        for start in range(0, len(data), 128):
            self.client.upsert(name, data[start:start + 128])
        self.client.flush(name)
        self.client.load_collection(name)

    def search(self, kb_id, generation, vector, contract):
        expression = f'kb_id == "{UUID(str(kb_id))}" and generation == "{UUID(generation)}"'
        try:
            results = self.client.search(self.collection(contract), [normalize_vector(vector, contract['dimensions'])],
                filter=expression, limit=30, output_fields=['chunk_id'],
                search_params={'metric_type': 'COSINE', 'params': {'ef': 64}}, consistency_level='Strong')
            return [(hit['entity']['chunk_id'], float(hit['distance'])) for hit in results[0]]
        except Exception as exc:
            raise HTTPException(503, 'Milvus 检索失败，请检查服务与索引状态。') from exc
