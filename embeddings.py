"""Configurable compatible /embeddings client; never substitutes synthetic vectors."""
from dataclasses import dataclass
import math
import os
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException


def normalize_vector(vector, dimensions):
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise ValueError('dimension mismatch')
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
        raise ValueError('invalid vector')
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError('zero or non-finite vector')
    return [value / norm for value in vector]


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dimensions: int
    api_key: str
    send_dimensions: bool = False
    query_instruction: str = ''

    @classmethod
    def from_env(cls):
        base = os.getenv('EMBEDDING_BASE_URL', '').strip().rstrip('/')
        model = os.getenv('EMBEDDING_MODEL', '').strip()
        key = os.getenv('EMBEDDING_API_KEY', '').strip()
        try:
            dimensions = int(os.getenv('EMBEDDING_DIMENSIONS', '0'))
            parsed = urlsplit(base)
            local = parsed.hostname in {'localhost', '127.0.0.1', '::1', 'host.docker.internal'}
            valid = (parsed.scheme == 'https' or (parsed.scheme == 'http' and local)) and parsed.hostname
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            if not valid or not model or len(model) > 200 or not 1 <= dimensions <= 8192 or (not key and not local):
                raise ValueError()
        except ValueError:
            raise HTTPException(503, '请在 .env 配置独立的 EMBEDDING_BASE_URL、EMBEDDING_MODEL、EMBEDDING_DIMENSIONS 和 EMBEDDING_API_KEY。') from None
        instruction = os.getenv('EMBEDDING_QUERY_INSTRUCTION', '').strip()
        if len(instruction) > 500:
            raise HTTPException(503, 'EMBEDDING_QUERY_INSTRUCTION 不应超过 500 字。')
        return cls(base, model, dimensions, key, os.getenv('EMBEDDING_SEND_DIMENSIONS', 'false').lower() == 'true', instruction)

    @property
    def contract(self):
        return {'endpoint': self.base_url, 'model': self.model, 'dimensions': self.dimensions,
                'query_instruction': self.query_instruction,
                'preprocessing': 'title+chunk-1000-overlap160-v1'}


class EmbeddingClient:
    def __init__(self, config):
        self.config = config

    def embed(self, texts):
        vectors = []
        try:
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                for start in range(0, len(texts), 16):
                    batch = texts[start:start + 16]
                    payload = {'model': self.config.model, 'input': batch}
                    if self.config.send_dimensions:
                        payload['dimensions'] = self.config.dimensions
                    headers = {'Authorization': f'Bearer {self.config.api_key}'} if self.config.api_key else {}
                    response = client.post(self.config.base_url + '/embeddings', headers=headers, json=payload)
                    response.raise_for_status()
                    document = response.json()
                    rows = document['data']
                    if not isinstance(rows, list) or len(rows) != len(batch):
                        raise ValueError()
                    if any(type(row.get('index')) is not int for row in rows):
                        raise ValueError()
                    rows = sorted(rows, key=lambda row: row['index'])
                    if [row['index'] for row in rows] != list(range(len(batch))):
                        raise ValueError()
                    vectors.extend(normalize_vector(row['embedding'], self.config.dimensions) for row in rows)
            return vectors
        except httpx.TimeoutException:
            raise HTTPException(504, 'Embedding 服务请求超时，未更新向量索引。') from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in {401, 403, 429}:
                status = 502
            raise HTTPException(status, 'Embedding 服务拒绝请求或返回错误，请检查模型、权限及额度。') from None
        except httpx.RequestError:
            raise HTTPException(502, '无法连接 Embedding 服务。') from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(502, 'Embedding 返回数量、顺序、维度或数值无效，拒绝写入混合向量空间。') from None
