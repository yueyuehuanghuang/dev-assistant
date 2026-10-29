import unittest
from unittest.mock import patch

import httpx

from local_embedding_server import app


class Vector:
    def tolist(self):
        return [1., 0., 0.]


class FakeModel:
    max_seq_length = 2048
    def get_sentence_embedding_dimension(self):
        return 3
    def tokenizer(self, inputs, **kwargs):
        return {'length': [len(text) for text in inputs]}
    def encode(self, inputs, **kwargs):
        return [Vector() for _ in inputs]


class LocalEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_shape_and_safety_limits(self):
        with patch('local_embedding_server.local_model', return_value=FakeModel()), patch.dict('os.environ', {'EMBEDDING_MODEL': 'fixture'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/v1/embeddings', json={'model': 'fixture', 'input': ['document']})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['data'][0]['embedding'], [1, 0, 0])
                self.assertEqual((await client.get('/health')).json()['local_only'], True)
                for payload in [{'model': 'wrong', 'input': ['a']}, {'model': 'fixture', 'input': ['a'], 'dimensions': 2},
                                {'model': 'fixture', 'input': ['x' * 2049]}, {'model': 'fixture', 'input': []}]:
                    self.assertEqual((await client.post('/v1/embeddings', json=payload)).status_code, 422)
